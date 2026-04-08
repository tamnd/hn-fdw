"""SQL generation for both DuckDB (the catalog) and Postgres (the FDW).

The schema of the source dataset is fixed and small, so we hard-code it here
rather than introspect every parquet file at bootstrap. If the upstream schema
ever changes, this is the one place to edit.
"""

from __future__ import annotations

from dataclasses import dataclass

from hn_fdw.config import Settings


@dataclass(frozen=True, slots=True)
class Column:
    """One column of the items view, expressed in DuckDB's SQL dialect."""

    name: str
    duckdb_expr: str
    comment: str


# Order matches the source parquet for readability.
ITEM_COLUMNS: tuple[Column, ...] = (
    Column("id", 'CAST("id" AS BIGINT)', "Monotonic Hacker News item id."),
    Column("deleted", 'CAST("deleted" AS SMALLINT)', "1 if soft-deleted, else 0."),
    Column("type", 'CAST("type" AS SMALLINT)', "1 story, 2 comment, 3 poll, 4 pollopt, 5 job."),
    Column("by", '"by"', "Author username."),
    Column("time", '("time" AT TIME ZONE \'UTC\')', "Creation time, UTC."),
    Column("text", '"text"', "HTML body text."),
    Column("dead", 'CAST("dead" AS SMALLINT)', "1 if killed/flagged, else 0."),
    Column("parent", 'CAST("parent" AS BIGINT)', "Parent item id (for comments)."),
    Column("poll", 'CAST("poll" AS BIGINT)', "Poll id (for poll options)."),
    Column("kids", 'CAST("kids" AS BIGINT[])', "Direct child item ids."),
    Column("url", '"url"', "External URL for link stories."),
    Column("score", '"score"', "Net votes."),
    Column("title", '"title"', "Story / job / poll title."),
    Column("parts", 'CAST("parts" AS BIGINT[])', "Poll option ids."),
    Column("descendants", '"descendants"', "Total comments in the discussion."),
    Column("words", '"words"', "Tokenised words from title/text."),
)


# Type-specific views: (view_name, where_clause, comment).
TYPE_VIEWS: tuple[tuple[str, str, str], ...] = (
    ("stories", "type = 1", "Hacker News stories (type = 1)."),
    ("comments", "type = 2", "Hacker News comments (type = 2)."),
    ("polls", "type = 3", "Hacker News polls (type = 3)."),
    ("poll_options", "type = 4", "Hacker News poll options (type = 4)."),
    ("jobs", "type = 5", "Hacker News jobs (type = 5)."),
)

ALL_VIEWS: tuple[str, ...] = (
    "items",
    *(name for name, _, _ in TYPE_VIEWS),
    "live_items",
)


# --------------------------------------------------------------------------- #
# DuckDB DDL                                                                  #
# --------------------------------------------------------------------------- #


def duckdb_bootstrap_sql(settings: Settings) -> str:
    """Return the full DuckDB script that builds the catalog file."""

    select_list = ",\n        ".join(f"{c.duckdb_expr} AS \"{c.name}\"" for c in ITEM_COLUMNS)

    parts: list[str] = []

    parts.append("INSTALL httpfs;")
    parts.append("LOAD httpfs;")
    parts.append(f"SET http_timeout = {settings.http_timeout_ms};")
    # Make hf:// URLs anonymous-friendly. A token is picked up from HF_TOKEN if set.
    parts.append("SET enable_http_metadata_cache = true;")

    parts.append(
        f"""
CREATE OR REPLACE VIEW items AS
SELECT
        {select_list}
FROM read_parquet(
    '{settings.data_glob}',
    union_by_name = false,
    hive_partitioning = false
);
""".strip()
    )

    for name, where, _comment in TYPE_VIEWS:
        parts.append(
            f"CREATE OR REPLACE VIEW {name} AS SELECT * FROM items WHERE {where};"
        )

    parts.append(
        f"""
CREATE OR REPLACE VIEW live_items AS
SELECT
        {select_list}
FROM read_parquet(
    '{settings.today_glob}',
    union_by_name = false,
    hive_partitioning = false
);
""".strip()
    )

    return "\n\n".join(parts) + "\n"


# --------------------------------------------------------------------------- #
# Postgres DDL                                                                #
# --------------------------------------------------------------------------- #


def postgres_bootstrap_sql(settings: Settings) -> str:
    """Return the Postgres script that creates the FDW server and imports tables."""

    schema = _ident(settings.schema_name)
    server = _ident(settings.server_name)
    duckdb_path = str(settings.duckdb_path)
    view_list = ", ".join(ALL_VIEWS)

    comments = "\n".join(
        f"COMMENT ON FOREIGN TABLE {schema}.{name} IS {_lit(comment)};"
        for name, comment in _foreign_table_comments()
    )

    return f"""\
-- hn-fdw bootstrap. Idempotent: safe to re-run.

CREATE SCHEMA IF NOT EXISTS {schema};

CREATE EXTENSION IF NOT EXISTS duckdb_fdw;

DO $$
DECLARE
    srv_name text := {_lit(settings.server_name)};
    db_path  text := {_lit(duckdb_path)};
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_foreign_server WHERE srvname = srv_name) THEN
        EXECUTE format(
            'CREATE SERVER %I FOREIGN DATA WRAPPER duckdb_fdw OPTIONS (database %L)',
            srv_name, db_path
        );
    END IF;
END $$;

-- Drop and re-import so the catalog stays in sync with the DuckDB view list.
DROP FOREIGN TABLE IF EXISTS
    {", ".join(f"{schema}.{v}" for v in ALL_VIEWS)}
    CASCADE;

IMPORT FOREIGN SCHEMA "main"
    LIMIT TO ({view_list})
    FROM SERVER {server}
    INTO {schema};

{comments}
"""


def _foreign_table_comments() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = [
        ("items", "All Hacker News items, read live from Hugging Face Parquet files."),
    ]
    out.extend((name, comment) for name, _where, comment in TYPE_VIEWS)
    out.append(
        (
            "live_items",
            "Items from today's 5-minute live blocks; "
            "refreshed by the dataset author every few minutes.",
        )
    )
    return out


# --------------------------------------------------------------------------- #
# Tiny helpers                                                                #
# --------------------------------------------------------------------------- #


def _ident(name: str) -> str:
    """Quote a SQL identifier the dumb-but-correct way."""
    if not name.replace("_", "").isalnum():
        raise ValueError(f"refusing to quote unusual identifier: {name!r}")
    return f'"{name}"'


def _lit(value: str) -> str:
    """Single-quoted SQL string literal."""
    escaped = value.replace("'", "''")
    return f"'{escaped}'"
