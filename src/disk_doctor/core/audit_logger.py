"""Append-only audit trail.

Every mutation is written before it happens and again after, so a crash leaves
evidence of intent rather than a silent gap. The log is plain JSONL: it can be
read with ``tail``, diffed, or replayed, and it never contains file contents.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .utils import iso_now


class AuditLogger:
    """Writes structured events to a JSONL file and keeps them in memory."""

    def __init__(self, path: str | Path | None = None, *, echo: bool = False) -> None:
        self.path = Path(path) if path else None
        self._events: list[dict[str, Any]] = []
        self._echo = echo
        self._prepared = False

    def _prepare(self) -> None:
        # Directory creation is deferred to the first write so that read-only
        # commands never touch the user's home directory.
        if self._prepared or self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._prepared = True

    def log(self, event: str, **fields: Any) -> dict[str, Any]:
        record: dict[str, Any] = {"ts": iso_now(), "event": event}
        record.update({k: v for k, v in fields.items() if v is not None})
        self._events.append(record)

        if self.path is not None:
            self._prepare()
            with self.path.open("a", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        if self._echo:
            print(f"[audit] {record}")
        return record

    @property
    def events(self) -> list[dict[str, Any]]:
        return list(self._events)

    def of_event(self, name: str) -> list[dict[str, Any]]:
        return [e for e in self._events if e.get("event") == name]
