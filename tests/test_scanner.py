"""Scanner: ordering, limits, and tolerance of unreadable entries."""

from __future__ import annotations

from pathlib import Path

import pytest

from disk_doctor.tools.disk_scanner import DiskScanner


@pytest.fixture
def scanner():
    return DiskScanner()


def test_volume_reports_positive_totals(scanner, vault):
    volume = scanner.volume(vault)
    assert volume.total_bytes > 0
    assert 0 <= volume.percent_used <= 100


def test_volume_accepts_a_file_path(scanner, vault, make_file):
    target = make_file(vault / "f.bin", 16)
    assert scanner.volume(target).total_bytes > 0


def test_top_files_sorted_descending(scanner, vault, make_file):
    make_file(vault / "small.bin", 10)
    make_file(vault / "medium.bin", 500)
    make_file(vault / "large.bin", 5000)

    result = scanner.top_files(vault, limit=3)
    sizes = [f.size_bytes for f in result]
    assert sizes == sorted(sizes, reverse=True)
    assert result[0].size_bytes == 5000


def test_top_files_respects_limit(scanner, vault, make_file):
    for i in range(10):
        make_file(vault / f"f{i}.bin", 100 + i)
    assert len(scanner.top_files(vault, limit=3)) == 3


def test_top_files_min_size_filter(scanner, vault, make_file):
    make_file(vault / "tiny.bin", 10)
    make_file(vault / "big.bin", 5000)
    result = scanner.top_files(vault, limit=10, min_size=1000)
    assert [f.path for f in result] == [str(vault / "big.bin")]


def test_top_files_walks_nested_directories(scanner, vault, make_file):
    make_file(vault / "a" / "b" / "deep.bin", 2048)
    result = scanner.top_files(vault, limit=1)
    assert result[0].size_bytes == 2048


def test_directory_sizes_largest_first(scanner, vault, make_file):
    make_file(vault / "small" / "x.bin", 10)
    make_file(vault / "big" / "y.bin", 10_000)

    entries = scanner.directory_sizes(vault)
    assert [Path(e.path).name for e in entries][:2] == ["big", "small"]


def test_directory_sizes_on_missing_path_returns_empty(scanner, tmp_path):
    assert scanner.directory_sizes(tmp_path / "nope") == []


def test_top_files_on_missing_path_returns_empty(scanner, tmp_path):
    assert scanner.top_files(tmp_path / "nope", limit=5) == []


def test_scan_returns_three_parts(scanner, vault, make_file):
    make_file(vault / "d" / "f.bin", 100)
    volume, dirs, files = scanner.scan(vault, top_n=3, dir_limit=3)
    assert volume.total_bytes > 0
    assert len(dirs) == 1
    assert len(files) == 1


def test_measure_sums_inputs(scanner, vault, make_file):
    a = make_file(vault / "a.bin", 100)
    b = make_file(vault / "b.bin", 200)
    assert scanner.measure([a, b]) == 300
