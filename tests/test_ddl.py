"""Pure-string tests on the DDL generators. No DuckDB or Postgres needed."""

from __future__ import annotations

from hn_fdw.config import Settings
from hn_fdw.ddl import (
    ALL_VIEWS,
    ITEM_COLUMNS,
    TYPE_VIEWS,
    duckdb_bootstrap_sql,
    postgres_bootstrap_sql,
)


def _settings() -> Settings:
    return Settings(
        hf_repo="open-index/hacker-news",
        hf_revision="main",
        duckdb_path="/tmp/hn.duckdb",  # type: ignore[arg-type]
        pg_dsn="postgresql://hn:hn@localhost:5432/hn",
        server_name="hn_duckdb",
    )


def test_item_columns_match_source_schema() -> None:
    expected = {
        "id", "deleted", "type", "by", "time", "text", "dead", "parent",
        "poll", "kids", "url", "score", "title", "parts", "descendants", "words",
    }
    assert {c.name for c in ITEM_COLUMNS} == expected


def test_type_views_cover_all_item_types() -> None:
    assert {name for name, _, _ in TYPE_VIEWS} == {
        "stories", "comments", "polls", "poll_options", "jobs",
    }


def test_all_views_listed() -> None:
    assert ALL_VIEWS[0] == "items"
    assert ALL_VIEWS[-1] == "live_items"
    for name, _, _ in TYPE_VIEWS:
        assert name in ALL_VIEWS


def test_duckdb_sql_loads_httpfs_and_creates_views() -> None:
    sql = duckdb_bootstrap_sql(_settings())
    assert "INSTALL httpfs" in sql
    assert "LOAD httpfs" in sql
    assert "CREATE OR REPLACE VIEW items" in sql
    assert "hf://datasets/open-index/hacker-news@main/data/*/*.parquet" in sql
    assert "hf://datasets/open-index/hacker-news@main/today/**/*.parquet" in sql
    for name, where, _ in TYPE_VIEWS:
        assert f"CREATE OR REPLACE VIEW {name}" in sql
        assert where in sql


def test_postgres_sql_creates_extension_server_and_imports() -> None:
    sql = postgres_bootstrap_sql(_settings())
    assert 'CREATE SCHEMA IF NOT EXISTS "hn"' in sql
    assert "CREATE EXTENSION IF NOT EXISTS duckdb_fdw" in sql
    assert "CREATE SERVER" in sql
    assert "duckdb_fdw" in sql
    assert 'IMPORT FOREIGN SCHEMA "main"' in sql
    assert "INTO \"hn\"" in sql
    for v in ALL_VIEWS:
        assert v in sql


def test_postgres_sql_is_idempotent_friendly() -> None:
    """Re-running the bootstrap script must not error on a populated DB."""
    sql = postgres_bootstrap_sql(_settings())
    # Idempotency markers we rely on:
    assert "CREATE SCHEMA IF NOT EXISTS" in sql
    assert "CREATE EXTENSION IF NOT EXISTS" in sql
    assert "DROP FOREIGN TABLE IF EXISTS" in sql
    # CREATE SERVER itself is gated by a DO block, so just check for that.
    assert "pg_foreign_server" in sql


def test_postgres_sql_quotes_password_safely() -> None:
    s = _settings()
    s = Settings(
        hf_repo=s.hf_repo,
        hf_revision=s.hf_revision,
        duckdb_path="/tmp/it's-quoted.duckdb",  # type: ignore[arg-type]
        pg_dsn=s.pg_dsn,
        server_name=s.server_name,
    )
    sql = postgres_bootstrap_sql(s)
    # Single quotes inside the path must be doubled in the SQL literal.
    assert "it''s-quoted.duckdb" in sql
