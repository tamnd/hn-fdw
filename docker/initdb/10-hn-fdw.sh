#!/usr/bin/env bash
#
# Postgres init hook: bootstraps the hn-fdw foreign tables once, the first
# time the data directory is initialised. Subsequent container starts skip
# this script entirely (see the official postgres image entrypoint).
#

set -euo pipefail

# Talk to the locally-running postgres over the unix socket so we don't need
# a password and don't depend on TCP being up yet.
export HN_FDW_PG_DSN="postgresql:///${POSTGRES_DB}?host=/var/run/postgresql&user=${POSTGRES_USER}"

echo "[hn-fdw] building DuckDB catalog at ${HN_FDW_DUCKDB_PATH}"
hn-fdw bootstrap --duckdb-only

echo "[hn-fdw] applying Postgres DDL"
hn-fdw bootstrap --postgres-only

echo "[hn-fdw] done. Try:"
echo "         psql -c 'select count(*) from ${HN_FDW_SCHEMA}.stories'"
