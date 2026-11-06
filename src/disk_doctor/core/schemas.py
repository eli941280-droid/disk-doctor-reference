"""Typed data models shared across the agent.

v0.2.1 changes, driven by the audit in ``docs/SECURITY_AUDIT_v0.2.0.md``:

* ``CleanPlan`` identifies *what to do*, never *whether to do it*. The
  ``dry_run`` field is gone, so a plan's identity cannot change between preview
  and execution (P0-3);
* ``SCHEMA_VERSION`` is bumped, which invalidates every plan persisted by
  v0.2.0 — old plans are not migrated;
* execution reports three distinct quantities: estimated bytes, bytes actually
  moved to trash, and the measured change in free space (P1-1).

A digest is an integrity check, not a signature. It detects accidental or naive
edits; it does **not** stop a local attacker who rewrites the plan and
recomputes the hash.
"""

from __future__ import annotations

import enum
import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, computed_field

SCHEMA_VERSION = "4"
POLICY_VERSION = "2"
DEFAULT_PLAN_TTL_SECONDS = 900  # 15 minutes
APPROVAL_TTL_SECONDS = 300  # 5 minutes


# --------------------------------------------------------------------------- #
# Enumerations
# --------------------------------------------------------------------------- #

class ExecutionState(str, enum.Enum):
    """Lifecycle of a stored plan.

    Persisted *beside* the plan, never inside its canonical payload: the plan's
    digest must identify a file list, not a progress marker. Allowed transitions
    are ``READY → IN_PROGRESS → COMPLETED | FAILED``. ``INTERRUPTED`` is what a
    run that died mid-flight becomes, and it is terminal — an interrupted plan
    is never resumed, because nobody can say how far it got.
    """

    READY = "ready"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"

    @property
    def is_terminal(self) -> bool:
        return self in {
            ExecutionState.COMPLETED,
            ExecutionState.FAILED,
            ExecutionState.INTERRUPTED,
        }

class RiskLevel(str, enum.Enum):
    """How dangerous it is to touch a candidate path."""

    SAFE = "safe"
    CAUTION = "caution"
    FORBIDDEN = "forbidden"

    @property
    def label(self) -> str:
        return {"safe": "安全", "caution": "注意", "forbidden": "禁止"}[self.value]

    @property
    def marker(self) -> str:
        return {"safe": "🟢", "caution": "🟡", "forbidden": "🔴"}[self.value]


class Intent(str, enum.Enum):
    """Internal task classification produced by the rule router."""

    DIAGNOSE = "diagnose"
    CLEAN_QUICK = "clean_quick"
    CLEAN_DEEP = "clean_deep"
    FIND_LARGE = "find_large"
    FIND_DUPLICATE = "find_duplicate"
    DEV_CLEAN = "dev_clean"
    TRASH_INFO = "trash_info"
    ACHIEVEMENTS = "achievements"
    OUT_OF_SCOPE = "out_of_scope"
    UNKNOWN = "unknown"

    @property
    def is_cleanup(self) -> bool:
        return self in {Intent.CLEAN_QUICK, Intent.CLEAN_DEEP, Intent.DEV_CLEAN}


class ItemStatus(str, enum.Enum):
    """Terminal state of a single planned file."""

    MOVED_TO_TRASH = "moved_to_trash"
    SKIPPED = "skipped"
    FAILED = "failed"


class OutcomeStatus(str, enum.Enum):
    COMPLETED = "completed"
    PARTIAL = "partial"
    BLOCKED = "blocked"
    FAILED = "failed"


class Action(str, enum.Enum):
    MOVE_TO_TRASH = "move_to_trash"

    @property
    def label(self) -> str:
        return {"move_to_trash": "移入回收站"}[self.value]


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #

