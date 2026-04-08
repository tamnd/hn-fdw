# hn-fdw

All of Hacker News, queryable from Postgres, with zero copies.

```sql
SELECT id, title, score, "by"
FROM hn.stories
WHERE id = 8863;
```
```
  id  |                     title                      | score |    by
------+------------------------------------------------+-------+----------
 8863 | My YC app: Dropbox - Throw away your USB drive |   104 | dhouston
```

That query runs against 47+ million rows of Parquet files that live on the
Hugging Face CDN. Postgres reads exactly the row groups it needs, fetches
exactly the columns you SELECTed, and returns rows. Nothing is copied to
disk. There is no ETL, no cron job, no `INSERT INTO` anywhere in the
project.

## Quickstart

```bash
git clone https://github.com/tamnd/hn-fdw
cd hn-fdw
docker compose up -d

# wait ~2 minutes for the first-run bootstrap (it has to open every
# Parquet file in the dataset once), then:
psql postgresql://hn:hn@localhost:5432/hn
```

```sql
hn=# SELECT id, type, "by", title
     FROM hn.items
     WHERE id IN (1, 2, 3, 8863)
     ORDER BY id;
  id  | type |    by    |                     title
------+------+----------+------------------------------------------------
    1 |    1 | pg       | Y Combinator
    2 |    1 | phyllis  | A Student's Guide to Startups
    3 |    1 | phyllis  | Woz Interview: the early days of Apple
 8863 |    1 | dhouston | My YC app: Dropbox - Throw away your USB drive
```

Four items, pulled directly from Parquet row groups on the Hugging Face
CDN. The first ever Hacker News post, the second, the third, and the
Dropbox launch. Nothing was downloaded ahead of time.

## What this actually is

Three pieces, glued together with care:

