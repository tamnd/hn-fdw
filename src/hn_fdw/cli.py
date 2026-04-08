"""Typer-based command line for hn-fdw."""

from __future__ import annotations

import logging
from typing import Annotated

import psycopg
import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from hn_fdw import __version__
from hn_fdw.bootstrap import apply_postgres_ddl, bootstrap_all, build_duckdb_catalog
from hn_fdw.catalog import fetch_inventory
from hn_fdw.config import Settings
from hn_fdw.ddl import duckdb_bootstrap_sql, postgres_bootstrap_sql

app = typer.Typer(
    name="hn-fdw",
    help="Query the open-index/hacker-news Parquet dataset from Postgres, no copies.",
    add_completion=False,
    no_args_is_help=True,
)

sql_app = typer.Typer(help="Print the SQL the bootstrap would run.")
app.add_typer(sql_app, name="sql")

console = Console()


def _settings() -> Settings:
    return Settings()  # reads env vars


@app.callback()
def _root(
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Verbose logging.")] = False,
) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        handlers=[RichHandler(console=console, show_time=False, show_path=False)],
    )


@app.command()
def version() -> None:
    """Print the package version."""
    console.print(f"hn-fdw {__version__}")


@app.command()
def discover() -> None:
    """List the parquet files currently published in the Hugging Face dataset."""
    s = _settings()
    console.print(f"[bold]Repo:[/bold] {s.hf_repo}@{s.hf_revision}")
    inv = fetch_inventory(s.hf_repo, s.hf_revision)

    table = Table(title="Monthly archive", header_style="bold")
    table.add_column("Year", justify="right")
    table.add_column("Files", justify="right")
    table.add_column("First")
    table.add_column("Last")
    for year, files in inv.files_by_year.items():
        table.add_row(str(year), str(len(files)), files[0], files[-1])

    console.print(table)
    console.print(
        f"\n[bold]Live blocks today:[/bold] {len(inv.live_files)} files "
        f"(today/<YYYY>/<MM>/<DD>/<HH>/<MM>.parquet)"
    )
    console.print(
        f"[bold]Total monthly files:[/bold] {len(inv.monthly_files)}  "
        f"[bold]Years:[/bold] {len(inv.years)}"
    )


@sql_app.command("duckdb")
def sql_duckdb() -> None:
    """Print the DuckDB DDL the bootstrap would run."""
    console.print(duckdb_bootstrap_sql(_settings()), markup=False, highlight=False)


@sql_app.command("postgres")
def sql_postgres() -> None:
    """Print the Postgres DDL the bootstrap would run."""
    console.print(postgres_bootstrap_sql(_settings()), markup=False, highlight=False)


@app.command()
def bootstrap(
    duckdb_only: Annotated[
        bool, typer.Option("--duckdb-only", help="Only build the DuckDB catalog file.")
    ] = False,
    postgres_only: Annotated[
        bool, typer.Option("--postgres-only", help="Only apply the Postgres DDL.")
    ] = False,
) -> None:
    """Build the DuckDB catalog and create the Postgres foreign tables."""
    s = _settings()
    if duckdb_only and postgres_only:
        raise typer.BadParameter("--duckdb-only and --postgres-only are mutually exclusive")

    if postgres_only:
        apply_postgres_ddl(s)
    elif duckdb_only:
        build_duckdb_catalog(s)
    else:
        bootstrap_all(s)

    console.print("[green]bootstrap complete[/green]")


@app.command()
def check() -> None:
    """Run a tiny smoke query against the foreign tables."""
    s = _settings()
    sql = f"SELECT count(*) FROM \"{s.schema_name}\".stories WHERE time >= now() - interval '1 day'"
    console.print(f"[dim]{sql}[/dim]")
    with psycopg.connect(s.pg_dsn) as conn, conn.cursor() as cur:
        cur.execute(sql)
        row = cur.fetchone()
    if row is None:
        console.print("[red]no rows returned[/red]")
        raise typer.Exit(code=1)
    console.print(f"[green]ok[/green] stories in last 24h: {row[0]}")