class DiskVolume(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: str
    total_bytes: int = Field(ge=0)
    used_bytes: int = Field(ge=0)
    free_bytes: int = Field(ge=0)
    percent_used: float = Field(ge=0.0, le=100.0)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def percent_free(self) -> float:
        return round(100.0 - self.percent_used, 1)


class FileEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: str
    size_bytes: int = Field(ge=0)
    mtime: float | None = None


class FileRef(BaseModel):
    """Identity of one file at plan time.

    ``device`` + ``inode`` + ``mtime_ns`` + ``size_bytes`` lets the executor
    notice that the file it is about to move is not the file that was reviewed.
    On Windows ``st_ino`` is the file index and ``st_dev`` the volume serial.
    """

    model_config = ConfigDict(frozen=True)

    path: str
    size_bytes: int = Field(ge=0)
    mtime_ns: int = Field(ge=0)
    device: int = Field(ge=0)
    inode: int = Field(ge=0)


class DuplicateGroup(BaseModel):
    model_config = ConfigDict(frozen=True)

    digest: str
    size_bytes: int = Field(ge=0)
    paths: tuple[str, ...]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def wasted_bytes(self) -> int:
        return self.size_bytes * max(0, len(self.paths) - 1)


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #

class Candidate(BaseModel):
    """A directory proposed for cleanup, with the exact files it contains."""

    model_config = ConfigDict(frozen=True)

    path: str
    category: str
    risk: RiskLevel = RiskLevel.CAUTION
    size_bytes: int = Field(ge=0)
    file_count: int = Field(default=0, ge=0)
    reason: str = ""
    source: str = "scan"
    files: tuple[FileRef, ...] = ()


class PolicyVerdict(BaseModel):
    model_config = ConfigDict(frozen=True)

    allowed: bool
    risk: RiskLevel
    rule: str
    reason: str


class ChainVerdict(BaseModel):
    """Result of walking every component between a required root and a target."""

    model_config = ConfigDict(frozen=True)

    allowed: bool
    rule: str
    reason: str
    resolved_target: str = ""
    resolved_root: str = ""


class PlanItem(BaseModel):
    """A cache directory that survived policy evaluation, plus its files."""

    model_config = ConfigDict(frozen=True)

    item_id: str
    path: str
    category: str
    risk: RiskLevel
    size_bytes: int = Field(ge=0)
    file_count: int = Field(default=0, ge=0)
    reason: str = ""
    rule: str = ""
    action: Action = Action.MOVE_TO_TRASH
    files: tuple[FileRef, ...] = ()


class Rejection(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: str
    category: str
    rule: str
    reason: str


class CleanPlan(BaseModel):
    """What to move. It says nothing about whether to move it.

    Execution authority lives in :class:`ApprovalReceipt`. Keeping the two
    separate is what makes a plan's identity stable across preview and execute.
    """

    schema_version: str = SCHEMA_VERSION
    policy_version: str = POLICY_VERSION
    plan_id: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    ttl_seconds: int = DEFAULT_PLAN_TTL_SECONDS
    root: str
    items: list[PlanItem] = Field(default_factory=list)
    rejections: list[Rejection] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    # Diagnostics only. Deliberately excluded from the canonical payload: they
    # are derived counters used by the preview, not part of what gets approved.
    excluded_files: int = 0
    protected_files: int = 0

    # -- derived totals ---------------------------------------------------- #

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_bytes(self) -> int:
        return sum(f.size_bytes for i in self.items for f in i.files)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_files(self) -> int:
        return sum(len(i.files) for i in self.items)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def requires_second_confirm(self) -> bool:
        """Structural threshold. The numbers are inherited from a self-report.

        See ``docs/known_unknowns.md`` S6: an inherited heuristic, not a
        verified behaviour of the original product.
        """
        return self.total_files > 50 or self.total_bytes > 1024**3

    # -- integrity --------------------------------------------------------- #

    def canonical_payload(self) -> dict[str, Any]:
        """Deterministic projection used for the digest.

        Keys are sorted when dumped, so the digest does not depend on dict
        insertion order. Execution state is deliberately absent.
        """

        return {
            "schema_version": self.schema_version,
            "policy_version": self.policy_version,
            "created_at": self.created_at.isoformat(),
            "ttl_seconds": self.ttl_seconds,
            "root": self.root,
            "total_bytes": self.total_bytes,
            "total_files": self.total_files,
            "requires_second_confirm": self.requires_second_confirm,
            "items": [
                {
                    "item_id": i.item_id,
                    "path": i.path,
                    "category": i.category,
                    "risk": i.risk.value,
                    "action": i.action.value,
                    "size_bytes": i.size_bytes,
                    "file_count": i.file_count,
                    "files": [
                        {
                            "path": f.path,
                            "size_bytes": f.size_bytes,
                            "mtime_ns": f.mtime_ns,
                            "device": f.device,
                            "inode": f.inode,
                        }
                        for f in i.files
                    ],
                }
                for i in self.items
            ],
        }

    def canonical_json(self) -> str:
        return json.dumps(
            self.canonical_payload(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )

    def compute_plan_id(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    def seal(self) -> CleanPlan:
        """Assign the digest. Called once, by the builder or the store."""

        self.plan_id = self.compute_plan_id()
        return self

    def verify_integrity(self) -> tuple[bool, str]:
        if not self.plan_id:
            return False, "plan_unsigned"
        if self.schema_version != SCHEMA_VERSION:
            return False, "plan_schema_outdated"
        if self.plan_id != self.compute_plan_id():
            return False, "plan_digest_mismatch"
        return True, "ok"

    # -- expiry ------------------------------------------------------------ #

    def is_expired(self, now: datetime | None = None) -> bool:
        moment = now or datetime.now(UTC)
        return moment - self.created_at > timedelta(seconds=self.ttl_seconds)

    def expires_at(self) -> datetime:
        return self.created_at + timedelta(seconds=self.ttl_seconds)

    def all_files(self) -> list[FileRef]:
        return [f for i in self.items for f in i.files]

    def item_by_id(self, item_id: str) -> PlanItem | None:
        return next((i for i in self.items if i.item_id == item_id), None)


# --------------------------------------------------------------------------- #
# Approval
# --------------------------------------------------------------------------- #

class ApprovalReceipt(BaseModel):
    """A one-time, plan-bound credential issued after exact confirmation.

    The HMAC only protects against accidental or naive forgery inside the
    application. It is **not** a defence against an attacker who can execute
    code in the same process.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    plan_id: str
    nonce: str
    issued_at: datetime
    expires_at: datetime
    second_confirmed: bool = False
    signature: str = ""


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #

class ItemOutcome(BaseModel):
    """What actually happened to one planned file."""

    item_id: str
    path: str
    status: ItemStatus
    bytes_moved: int = Field(default=0, ge=0)
    error: str | None = None


class ExecutionResult(BaseModel):
    """Measured facts about one run. Nothing here is derived from the plan."""

    plan_id: str
    mode: str = "execute"
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
    outcomes: list[ItemOutcome] = Field(default_factory=list)

    # Three distinct quantities. Conflating them is the bug this schema exists
    # to prevent: moving a file into the recycle bin does not free disk space.
    estimated_bytes: int = Field(default=0, ge=0)
    bytes_moved_to_trash: int = Field(default=0, ge=0)
    free_space_before: int | None = None
    free_space_after: int | None = None

    blocked_reason: str | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def available_space_change_bytes(self) -> int | None:
        if self.free_space_before is None or self.free_space_after is None:
            return None
        return self.free_space_after - self.free_space_before

    @computed_field  # type: ignore[prop-decorator]
    @property
    def success_count(self) -> int:
        return sum(1 for o in self.outcomes if o.status is ItemStatus.MOVED_TO_TRASH)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def failure_count(self) -> int:
        return sum(1 for o in self.outcomes if o.status is ItemStatus.FAILED)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def skipped_count(self) -> int:
        return sum(1 for o in self.outcomes if o.status is ItemStatus.SKIPPED)

    def status(self) -> OutcomeStatus:
        if self.blocked_reason:
            return OutcomeStatus.BLOCKED
        if not self.outcomes:
            return OutcomeStatus.FAILED
        if self.failure_count == 0 and self.skipped_count == 0:
            return OutcomeStatus.COMPLETED
        if self.success_count > 0:
            return OutcomeStatus.PARTIAL
        return OutcomeStatus.FAILED


class Verification(BaseModel):
    """Reconciliation across the three quantities. Never invents a fourth."""

    estimated_bytes: int = Field(ge=0)
    bytes_moved_to_trash: int = Field(ge=0)
    available_space_change_bytes: int | None = None
    ok: bool = True
    notes: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Diagnosis & reporting
# --------------------------------------------------------------------------- #

class Finding(BaseModel):
    model_config = ConfigDict(frozen=True)

    level: str
    title: str
    detail: str = ""


class Recommendation(BaseModel):
    model_config = ConfigDict(frozen=True)

    action: str
    impact: str
    risk: RiskLevel = RiskLevel.SAFE


class Diagnosis(BaseModel):
    volume: DiskVolume
    health_score: int = Field(ge=0, le=100)
    findings: list[Finding] = Field(default_factory=list)
    recommendations: list[Recommendation] = Field(default_factory=list)
    largest_directories: list[FileEntry] = Field(default_factory=list)
    largest_files: list[FileEntry] = Field(default_factory=list)
    days_until_full: float | None = None
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class AgentTurn(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    intent: Intent
    message: str
    data: dict[str, Any] = Field(default_factory=dict)
    awaiting_confirmation: bool = False
    plan_id: str | None = None
    llm_used: str = "mock"


def new_item_id(index: int) -> str:
    return f"I{index:03d}-{uuid.uuid5(uuid.NAMESPACE_OID, str(index)).hex[:6]}"
