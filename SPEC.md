# hn-fdw: Specification

Status: v0.1
Last reviewed: 2026-04-08

## 1. Goal

Expose the [`open-index/hacker-news`](https://huggingface.co/datasets/open-index/hacker-news)
Parquet dataset (47M+ rows of Hacker News items, updated continuously) as
queryable Postgres tables, **without copying any data into Postgres**.

The success criterion is a one-liner:

```bash
docker compose up -d
psql ... -c "select id, title, score from hn.stories order by score desc limit 5"
```

That query must execute against bytes still living on the Hugging Face CDN.

## 2. Non-goals

* Building a full ETL pipeline. We deliberately do not copy, transform, or
  schedule loads.
* Reimplementing Hacker News' Firebase API. The dataset already mirrors it.
* Vendoring DuckDB inside Postgres at the table-AM layer (that's what
  `pg_duckdb` does). This project uses a true SQL/MED foreign data wrapper so
  the result behaves like ordinary Postgres foreign tables.

## 3. Architecture in one picture

```
                                       HTTPS range requests
   psql / app  ──►  Postgres 18  ──►  duckdb_fdw  ──►  DuckDB  ──►  HuggingFace CDN
                       │                  │             │          (parquet files,
                       │                  │             │           never copied)
                       │                  │             └─ httpfs + parquet readers
                       │                  └─ libduckdb.so loaded in-process
                       └─ foreign tables in schema `hn`
```

The data path is:

1. A SQL query lands on Postgres.
2. The planner sees a foreign table and hands it to `duckdb_fdw`.
3. `duckdb_fdw` translates the scan (with column projection and predicate
   pushdown) into a DuckDB SQL query against an embedded DuckDB instance.
4. DuckDB resolves the table to a view that wraps
   `read_parquet('hf://datasets/open-index/hacker-news/...')`.
5. DuckDB's `httpfs` extension issues HTTP **range** requests to fetch only
   the row groups and columns needed.
6. Bytes flow back: HF CDN → DuckDB → duckdb_fdw → Postgres → client.

No background sync. No materialised copy. The dataset author publishes a new
monthly Parquet file, and the next query sees it.

## 4. Why this combination

| Choice                | Why not the alternative |
|-----------------------|-------------------------|
| `duckdb_fdw`          | True FDW. Composes with regular Postgres tables, RLS, views, dblink, logical replication. `pg_duckdb` is a table-AM, not an FDW, and changes Postgres internals more invasively. |
| DuckDB under the hood | Best-in-class Parquet reader. `httpfs` already understands `hf://`. Zero glue code. |
| Hugging Face CDN      | The dataset is already there. The CDN handles range reads. We piggyback on its bandwidth. |
| `uv`                  | Reproducible Python lockfile, fast resolver, single binary. The whole tool installs in under 2 seconds in CI. |

## 5. Source dataset

* Repo: `open-index/hacker-news`
* Layout:
  * `data/<YYYY>/<YYYY>-<MM>.parquet`: one file per calendar month, sealed once the month is over.
  * `today/<YYYY>/<MM>/<DD>/<HH>/<MM>.parquet`: five-minute live blocks for the current day.
  * `stats.csv`, `stats_today.csv`: small bookkeeping files.
* Total rows (snapshot 2026-04-08): ~47.6M items.
* Compression: Zstandard level 22, sorted by `id`.

### Source schema

All Parquet files share one schema:

| Column        | Parquet type   | Postgres type  | Notes |
|---------------|----------------|----------------|-------|
| `id`          | uint32         | `bigint`       | Monotonic item id. |
| `deleted`     | uint8          | `smallint`     | 0/1 flag. |
| `type`        | int8           | `smallint`     | 1 story, 2 comment, 3 poll, 4 pollopt, 5 job. |
| `by`          | string         | `text`         | Author. |
| `time`        | timestamp      | `timestamptz`  | UTC. |
| `text`        | string         | `text`         | HTML body. |
| `dead`        | uint8          | `smallint`     | 0/1 flag. |
| `parent`      | uint32         | `bigint`       | Comment parent. |
| `poll`        | uint32         | `bigint`       | Pollopt parent. |
| `kids`        | list<uint32>   | `bigint[]`     | Direct children. |
| `url`         | string         | `text`         | Story URL. |
| `score`       | int32          | `integer`      | Net votes. |
| `title`       | string         | `text`         | Story / job / poll title. |
| `parts`       | list<uint32>   | `bigint[]`     | Poll option ids. |
| `descendants` | int32          | `integer`      | Comment count. |
| `words`       | list<string>   | `text[]`       | Tokens. |

The Postgres types are chosen to round-trip without precision loss.

## 6. Logical schema in Postgres

Everything lives in a dedicated `hn` schema. Six foreign tables:

| Table             | Backing DuckDB view                                                                                       | Filter        |
|-------------------|------------------------------------------------------------------------------------------------------------|---------------|
| `hn.items`        | `read_parquet('hf://datasets/open-index/hacker-news/data/*/*.parquet', hive_partitioning = false)`         | none          |
| `hn.stories`      | view over `hn.items`                                                                                       | `type = 1`    |
| `hn.comments`     | view over `hn.items`                                                                                       | `type = 2`    |
| `hn.polls`        | view over `hn.items`                                                                                       | `type = 3`    |
| `hn.poll_options` | view over `hn.items`                                                                                       | `type = 4`    |
| `hn.jobs`         | view over `hn.items`                                                                                       | `type = 5`    |
| `hn.live_items`   | `read_parquet('hf://datasets/open-index/hacker-news/today/**/*.parquet')`                                  | none          |

Filters live **inside DuckDB views**, not as Postgres views, so that
predicate pushdown reaches Parquet row-group statistics.

## 7. Components

### 7.1 Postgres image

A Dockerfile based on `postgres:18-bookworm` that builds `duckdb_fdw`
against the matching `libduckdb.so`. Build artefacts are copied into the
final stage; the runtime image carries only `libduckdb.so` and the
extension's shared object.

The image also installs Python 3.13 and `uv`, then installs the `hn-fdw`
package itself, so the bootstrap CLI is available inside the container.

### 7.2 Python package: `hn_fdw`

Tree:

```
src/hn_fdw/
  __init__.py     package metadata
  cli.py          Typer entry point
  config.py       pydantic-settings
  catalog.py      Hugging Face dataset listing + glob expansion
  ddl.py          DuckDB and Postgres DDL generation
  bootstrap.py    Orchestrates the end-to-end setup
```

Public CLI (`hn-fdw --help`):

| Command            | Purpose                                                                                  |
|--------------------|------------------------------------------------------------------------------------------|
| `discover`         | Print the parquet files in the dataset (year ranges, file count, total bytes if known). |
| `sql duckdb`       | Print the DuckDB DDL the bootstrap would run.                                            |
| `sql postgres`     | Print the Postgres DDL the bootstrap would run.                                          |
| `bootstrap`        | Initialise the DuckDB catalog and the Postgres schema/server/foreign tables.             |
| `check`            | Run a tiny smoke query (`select count(*) from hn.stories where time >= now() - '1 day'`).|

### 7.3 SQL bootstrap

The bootstrap is idempotent. Running it twice is a no-op. It performs:

1. `CREATE SCHEMA IF NOT EXISTS hn;`
2. `CREATE EXTENSION IF NOT EXISTS duckdb_fdw;`
3. Generates a DuckDB database file at `${DUCKDB_PATH}` and runs:
   * `INSTALL httpfs; LOAD httpfs;`
   * `CREATE OR REPLACE VIEW items AS SELECT ... FROM read_parquet('hf://...')`
   * One view per logical type.
4. `CREATE SERVER IF NOT EXISTS hn_duckdb FOREIGN DATA WRAPPER duckdb_fdw OPTIONS (database '${DUCKDB_PATH}');`
5. `IMPORT FOREIGN SCHEMA main LIMIT TO (...) FROM SERVER hn_duckdb INTO hn;`
6. Adds Postgres-side comments documenting each foreign table.

The DuckDB file holds **only views**, no row data, and is therefore tiny
(a few KB). It can be regenerated at any time.

## 8. Configuration

All configuration is via environment variables, validated by
`hn_fdw.config.Settings`:

| Env var             | Default                                                          | Meaning                              |
|---------------------|------------------------------------------------------------------|--------------------------------------|
| `HN_FDW_HF_REPO`    | `open-index/hacker-news`                                         | Source dataset.                      |
| `HN_FDW_HF_REVISION`| `main`                                                           | Branch / commit pin.                 |
| `HN_FDW_DUCKDB_PATH`| `/var/lib/postgresql/duckdb/hn.duckdb`                           | Catalog file.                        |
| `HN_FDW_PG_DSN`     | `postgresql://hn:hn@localhost:5432/hn`                            | Postgres connection.                 |
| `HN_FDW_SCHEMA`     | `hn`                                                             | Target schema.                       |
| `HN_FDW_SERVER`     | `hn_duckdb`                                                      | FDW server name.                     |
| `HN_FDW_HTTP_TIMEOUT_MS` | `60000`                                                     | DuckDB httpfs timeout.               |

## 9. Pushdown and performance

The interesting question is: which queries are fast?

Fast (DuckDB will read only a few row groups):

* `WHERE id BETWEEN x AND y`. DuckDB uses Parquet `id` min/max stats.
  Files are also named by month, so the file pruner skips entire months.
* `WHERE time >= '2025-01-01'`. Same idea via the `time` column stats.
* `WHERE type = 1` (any of the type-specific tables). Pushed into the
  DuckDB view; the optimiser combines it with file pruning.
* Column projection (e.g. `select id, title from hn.stories`). Only the
  referenced columns are downloaded.

Slow:

* `LIKE '%term%'` over `text` for the entire archive. There is no index.
  Use `WHERE time >= ...` to bound the scan, or add a Postgres
  materialised view for the slice you care about.
* Joins between `hn.items` and itself across the full archive. Bound at
  least one side.

Cache: DuckDB keeps an HTTP metadata cache and a small page cache in
process. We mount a volume at `/var/lib/postgresql/duckdb` so the catalog
file and the cache survive restarts.

## 10. Operational notes

* **Updates.** New monthly files appear automatically; the glob picks them
  up on the next query. New `today/...` files appear every five minutes.
  Nothing to do.
* **Pinning.** Set `HN_FDW_HF_REVISION` to a commit SHA for reproducible
  reads.
* **Outage on HF.** Queries fail with a clear `httpfs` error. Postgres
  itself stays up; only the foreign tables are affected.
* **Auth.** Public dataset, no token. If a private dataset is later
  required, set `HF_TOKEN` and DuckDB's `httpfs` will pick it up.

## 11. Out of scope (today)

* Write paths. Foreign tables are read-only. Inserts are rejected.
* Per-row authorisation. Add Postgres views with RLS if needed.
* Cross-region replication of the cache. The cache is per-container.

## 12. References

* Hugging Face dataset: <https://huggingface.co/datasets/open-index/hacker-news>
* `duckdb_fdw`: <https://github.com/alitrack/duckdb_fdw>
* DuckDB `httpfs` and `hf://` support: <https://duckdb.org/docs/extensions/httpfs/hugging_face>
* SQL/MED (foreign data wrappers): <https://wiki.postgresql.org/wiki/SQL/MED>
