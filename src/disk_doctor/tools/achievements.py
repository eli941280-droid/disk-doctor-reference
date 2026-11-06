"""Milestone tracking.

Optional and side-effect free: it only reads and writes a small JSON file. If
the file is unwritable the agent keeps working — gamification must never be able
to block the safety path.

v0.2.1 wording: the numbers here are **files moved to the recycle bin**, not
space freed. A file sitting in the bin still occupies the disk, so calling this
"released space" was misleading (P1-1). The stored keys were renamed to match;
old files are read through a compatibility fallback.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ..core.utils import human_bytes, iso_now

GIB = 1024**3


@dataclass(frozen=True)
class Milestone:
    key: str
    title: str
    threshold_bytes: int


MILESTONES: tuple[Milestone, ...] = (
    Milestone("first_gb", "累计移入回收站 1 GB", 1 * GIB),
    Milestone("five_gb", "累计移入回收站 5 GB", 5 * GIB),
    Milestone("twenty_gb", "累计移入回收站 20 GB", 20 * GIB),
    Milestone("hundred_gb", "累计移入回收站 100 GB", 100 * GIB),
)


class AchievementStore:
    """Cumulative statistics for files moved to the recycle bin."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else None
        self._data = self._load()

    # -- persistence ------------------------------------------------------- #

    def _defaults(self) -> dict:
        return {
            "total_moved_bytes": 0,
            "total_files": 0,
            "runs": 0,
            "unlocked": [],
            "updated_at": None,
        }

    def _load(self) -> dict:
        if self.path is None or not self.path.exists():
            return self._defaults()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return self._defaults()

        base = self._defaults()
        # Compatibility: v0.2.0 stored the same quantity under an older key.
        if "total_moved_bytes" not in payload and "total_freed_bytes" in payload:
            payload["total_moved_bytes"] = payload["total_freed_bytes"]
        base.update({k: payload[k] for k in base if k in payload})
        return base

    def _save(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            # Statistics are a nicety; never let them break a real operation.
            pass

    # -- API --------------------------------------------------------------- #

    def record(self, moved_bytes: int, file_count: int = 0) -> list[str]:
        """Add a run and return titles newly unlocked by it."""

        self._data["total_moved_bytes"] += max(0, int(moved_bytes))
        self._data["total_files"] += max(0, int(file_count))
        self._data["runs"] += 1
        self._data["updated_at"] = iso_now()

        total = self._data["total_moved_bytes"]
        unlocked: list[str] = list(self._data["unlocked"])
        newly: list[str] = []
        for m in MILESTONES:
            if total >= m.threshold_bytes and m.key not in unlocked:
                unlocked.append(m.key)
                newly.append(m.title)
        self._data["unlocked"] = unlocked
        self._save()
        return newly

    def summary(self) -> dict:
        total = self._data["total_moved_bytes"]
        next_milestone = next(
            (m for m in MILESTONES if total < m.threshold_bytes), None
        )
        return {
            "total_moved_bytes": total,
            "total_moved_human": human_bytes(total),
            "total_files": self._data["total_files"],
            "runs": self._data["runs"],
            "unlocked": [m.title for m in MILESTONES if m.key in self._data["unlocked"]],
            "next_milestone": next_milestone.title if next_milestone else None,
            "next_milestone_remaining": (
                human_bytes(next_milestone.threshold_bytes - total)
                if next_milestone
                else None
            ),
        }
