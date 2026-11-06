"""Small shared helpers: unit formatting, tolerant measurement, file inventory.

Path-redirect detection lives here rather than in each scanner. v0.2.2 gave it
one implementation because three modules had grown their own variant, and a
scanner that forgets to check a junction reports a directory that is really
somewhere else on the disk.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .schemas import FileRef

_UNIT_STEPS = ("B", "KB", "MB", "GB", "TB", "PB")

_FILE_ATTRIBUTE_REPARSE_POINT = 0x0400


class UnsafeScanRoot(RuntimeError):
    """Raised when a scan root is itself a symlink or a reparse point.

    A linked *root* is different from a linked entry inside a tree. An inner
    link is one directory the scan skips; a linked root means the caller's
    "selected" path is a door to somewhere else, and everything measured under
    it belongs to that somewhere else. Refusing the root is the only honest
    answer — returning zero bytes would look like an empty directory.
    """

    def __init__(self, path: str, rule: str = "linked_root") -> None:
        super().__init__(f"扫描根是符号链接或重解析点，已拒绝：{path}")
        self.path = path
        self.rule = rule



def is_reparse_point(path: str | os.PathLike[str]) -> bool:
    """True for Windows junctions, mount points and other reparse points.

    ``os.path.islink`` returns False for a directory junction, so attribute
    inspection is required: a junction can redirect a "safe" directory name to
    an arbitrary volume.
    """

    if os.name != "nt":
        return False
    try:
        info = os.stat(path, follow_symlinks=False)
    except OSError:
        return False
    attrs = getattr(info, "st_file_attributes", 0)
    return bool(attrs & _FILE_ATTRIBUTE_REPARSE_POINT)


def is_symlink_or_reparse(path: str | os.PathLike[str]) -> bool:
    """The one guard every scanner must use before descending or measuring.

    Covers POSIX symlinks (``stat.S_ISLNK``) and Windows reparse points. A path
    that cannot be stat'ed is reported as "not a redirect" and left to the
    caller's existence checks — inventing a redirect there would produce
    misleading skip counts on transient files.
    """

    try:
        info = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    return is_reparse_point(path)



def human_bytes(value: float) -> str:
    """Format a byte count the way a person would say it.

    Uses 1024-based units but labels them KB/MB/GB (the common informal
    convention). Report text always says "约" for derived values so a reader
    can tell estimates from measurements.
    """

    n = float(value)
    if n < 0:
        return f"-{human_bytes(-n)}"
    for unit in _UNIT_STEPS:
        if n < 1024 or unit == _UNIT_STEPS[-1]:
            if unit == "B":
                return f"{int(n)} B"
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso_now() -> str:
    return utc_now().isoformat(timespec="seconds")


@dataclass(frozen=True)
class SizeReport:
    """What a measurement saw, including what it refused to follow."""

    bytes: int = 0
    files: int = 0
    skipped_redirects: int = 0


def path_size_report(path: str | os.PathLike[str]) -> SizeReport:
    """Measure a file or directory without ever following a redirect.

    A symlinked or reparse-point subtree is skipped and counted in
    ``skipped_redirects`` rather than silently contributing zero — the caller
    can then say "this number excludes two linked directories" instead of
    presenting an unexplained shortfall.
    """

    root = Path(path)
    try:
        info = root.lstat()
    except OSError:
        return SizeReport()

    if is_symlink_or_reparse(root):
        return SizeReport(skipped_redirects=1)
    if not stat.S_ISDIR(info.st_mode):
        return SizeReport(bytes=info.st_size, files=1) if stat.S_ISREG(info.st_mode) else SizeReport()

    total = 0
    files = 0
    skipped = 0
    stack: list[Path] = [root]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if is_symlink_or_reparse(entry.path):
                            skipped += 1
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                            files += 1
                    except OSError:
                        continue
        except OSError:
            continue
    return SizeReport(bytes=total, files=files, skipped_redirects=skipped)


def path_size(path: str | os.PathLike[str]) -> tuple[int, int]:
    """Return ``(bytes, file_count)`` for a file or directory.

    Never raises on permission errors or broken links — an unreadable subtree
    contributes zero instead of aborting the whole scan. Symlinks and reparse
    points are not followed, so a link cannot inflate a total or create a cycle.
    Use :func:`path_size_report` when the skip count matters.
    """

    report = path_size_report(path)
    return report.bytes, report.files


def collect_files(
    directory: str | os.PathLike[str],
    *,
    max_files: int = 20_000,
) -> tuple[tuple[FileRef, ...], int]:
    """Inventory every regular, non-redirecting file under *directory*.

    Returns ``(refs, total_bytes)``. Symlinks and reparse points are skipped
    here as well as in the policy engine: defence in depth, because a plan is
    built from this list and later compared against it.

    A *linked root* is refused outright and yields an empty inventory. Skipping
    entries inside the tree is a filter; following a linked root would mean the
    inventory describes a different directory than the one the caller named.
    """

    refs: list[FileRef] = []
    total = 0
    root = Path(directory)

    if is_symlink_or_reparse(root):
        return (), 0

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [
            d
            for d in dirnames
            if not is_symlink_or_reparse(os.path.join(dirpath, d))
        ]
        for name in sorted(filenames):
            if len(refs) >= max_files:
                return tuple(refs), total
            full = os.path.join(dirpath, name)
            try:
                info = os.lstat(full)
            except OSError:
                continue
            if is_symlink_or_reparse(full) or not stat.S_ISREG(info.st_mode):
                continue
            refs.append(
                FileRef(
                    path=full,
                    size_bytes=info.st_size,
                    mtime_ns=info.st_mtime_ns,
                    device=info.st_dev,
                    inode=info.st_ino,
                )
            )
            total += info.st_size
    return tuple(refs), total


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
