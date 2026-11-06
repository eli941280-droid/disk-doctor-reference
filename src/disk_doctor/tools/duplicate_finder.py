"""Duplicate detection via size bucketing and content hashing.

Three stages, cheapest first:

1. group by size — eliminates almost everything without opening a file;
2. hash the first 64 KiB — separates same-sized but different files;
3. full SHA-256 only for survivors.

Results are *candidates*. Nothing here deletes; duplicates need human judgement
because "identical content" is not the same as "safe to remove".
"""

from __future__ import annotations

import hashlib
import os
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path

from ..core.policy_engine import PolicyEngine
from ..core.schemas import DuplicateGroup
from ..core.utils import is_symlink_or_reparse


class DuplicateFinder:
    def __init__(
        self,
        *,
        min_size: int = 1024,
        max_files: int = 50_000,
        chunk_size: int = 65_536,
        exclude_components: Sequence[str] = (".git", "node_modules", "__pycache__"),
    ) -> None:
        self.min_size = min_size
        self.max_files = max_files
        self.chunk_size = chunk_size
        self.exclude = {c.lower() for c in exclude_components}

    # -- hashing ----------------------------------------------------------- #

    def _hash(self, path: Path, limit: int | None = None) -> str:
        digest = hashlib.sha256()
        remaining = limit
        with path.open("rb") as fh:
            while True:
                size = self.chunk_size if remaining is None else min(self.chunk_size, remaining)
                if size <= 0:
                    break
                block = fh.read(size)
                if not block:
                    break
                digest.update(block)
                if remaining is not None:
                    remaining -= len(block)
        return digest.hexdigest()

    # -- collection -------------------------------------------------------- #

    def _collect(
        self, roots: Sequence[str | os.PathLike[str]], policy: PolicyEngine | None
    ) -> dict[int, list[Path]]:
        by_size: dict[int, list[Path]] = defaultdict(list)
        count = 0

        for root in roots:
            base = Path(root)
            # A linked root is a door to somewhere else. Refuse this root
            # entirely rather than walking a tree the caller did not name.
            if is_symlink_or_reparse(base):
                continue
            if not base.exists():
                continue
            for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
                dirnames[:] = [
                    d for d in dirnames
                    if d.lower() not in self.exclude
                    and not is_symlink_or_reparse(os.path.join(dirpath, d))
                ]
                for name in filenames:
                    if count >= self.max_files:
                        return by_size
                    full = Path(dirpath) / name
                    try:
                        if is_symlink_or_reparse(full):
                            continue
                        st = full.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if st.st_size < self.min_size:
                        continue
                    if policy is not None and not policy.allows(full):
                        continue
                    by_size[st.st_size].append(full)
                    count += 1
        return by_size

    # -- public API -------------------------------------------------------- #

    def find(
        self,
        roots: Sequence[str | os.PathLike[str]],
        *,
        policy: PolicyEngine | None = None,
        max_groups: int = 100,
    ) -> list[DuplicateGroup]:
        by_size = self._collect(roots, policy)
        size_groups = {size: paths for size, paths in by_size.items() if len(paths) > 1}

        results: list[DuplicateGroup] = []
        for size, paths in sorted(size_groups.items(), reverse=True):
            head_groups: dict[str, list[Path]] = defaultdict(list)
            for p in paths:
                try:
                    head_groups[self._hash(p, limit=self.chunk_size)].append(p)
                except OSError:
                    continue

            for head, candidates in head_groups.items():
                if len(candidates) < 2:
                    continue
                if size <= self.chunk_size:
                    digest = head
                    results.append(
                        DuplicateGroup(
                            digest=digest,
                            size_bytes=size,
                            paths=tuple(sorted(str(p) for p in candidates)),
                        )
                    )
                    continue

                full_groups: dict[str, list[Path]] = defaultdict(list)
                for p in candidates:
                    try:
                        full_groups[self._hash(p)].append(p)
                    except OSError:
                        continue
                for digest, files in full_groups.items():
                    if len(files) < 2:
                        continue
                    results.append(
                        DuplicateGroup(
                            digest=digest,
                            size_bytes=size,
                            paths=tuple(sorted(str(p) for p in files)),
                        )
                    )
            if len(results) >= max_groups:
                break

        results.sort(key=lambda g: g.wasted_bytes, reverse=True)
        return results[:max_groups]
