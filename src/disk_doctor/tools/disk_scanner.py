"""Read-only disk analysis: volume usage, directory sizes, largest files.

Nothing here writes. That separation is what makes it safe to run a full
diagnosis before the user has agreed to anything.

v0.2.3: every public entry refuses a scan root that is itself a symlink or a
reparse point. Skipping linked *entries* inside a tree was never enough — a
linked root makes the whole measurement describe a directory the caller did not
name, and ``shutil.disk_usage`` on it reports a different volume.
"""

from __future__ import annotations

import heapq
import os
import shutil
from collections.abc import Iterable, Sequence
from pathlib import Path

from ..core.schemas import DiskVolume, FileEntry
from ..core.utils import (
    UnsafeScanRoot,
    is_symlink_or_reparse,
    path_size,
    path_size_report,
)


def require_safe_root(path: str | os.PathLike[str]) -> Path:
    """Return *path* as a ``Path``, or refuse if the root redirects elsewhere."""

    root = Path(path)
    if is_symlink_or_reparse(root):
        raise UnsafeScanRoot(str(root))
    return root


class DiskScanner:
    """Read-only filesystem measurements."""

    def __init__(self, *, max_entries: int = 200_000) -> None:
        self.max_entries = max_entries

    # -- volume ------------------------------------------------------------ #

    def volume(self, path: str | os.PathLike[str] = ".") -> DiskVolume:
        # The check comes first: measuring a linked root would report the
        # *target* volume, not the one the caller pointed at.
        target = require_safe_root(path)
        probe = target if target.is_dir() else target.parent
        usage = shutil.disk_usage(str(probe))
        percent = (usage.used / usage.total * 100.0) if usage.total else 0.0
        return DiskVolume(
            path=str(probe),
            total_bytes=usage.total,
            used_bytes=usage.used,
            free_bytes=usage.free,
            percent_used=round(percent, 1),
        )

    # -- directories ------------------------------------------------------- #

    def directory_sizes(
        self,
        root: str | os.PathLike[str],
        *,
        limit: int | None = None,
    ) -> list[FileEntry]:
        """Size of each immediate child directory of *root*, largest first."""

        base = require_safe_root(root)
        entries: list[FileEntry] = []
        try:
            with os.scandir(base) as it:
                for child in it:
                    if is_symlink_or_reparse(child.path):
                        continue
                    if not child.is_dir(follow_symlinks=False):
                        continue
                    # A junction inside the directory is skipped by path_size;
                    # the count it reports is kept out of the headline number.
                    report = path_size_report(child.path)
                    entries.append(FileEntry(path=child.path, size_bytes=report.bytes))
        except OSError:
            return []

        entries.sort(key=lambda e: e.size_bytes, reverse=True)
        return entries[:limit] if limit else entries

    # -- files ------------------------------------------------------------- #

    def top_files(
        self,
        root: str | os.PathLike[str],
        *,
        limit: int = 10,
        min_size: int = 0,
        exclude_components: Sequence[str] = (),
    ) -> list[FileEntry]:
        """The *limit* largest regular files under *root*, largest first."""

        base = require_safe_root(root)
        excluded = {c.lower() for c in exclude_components}
        heap: list[tuple[int, str, float]] = []
        seen = 0

        for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
            dirnames[:] = [
                d
                for d in dirnames
                if d.lower() not in excluded
                and not is_symlink_or_reparse(os.path.join(dirpath, d))
            ]
            for name in filenames:
                if seen >= self.max_entries:
                    break
                full = os.path.join(dirpath, name)
                try:
                    if is_symlink_or_reparse(full):
                        continue
                    st = os.stat(full, follow_symlinks=False)
                except OSError:
                    continue
                seen += 1
                if st.st_size < min_size:
                    continue
                item = (st.st_size, full, st.st_mtime)
                if len(heap) < limit:
                    heapq.heappush(heap, item)
                elif item[0] > heap[0][0]:
                    heapq.heapreplace(heap, item)

        ordered = sorted(heap, key=lambda t: t[0], reverse=True)
        return [FileEntry(path=p, size_bytes=s, mtime=m) for s, p, m in ordered]

    # -- composite --------------------------------------------------------- #

    def scan(
        self,
        root: str | os.PathLike[str],
        *,
        top_n: int = 10,
        dir_limit: int = 8,
    ) -> tuple[DiskVolume, list[FileEntry], list[FileEntry]]:
        require_safe_root(root)
        return (
            self.volume(root),
            self.directory_sizes(root, limit=dir_limit),
            self.top_files(root, limit=top_n),
        )

    def measure(self, paths: Iterable[str | os.PathLike[str]]) -> int:
        return sum(path_size(p)[0] for p in paths)
