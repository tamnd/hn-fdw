"""Discover what's available in the Hugging Face dataset.

The bootstrap doesn't actually need a file list (the DuckDB view uses a glob),
but it's nice to print one for `hn-fdw discover`, and it's useful in tests.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass

from huggingface_hub import HfApi

_MONTH_FILE_RE = re.compile(r"^data/(\d{4})/\d{4}-\d{2}\.parquet$")
_LIVE_FILE_RE = re.compile(r"^today/(\d{4})/(\d{2})/(\d{2})/\d{2}/\d{2}\.parquet$")


@dataclass(frozen=True, slots=True)
class DatasetInventory:
    """Snapshot of what the source dataset currently exposes."""

    repo_id: str
    revision: str
    monthly_files: tuple[str, ...]
    live_files: tuple[str, ...]

    @property
    def years(self) -> tuple[int, ...]:
        years: set[int] = set()
        for path in self.monthly_files:
            m = _MONTH_FILE_RE.match(path)
            if m:
                years.add(int(m.group(1)))
        return tuple(sorted(years))

    @property
    def files_by_year(self) -> dict[int, list[str]]:
        out: dict[int, list[str]] = defaultdict(list)
        for path in self.monthly_files:
            m = _MONTH_FILE_RE.match(path)
            if m:
                out[int(m.group(1))].append(path)
        return {year: sorted(files) for year, files in sorted(out.items())}


def fetch_inventory(repo_id: str, revision: str = "main") -> DatasetInventory:
    """Ask the Hugging Face API for the current file list.

    This is a single REST call. We never download Parquet bytes from here.
    """
    api = HfApi()
    paths = api.list_repo_files(repo_id=repo_id, revision=revision, repo_type="dataset")

    monthly: list[str] = []
    live: list[str] = []
    for p in paths:
        if _MONTH_FILE_RE.match(p):
            monthly.append(p)
        elif _LIVE_FILE_RE.match(p):
            live.append(p)

    return DatasetInventory(
        repo_id=repo_id,
        revision=revision,
        monthly_files=tuple(sorted(monthly)),
        live_files=tuple(sorted(live)),
    )