1. **A Hugging Face dataset.** [`open-index/hacker-news`](https://huggingface.co/datasets/open-index/hacker-news)
   publishes the entire Hacker News firehose as Parquet files. One file per
   month, plus 5-minute live blocks for the current day. Sorted by `id`,
   compressed with zstd, schema is stable.

2. **DuckDB.** Best-in-class Parquet reader. Its `httpfs` extension knows
   how to issue HTTP range requests over `hf://` URLs. Column projection
   and predicate pushdown both work, so a query like `SELECT title FROM
   hn.stories WHERE id = 12345` downloads kilobytes, not megabytes.

3. **`duckdb_fdw`.** A SQL/MED foreign data wrapper that lets Postgres
   delegate scans to an embedded DuckDB instance. The result behaves like a
   regular Postgres table. You can join it with local tables, wrap it in
   views, expose it through PostgREST, point Grafana at it, whatever.

The Python package in this repo (`hn-fdw`) is the small bit of glue that
generates the DuckDB views and the Postgres foreign-table imports. It runs
once, at container startup. After that the only Python on the hot path is
zero.

## How it works

```
                                 HTTPS range reads
   psql      Postgres 18    duckdb_fdw      DuckDB        Hugging Face
   ----  -->  --------  -->  ---------  -->  ------  -->  ------------
   query     planner /        translates     httpfs +      parquet
   client    foreign table    SQL into       parquet       files (no
             scan             DuckDB SQL     readers       local copy)
```

When you run a query against `hn.stories`, the Postgres planner sees a
foreign table and hands the scan to `duckdb_fdw`. The wrapper opens a
DuckDB connection (in-process, via libduckdb), executes the equivalent
DuckDB query, and streams rows back. DuckDB itself resolves
`hn.stories` to a view that wraps `read_parquet('hf://datasets/.../data/*/*.parquet')`,
fetches only the row groups whose statistics overlap your `WHERE` clause,
and only the columns you actually `SELECT`ed.

The full architecture, schema mapping, and design trade-offs live in
[SPEC.md](./SPEC.md).

## What you get in Postgres

A schema called `hn` with seven foreign tables:

| Table             | Description                                          |
|-------------------|------------------------------------------------------|
| `hn.items`        | Every Hacker News item ever, all 47M+ of them.       |
| `hn.stories`      | Just stories (`type = 1`).                           |
| `hn.comments`     | Just comments (`type = 2`).                          |
| `hn.polls`        | Polls (`type = 3`).                                  |
| `hn.poll_options` | Poll options (`type = 4`).                           |
| `hn.jobs`         | Job postings (`type = 5`).                           |
| `hn.live_items`   | Today's items, refreshed every 5 minutes upstream.   |

All seven share the same shape:

```
Column      | Type
------------+------------------
id          | bigint
deleted     | smallint
type        | smallint
by          | text
time        | timestamp with time zone
text        | text
dead        | smallint
parent      | bigint
poll        | bigint
kids        | bigint[]
url         | text
score       | integer
title       | text
parts       | bigint[]
descendants | integer
words       | text[]
```

## Things you can do with this

Every query below was actually run against this container. The outputs
are the real, verbatim results from the Hacker News dataset as of
April 2026.

**Top stories of a single month.** DuckDB uses the `time` column's min/max
stats plus the file names to open only the four Parquet files that cover
March 2026.

```sql
SELECT id, score, title, "by"
FROM hn.stories
WHERE time >= '2026-03-01' AND time < '2026-04-01'
  AND dead = 0 AND deleted = 0
ORDER BY score DESC
LIMIT 10;
```
```
    id    | score |                                      title                                      |       by
----------+-------+---------------------------------------------------------------------------------+----------------
 47570269 |   569 | Copilot edited an ad into my PR                                                 | pavo-etc
 47548243 |   292 | If you don't opt out by Apr 24 GitHub will train on your private repos          | vmg12
 47340079 |   287 | Don't post generated/AI-edited comments. HN is for conversation between humans. | usefulposter
 47454782 |   206 | Chuck Norris Has Died                                                           | mp3il
 47438723 |   198 | Astral to Join OpenAI                                                           | ibraheemdev
 47261688 |   179 | Judge Orders Government to Begin Refunding More Than $130B in Tariffs           | JumpCrisscross
 47202032 |   173 | Claude becomes number one app on the U.S. App Store                             | byincugnito
 47232453 |   170 | Apple Introduces MacBook Pro with All‑New M5 Pro and M5 Max                     | scrlk
 47522709 |   167 | The EU still wants to scan  your private messages and photos                    | MrBruh
 47570666 |   167 | The curious case of retro demo scene graphics                                   | zdw
```

**Dig up a specific author's greatest hits.** Column projection plus a
simple filter; DuckDB reads just the columns and row groups it needs.

```sql
SELECT id, score, title, time::date AS date
FROM hn.stories
WHERE "by" = 'pg' AND score >= 100 AND time >= '2016-01-01'
ORDER BY score DESC
LIMIT 10;
```
```
    id    | score |    title     |    date
----------+-------+--------------+------------
 21231208 |  1288 | Show HN: Bel | 2019-10-12
```

**What's on the front page right now.** `hn.live_items` reads the 5-minute
live blocks under `today/`, which the upstream dataset publishes as new
Parquet files every few minutes.

```sql
SELECT id, score, title, "by"
FROM hn.live_items
WHERE type = 1 AND title IS NOT NULL
ORDER BY score DESC NULLS LAST
LIMIT 5;
```
```
    id    | score |                                   title                                   |       by
----------+-------+---------------------------------------------------------------------------------+-----------------
 47691380 |    10 | ICE acknowledges it is using powerful spyware                                   | helterskelter
 47690485 |     3 | 92% of MCP servers have security issues (and how to fix it)                     | nicholasfvelten
 47690037 |     3 | Show HN: CertKit for automating SSL certs to Windows, JKS, and appliances       | eric_trackjs
 47687248 |     3 | Škoda DuoBell: A bicycle bell that penetrates noise-cancelling headphones       | ra
 47690962 |     3 | Cogito: Beautiful AI Markdown Editor for Mac                                    | 0xferruccio
```

**Bounded aggregate on the early archive.** `WHERE id BETWEEN ...` hits
Parquet row-group statistics; only the first handful of monthly files
end up being touched.

```sql
SELECT count(*)                              AS total_items,
       count(*) FILTER (WHERE type = 1)      AS stories,
       count(*) FILTER (WHERE type = 2)      AS comments
FROM hn.items
WHERE id BETWEEN 1 AND 1000000;
```
```
 total_items | stories | comments
-------------+---------+----------
      997873 |  205760 |   789457
```

**Join foreign tables with a local Postgres table.** Postgres does the
join locally after `duckdb_fdw` returns only the rows matching the time
filter. No data is staged.

```sql
CREATE TEMP TABLE watchlist (author text PRIMARY KEY);
INSERT INTO watchlist VALUES ('pg'), ('tptacek'), ('patio11'), ('jgrahamc');

SELECT s."by", s.score, s.title, s.time::date AS date
FROM hn.stories s
JOIN watchlist w ON w.author = s."by"
WHERE s.time >= '2026-01-01' AND s.dead = 0 AND s.deleted = 0
ORDER BY s.score DESC
LIMIT 8;
```
```
    by    | score |                                      title                                      |    date
----------+-------+---------------------------------------------------------------------------------+------------
 jgrahamc |     3 | Claude broke a ZIP password in a smart way                                      | 2026-03-14
 jgrahamc |     3 | What we know about Iran's Internet shutdown                                     | 2026-01-13
 jgrahamc |     2 | There's a ridiculous amount of tech in a disposable vape                        | 2026-01-08
 tptacek  |     1 | Robust and efficient quantum-safe HTTPS                                         | 2026-02-27
 jgrahamc |     1 | Batteries included: how AI will transform the who and how of programming (2023) | 2026-03-04
 jgrahamc |     1 | Quantum frontiers may be closer than they appear                                | 2026-03-25
 jgrahamc |     1 | Maistro                                                                         | 2026-04-03
 jgrahamc |     1 | On Cloudflare                                                                   | 2026-01-09
```

## CLI

The Python tool that bootstraps the database is also useful on its own.

```
$ hn-fdw --help

 Usage: hn-fdw [OPTIONS] COMMAND [ARGS]...

 Query the open-index/hacker-news Parquet dataset from Postgres, no copies.

 Options
   --verbose  -v        Verbose logging.
   --help               Show this message and exit.

 Commands
   version    Print the package version.
   discover   List the parquet files currently published in the Hugging Face
              dataset.
   bootstrap  Build the DuckDB catalog and create the Postgres foreign tables.
   check      Run a tiny smoke query against the foreign tables.
   sql        Print the SQL the bootstrap would run.
```

`hn-fdw discover` is handy for sanity-checking what the upstream dataset
currently exposes:

```
$ hn-fdw discover
Repo: open-index/hacker-news@main
                            Monthly archive
 Year   Files   First                       Last
 2006       3   data/2006/2006-10.parquet   data/2006/2006-12.parquet
 2007      12   data/2007/2007-01.parquet   data/2007/2007-12.parquet
 ...
 2025      12   data/2025/2025-01.parquet   data/2025/2025-12.parquet
 2026       4   data/2026/2026-01.parquet   data/2026/2026-04.parquet

Live blocks today: 184 files (today/<YYYY>/<MM>/<DD>/<HH>/<MM>.parquet)
Total monthly files: 235  Years: 21
```

`hn-fdw sql duckdb` and `hn-fdw sql postgres` print the exact DDL the
bootstrap will execute, in case you want to read it, audit it, or apply
it by hand to a Postgres you already have.

## Configuration

Everything is environment variables, all prefixed `HN_FDW_`:

| Variable                 | Default                                  | What it does                              |
|--------------------------|------------------------------------------|-------------------------------------------|
| `HN_FDW_HF_REPO`         | `open-index/hacker-news`                 | The dataset to mount.                     |
| `HN_FDW_HF_REVISION`     | `main`                                   | Branch, tag, or commit SHA. Pin for repro.|
| `HN_FDW_DUCKDB_PATH`     | `/var/lib/postgresql/duckdb/hn.duckdb`   | Where the (tiny) catalog file lives.      |
| `HN_FDW_PG_DSN`          | `postgresql://hn:hn@localhost:5432/hn`   | How the bootstrap reaches Postgres.       |
| `HN_FDW_SCHEMA`          | `hn`                                     | Postgres schema for the foreign tables.   |
| `HN_FDW_SERVER`          | `hn_duckdb`                              | Foreign server name.                      |
| `HN_FDW_HTTP_TIMEOUT_MS` | `60000`                                  | DuckDB httpfs timeout per request.        |

## Performance, honestly

Let me set expectations properly, because the alternative is hype that
collapses on first contact.

This is a **query-a-dataset-at-rest** tool, not an OLTP index. There is
no local copy, no warmed connection pool, no pre-fetched metadata on the
hot path. Every query goes through `duckdb_fdw`, which opens a fresh
`libduckdb` connection inside the Postgres backend, which in turn makes
the HTTPS calls out to the Hugging Face CDN to read Parquet footers and
row groups. That startup work is real and it is not free.

On a home internet connection, from the cold container I used to make
this README, here is what the queries above actually took:

| Query                                                  | Time   |
|--------------------------------------------------------|--------|
| Point lookup, `hn.items WHERE id IN (1,2,3,8863)`      | ~81 s  |
| Point lookup, `hn.stories WHERE id = 8863`             | ~86 s  |
| `hn.live_items` top 5 by score                         | ~47 s  |
| Top 10 stories of March 2026                           | ~117 s |
| `pg`'s popular stories since 2016                      | ~102 s |
| Bounded count on the first million items               | ~80 s  |
| Watchlist join against 2026 stories                    | ~170 s |

Read that table honestly: the *fast* queries are in the **tens of
seconds**, and the slower ones are a couple of minutes. Most of that
time is not the data scan, it is DuckDB opening the files, fetching
Parquet footers over HTTPS, and building statistics. The actual
row-group read afterwards is comparatively tiny. This is what "zero
copies, pay-as-you-go" looks like in practice.

**What makes queries fast (in the relative sense above):**

- `WHERE id = ...` or `WHERE id BETWEEN ...`. The Parquet files are
  sorted by `id` and named by month, so DuckDB's file pruner skips
  whole files before opening them.
- A tight `WHERE time >= ... AND time < ...`. Same idea via the `time`
  column's row-group statistics.
- Column projection. `SELECT id, title FROM hn.stories` only downloads
  two columns' worth of bytes, not all sixteen.
- Reading from `hn.live_items` instead of the full archive. It points
  at today's 5-minute blocks only, so there is much less to open.

**What genuinely hurts:**

- `text LIKE '%term%'` over the entire archive. There is no FTS index;
  DuckDB has to scan the `text` column of every row group. Bound it
  with `time >= ...` first.
- Any query without a pushable predicate on `id` or `time`. Examples:
  `WHERE "by" = '...'` alone (forces DuckDB to scan every file),
  `ORDER BY score DESC LIMIT N` alone (no pruning signal), self-joins
  on `hn.items` across all of history.
- Running many small ad-hoc queries back to back. Each one pays the
  cold-start cost; `duckdb_fdw` does not share a DuckDB connection
  across Postgres sessions.

**If you need speed, materialise the slice.** Once you know which part
of the archive you care about, a single `CREATE MATERIALIZED VIEW` turns
it into an ordinary indexed Postgres table and the second query onwards
is sub-millisecond:

```sql
CREATE MATERIALIZED VIEW hn_2025_stories AS
SELECT id, title, "by", score, time, url
FROM hn.stories
WHERE time >= '2025-01-01' AND time < '2026-01-01';

CREATE INDEX ON hn_2025_stories (time);
CREATE INDEX ON hn_2025_stories ("by");
```

That is the only "ETL" this project ever encourages, and it is one SQL
statement against data you have already narrowed down.

## Development

```bash
uv sync                          # install deps
uv run pytest                    # run tests
uv run hn-fdw sql postgres       # see what the bootstrap would do
uv run hn-fdw sql duckdb         # ditto, but the DuckDB side

docker compose build             # build the image
docker compose up -d             # start postgres + the bootstrap
docker compose logs -f postgres  # watch the init script run
docker compose down -v           # nuke the volumes when you're done
```

The Python package has no runtime cost inside the container after bootstrap.
It builds the DuckDB catalog file once, runs the Postgres DDL once, and
exits. From then on it's just `psql -> postgres -> duckdb_fdw -> libduckdb`.

## Things this project does not try to do

- **Write back to Hacker News.** The foreign tables are read-only. Inserts
  and updates will be rejected.
- **Replace the Hacker News API.** If you need item IDs the second they
  appear, hit the Firebase API. The dataset publishes 5-minute blocks, so
  there's a few minutes of lag on `hn.live_items`.
- **Be a search engine.** No FTS, no embeddings, no vector index. If you
  want those, layer them on top.
- **Hide DuckDB.** It's right there. Open the `.duckdb` file with the
  DuckDB CLI and poke around if you want to.

## Credits

- [`open-index/hacker-news`](https://huggingface.co/datasets/open-index/hacker-news)
  for publishing the dataset and keeping it current.
- [DuckDB](https://duckdb.org/) and the `httpfs` extension authors for
  making `hf://` URLs feel like local files.
- [`duckdb_fdw`](https://github.com/alitrack/duckdb_fdw) for being the
  bridge that makes this whole thing five lines of SQL instead of a
  weekend project.
- [`uv`](https://github.com/astral-sh/uv) for making the Python side
  install in well under a second.

## License

MIT. See [LICENSE](./LICENSE).
