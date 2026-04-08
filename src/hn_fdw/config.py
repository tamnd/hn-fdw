"""Runtime configuration for hn-fdw, sourced from environment variables."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All knobs in one place. Override via env vars (HN_FDW_*)."""

    model_config = SettingsConfigDict(
        env_prefix="HN_FDW_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    hf_repo: str = Field(
        default="open-index/hacker-news",
        description="Hugging Face dataset repo id.",
    )
    hf_revision: str = Field(
        default="main",
        description="Branch, tag, or commit SHA on the dataset repo.",
    )
    duckdb_path: Path = Field(
        default=Path("/var/lib/postgresql/duckdb/hn.duckdb"),
        description="Catalog file that holds the DuckDB views.",
    )
    pg_dsn: str = Field(
        default="postgresql://hn:hn@localhost:5432/hn",
        description="libpq connection string for the target Postgres database.",
    )
    schema_name: str = Field(
        default="hn",
        alias="HN_FDW_SCHEMA",
        description="Postgres schema that will hold the foreign tables.",
    )
    server_name: str = Field(
        default="hn_duckdb",
        description="Name of the foreign server created in Postgres.",
    )
    http_timeout_ms: int = Field(
        default=60_000,
        description="DuckDB httpfs timeout for HF range reads.",
    )

    @property
    def data_glob(self) -> str:
        """Glob covering every monthly Parquet file in the dataset."""
        return f"hf://datasets/{self.hf_repo}@{self.hf_revision}/data/*/*.parquet"

    @property
    def today_glob(self) -> str:
        """Glob covering today's live 5-minute blocks."""
        return f"hf://datasets/{self.hf_repo}@{self.hf_revision}/today/**/*.parquet"
