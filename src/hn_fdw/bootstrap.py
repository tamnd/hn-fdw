"""End-to-end orchestration: build the DuckDB catalog, then wire up Postgres."""

from __future__ import annotations

import logging
from pathlib import Path

import duckdb
import psycopg

from hn_fdw.config import Settings
from hn_fdw.ddl import duckdb_bootstrap_sql, postgres_bootstrap_sql

log = logging.getLogger(__name__)


def build_duckdb_catalog(settings: Settings) -> Path:
    """Create (or overwrite) the DuckDB file that holds the read_parquet views.

    The file holds *only views*. It contains no row data, so it's tiny and
    cheap to rebuild.
    """
    path = settings.duckdb_path
    path.parent.mkdir(parents=True, exist_ok=True)

    sql = duckdb_bootstrap_sql(settings)
    log.info("writing DuckDB catalog to %s", path)

    con = duckdb.connect(str(path))
    try:
        con.execute(sql)
        # Sanity check: confirm every view we expect actually got created.
        # We deliberately do *not* DESCRIBE the views, because that would
        # trigger an HTTP round trip to Hugging Face just to read parquet
        # footers. The bootstrap should be fast and offline-capable.
        existing = {
            row[0]
            for row in con.execute(
                "SELECT view_name FROM duckdb_views() WHERE internal = false"
            ).fetchall()
        }
        expected = {
            "items",
            "stories",
            "comments",
            "jobs",
            "polls",
            "poll_options",
            "live_items",
        }
        missing = expected - existing
        if missing:
            raise RuntimeError(f"DuckDB catalog is missing views: {sorted(missing)}")
    finally:
        con.close()

    return path


def apply_postgres_ddl(settings: Settings) -> None:
    """Run the Postgres bootstrap script against the configured DSN.

    The DSN is intentionally not logged. libpq DSNs come in two flavours
    (URI and key=value), and a half-correct redactor is worse than no log
    line at all.
    """
    sql = postgres_bootstrap_sql(settings)
    log.info("applying Postgres bootstrap DDL")

    with psycopg.connect(settings.pg_dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(sql)


def bootstrap_all(settings: Settings) -> None:
    build_duckdb_catalog(settings)
    apply_postgres_ddl(settings)
