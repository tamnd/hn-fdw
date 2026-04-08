"""hn-fdw — query the open-index/hacker-news Parquet dataset from Postgres."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("hn-fdw")
except PackageNotFoundError:  # pragma: no cover - editable install w/o metadata
    __version__ = "0.0.0"

__all__ = ["__version__"]
