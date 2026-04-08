# syntax=docker/dockerfile:1.7
#
# hn-fdw image: postgres 18 + duckdb_fdw + the hn-fdw bootstrap CLI.
#
# Two stages:
#   1. builder  compiles duckdb_fdw against postgresql-server-dev-18.
#   2. runtime  copies just the extension shared object, control file,
#               libduckdb.so, and the hn-fdw Python tool.
#
# Build:   podman build -t hn-fdw .
# Run:     podman run -p 5432:5432 hn-fdw
#

ARG POSTGRES_VERSION=18
# Pinned to a specific commit so the image is reproducible. v1.3.2 is the
# latest tag (Aug 2025), but it predates Postgres 18 support; the SHA
# below is the "add Postgres 18 support" commit on main (Nov 2025). Bump
# to a proper tag once one is cut upstream.
ARG DUCKDB_FDW_REF=870bc4366b
# DuckDB release to link against. Must match the duckdb_fdw commit above:
# its control file pins default_version = 1.4.1, and the C++ sources use
# DBConfigOptions.allow_unsigned_extensions, which was removed in DuckDB
# 1.5.x. The upstream download_libduckdb.sh is broken (uses the old
# "aarch64" filename; DuckDB renamed it to "arm64"), so the builder
# fetches the zip directly with the names DuckDB actually publishes.
ARG DUCKDB_VERSION=v1.4.1
# uv handles both the Python runtime and the package install. We let it
# fetch its own pinned CPython build rather than relying on whatever
# python3 the base image's distro happens to ship, so bumping PYTHON_VERSION
# here is the single source of truth.
ARG UV_VERSION=0.11.4
ARG PYTHON_VERSION=3.13

# --------------------------------------------------------------------------- #
# Stage 1: build duckdb_fdw                                                   #
# --------------------------------------------------------------------------- #
FROM postgres:${POSTGRES_VERSION}-trixie AS builder

ARG POSTGRES_VERSION
ARG DUCKDB_FDW_REF
ARG DUCKDB_VERSION
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        curl \
        git \
        libcurl4-openssl-dev \
        pkg-config \
        postgresql-server-dev-${POSTGRES_VERSION} \
        unzip \
 && rm -rf /var/lib/apt/lists/*

# `git clone --branch` doesn't accept commit SHAs, so fetch then check out.
WORKDIR /build
RUN git clone https://github.com/alitrack/duckdb_fdw.git \
 && cd duckdb_fdw \
 && git checkout "${DUCKDB_FDW_REF}"

WORKDIR /build/duckdb_fdw

# Fetch libduckdb ourselves instead of running the broken upstream
# download_libduckdb.sh. The zip contains libduckdb.so plus the C/C++
# headers the extension needs; everything lands next to the Makefile.
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
        amd64) duckdb_arch=amd64 ;; \
        arm64) duckdb_arch=arm64 ;; \
        *) echo "unsupported arch: $arch" >&2; exit 1 ;; \
    esac; \
    url="https://github.com/duckdb/duckdb/releases/download/${DUCKDB_VERSION}/libduckdb-linux-${duckdb_arch}.zip"; \
    echo "fetching $url"; \
    curl -fsSL -o /tmp/libduckdb.zip "$url"; \
    unzip -o /tmp/libduckdb.zip; \
    rm /tmp/libduckdb.zip; \
    ls -la libduckdb.so duckdb.h duckdb.hpp

RUN make USE_PGXS=1 \
 && make USE_PGXS=1 install \
 && install -m 0755 libduckdb.so /usr/local/lib/libduckdb.so

# --------------------------------------------------------------------------- #
# Stage 2: runtime                                                            #
# --------------------------------------------------------------------------- #
FROM postgres:${POSTGRES_VERSION}-trixie AS runtime

ARG POSTGRES_VERSION
ARG UV_VERSION
ARG PYTHON_VERSION
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        libcurl4 \
 && rm -rf /var/lib/apt/lists/*

# duckdb_fdw artefacts from the builder stage. The wildcard copy picks up
# the .control file plus all duckdb_fdw--*.sql variants without hard-coding
# a version number.
COPY --from=builder /usr/lib/postgresql/${POSTGRES_VERSION}/lib/duckdb_fdw.so \
                    /usr/lib/postgresql/${POSTGRES_VERSION}/lib/duckdb_fdw.so
COPY --from=builder /usr/share/postgresql/${POSTGRES_VERSION}/extension/duckdb_fdw* \
                    /usr/share/postgresql/${POSTGRES_VERSION}/extension/
COPY --from=builder /usr/local/lib/libduckdb.so /usr/local/lib/libduckdb.so
RUN echo /usr/local/lib > /etc/ld.so.conf.d/duckdb.conf && ldconfig

# uv (single static binary, no Python deps).
RUN curl -LsSf "https://astral.sh/uv/${UV_VERSION}/install.sh" | env UV_INSTALL_DIR=/usr/local/bin sh

# Have uv manage the Python runtime too. Whatever python3 the base image
# ships is ignored; uv fetches an exact CPython build (PYTHON_VERSION)
# once and caches it under /opt/uv-python so the venv is reproducible.
ENV UV_PYTHON_INSTALL_DIR=/opt/uv-python
RUN uv python install "${PYTHON_VERSION}"

# Install the hn-fdw Python package into a dedicated venv. Using uv for the
# install gives us a reproducible, fast resolve and a single binary on PATH.
WORKDIR /opt/hn-fdw
COPY pyproject.toml README.md ./
COPY src ./src
RUN uv venv --python "${PYTHON_VERSION}" /opt/venv \
 && VIRTUAL_ENV=/opt/venv uv pip install --no-cache . \
 && ln -sf /opt/venv/bin/hn-fdw /usr/local/bin/hn-fdw

# Postgres init hook: bootstraps the FDW the first time the cluster comes up.
COPY docker/initdb /docker-entrypoint-initdb.d

# Catalog directory persisted in a volume so the DuckDB views and the http
# metadata cache survive restarts.
RUN mkdir -p /var/lib/postgresql/duckdb \
 && chown -R postgres:postgres /var/lib/postgresql/duckdb
VOLUME /var/lib/postgresql/duckdb

ENV HN_FDW_DUCKDB_PATH=/var/lib/postgresql/duckdb/hn.duckdb \
    HN_FDW_HF_REPO=open-index/hacker-news \
    HN_FDW_HF_REVISION=main \
    HN_FDW_SCHEMA=hn \
    HN_FDW_SERVER=hn_duckdb

# Healthcheck reuses postgres' own pg_isready.
HEALTHCHECK --interval=10s --timeout=5s --start-period=30s --retries=10 \
    CMD pg_isready -U "${POSTGRES_USER:-postgres}" -d "${POSTGRES_DB:-postgres}" || exit 1
