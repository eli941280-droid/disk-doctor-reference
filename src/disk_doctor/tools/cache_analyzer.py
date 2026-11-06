"""Positive identification of cache directories.

v0.2.1 rewrites this module around one rule: **a directory is cleanable only if
its name *and* its contents positively prove it is a rebuildable cache.**

The v0.2.0 version treated any directory called ``Temp`` or ``tmp`` as safe.
That is not evidence — a user's notes live in ``Temp`` just as easily as a
build artefact does. Name-based guessing is gone (P0-2).

What remains is a short, explicit allow-list:

* ``__pycache__`` — and only its ``.pyc`` files. A ``.py`` or ``.txt`` sitting
  in there is not bytecode and is not offered.
* ``.pytest_cache`` / ``.ruff_cache`` — only when a valid ``CACHEDIR.TAG`` with
  the registered signature is present.

Everything else (pip, npm, browser, node_modules, Gradle, any directory called
``cache``) is *optional*: it is produced only when the caller passes
``allow_optional=True``, it is always ``CAUTION``, and it is never part of a
default plan.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from ..core.policy_engine import PolicyEngine, is_within
from ..core.schemas import Candidate, FileRef, RiskLevel
from ..core.utils import collect_files, is_symlink_or_reparse

# Registered CACHEDIR.TAG signature (https://bford.info/cachedir/).
CACHEDIR_SIGNATURE = b"Signature: 8a477f597d28d172789f06886806bc55"
TAG_FILENAME = "CACHEDIR.TAG"

PYCACHE_DIR = "__pycache__"
TAG_REQUIRED_DIRS: dict[str, tuple[str, str]] = {
    ".pytest_cache": ("pytest 缓存", "带 CACHEDIR.TAG 标记，测试运行时可重建"),
    ".ruff_cache": ("ruff 缓存", "带 CACHEDIR.TAG 标记，检查运行时可重建"),
}

# Never produced unless the caller explicitly opts in. Always CAUTION.
OPTIONAL_DIRS: dict[str, tuple[str, str]] = {
    "node_modules": ("Node 依赖", "可重装，但需要重新下载依赖"),
    "_cacache": ("npm 下载缓存", "需要时会重新下载"),
    "npm-cache": ("npm 下载缓存", "需要时会重新下载"),
}


@dataclass(frozen=True)
class Signature:
    """Static description of an accepted cache directory."""

    name: str
    category: str
    risk: RiskLevel
    reason: str
    suffix_filter: str | None = None
    requires_tag: bool = False


SAFE_SIGNATURES: tuple[Signature, ...] = (
    Signature(
        name=PYCACHE_DIR,
        category="Python 字节码",
        risk=RiskLevel.SAFE,
        reason="仅 .pyc 字节码，解释器可自动重建",
        suffix_filter=".pyc",
    ),
    Signature(
        name=".pytest_cache",
        category=TAG_REQUIRED_DIRS[".pytest_cache"][0],
        risk=RiskLevel.SAFE,
        reason=TAG_REQUIRED_DIRS[".pytest_cache"][1],
        requires_tag=True,
    ),
    Signature(
        name=".ruff_cache",
        category=TAG_REQUIRED_DIRS[".ruff_cache"][0],
        risk=RiskLevel.SAFE,
        reason=TAG_REQUIRED_DIRS[".ruff_cache"][1],
        requires_tag=True,
    ),
)

OPTIONAL_SIGNATURES: tuple[Signature, ...] = tuple(
    Signature(name=name, category=category, risk=RiskLevel.CAUTION, reason=reason)
    for name, (category, reason) in OPTIONAL_DIRS.items()
)


def has_valid_cachedir_tag(directory: str | os.PathLike[str]) -> bool:
    """True if *directory* contains a CACHEDIR.TAG with the registered signature."""

    tag = Path(directory) / TAG_FILENAME
    try:
        if is_symlink_or_reparse(tag):
            return False
        with tag.open("rb") as fh:
            head = fh.read(512)
    except OSError:
        return False
    return CACHEDIR_SIGNATURE in head


class CacheAnalyzer:
    """Walks a bounded tree and reports *positively identified* caches.

    Scanning never leaves ``roots``. That is the BUG-01 fix: the analyzer is
    scope-bound by construction, so authorising root A cannot produce
    candidates from root B.
    """

    def __init__(
        self,
        policy: PolicyEngine,
        *,
        signatures: Sequence[Signature] = SAFE_SIGNATURES,
        optional_signatures: Sequence[Signature] = OPTIONAL_SIGNATURES,
        max_depth: int = 4,
        max_candidates: int = 200,
        max_files_per_candidate: int = 20_000,
    ) -> None:
        self.policy = policy
        self.signatures = tuple(signatures)
        self.optional_signatures = tuple(optional_signatures)
        self.max_depth = max_depth
        self.max_candidates = max_candidates
        self.max_files_per_candidate = max_files_per_candidate

    # -- classification ---------------------------------------------------- #

    def classify(
        self, path: str | os.PathLike[str], *, allow_optional: bool = False
    ) -> Signature | None:
        """Return the matching signature, or ``None`` if this is not a known cache."""

        p = self.policy.normalize(path)
        if not p.is_dir():
            return None
        for sig in self.signatures:
            if p.name.lower() != sig.name.lower():
                continue
            if sig.requires_tag and not has_valid_cachedir_tag(p):
                return None
            return sig
        if allow_optional:
            for sig in self.optional_signatures:
                if p.name.lower() == sig.name.lower():
                    return sig
        return None

    def is_cleanable(
        self, path: str | os.PathLike[str], *, allow_optional: bool = False
    ) -> bool:
        return self.classify(path, allow_optional=allow_optional) is not None

    # -- discovery --------------------------------------------------------- #

    def _files_for(self, directory: Path, sig: Signature) -> tuple[tuple[FileRef, ...], int]:
        refs, total = collect_files(directory, max_files=self.max_files_per_candidate)
        if sig.suffix_filter:
            refs = tuple(r for r in refs if Path(r.path).suffix.lower() == sig.suffix_filter)
            total = sum(r.size_bytes for r in refs)
        return refs, total

    def discover(
        self,
        roots: Sequence[str | os.PathLike[str]],
        *,
        allow_optional: bool = False,
    ) -> list[Candidate]:
        found: list[Candidate] = []
        seen: set[str] = set()

        for root in roots:
            base = self.policy.normalize(root)
            # A linked root would make every candidate below it belong to a
            # different directory than the one the caller authorised.
            if is_symlink_or_reparse(base):
                continue
            if not base.is_dir():
                continue
            for dirpath, dirnames, _ in os.walk(base, followlinks=False):
                rel_depth = len(Path(dirpath).relative_to(base).parts)
                if rel_depth >= self.max_depth:
                    dirnames[:] = []

                keep: list[str] = []
                for name in dirnames:
                    full = Path(dirpath) / name
                    if is_symlink_or_reparse(full):
                        continue

                    sig = self.classify(full, allow_optional=allow_optional)
                    if sig is None:
                        keep.append(name)
                        continue

                    key = os.path.normcase(str(full))
                    if key in seen:
                        continue
                    seen.add(key)

                    # Scope check before any inventory work: never measure a
                    # directory the caller did not authorise, and never accept
                    # one that is outside the currently scanned root.
                    if not is_within(full, base):
                        continue
                    if not self.policy.allows(full, required_root=base):
                        continue

                    files, total = self._files_for(full, sig)
                    if not files:
                        continue

                    found.append(
                        Candidate(
                            path=str(full),
                            category=sig.category,
                            risk=sig.risk,
                            size_bytes=total,
                            file_count=len(files),
                            reason=sig.reason,
                            source=f"signature:{sig.name}",
                            files=files,
                        )
                    )
                    if len(found) >= self.max_candidates:
                        return sorted(found, key=lambda c: (-c.size_bytes, c.path))
                dirnames[:] = keep

        found.sort(key=lambda c: (-c.size_bytes, c.path))
        return found
