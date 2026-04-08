"""Tests for the HF inventory parsing. We don't hit the network here."""

from __future__ import annotations

from hn_fdw.catalog import DatasetInventory


def test_files_by_year_groups_correctly() -> None:
    inv = DatasetInventory(
        repo_id="open-index/hacker-news",
        revision="main",
        monthly_files=(
            "data/2006/2006-10.parquet",
            "data/2006/2006-12.parquet",
            "data/2007/2007-01.parquet",
            "README.md",  # noise that must be ignored
        ),
        live_files=(
            "today/2026/04/08/00/00.parquet",
            "today/2026/04/08/00/05.parquet",
        ),
    )

    assert inv.years == (2006, 2007)
    by_year = inv.files_by_year
    assert by_year[2006] == [
        "data/2006/2006-10.parquet",
        "data/2006/2006-12.parquet",
    ]
    assert by_year[2007] == ["data/2007/2007-01.parquet"]


def test_inventory_handles_empty_inputs() -> None:
    inv = DatasetInventory(
        repo_id="x",
        revision="main",
        monthly_files=(),
        live_files=(),
    )
    assert inv.years == ()
    assert inv.files_by_year == {}
