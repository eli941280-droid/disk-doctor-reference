"""Plan construction, persistence, and approval.

Three responsibilities, deliberately kept in one module because they are one
lifecycle (the file allow-list for v0.2.1 does not admit new modules):

``PlanBuilder``
    Turns candidates into a sealed :class:`CleanPlan`. Rejects anything outside
    the requested scope, and anything the chain walk does not like.

``PlanStore``
    Persists a plan under ``<app-data>/plans/<plan_id>.json`` so that execution
    acts on the *same bytes* the user reviewed. No re-scanning, no regeneration.

``ConfirmationAuthority``
    Issues a one-time, plan-bound, HMAC-signed approval receipt — but only after
    the user types the complete plan id exactly. A bare "yes" is not an
    approval, because it cannot be tied to a file list.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
import threading
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

try:  # cross-process lock; absence is fail-closed, never "run unlocked"
    import portalocker
except Exception:  # noqa: BLE001 - any import failure means "no lock backend"
    portalocker = None  # type: ignore[assignment]

from .policy_engine import PolicyEngine, is_within
from .schemas import (
    APPROVAL_TTL_SECONDS,
    DEFAULT_PLAN_TTL_SECONDS,
    SCHEMA_VERSION,
    ApprovalReceipt,
    Candidate,
    CleanPlan,
    ExecutionState,
    FileRef,
    PlanItem,
    Rejection,
    RiskLevel,
    new_item_id,
)

_PLAN_ID_RE = re.compile(r"^[0-9a-f]{64}$")

_RISK_ORDER = {RiskLevel.SAFE: 0, RiskLevel.CAUTION: 1, RiskLevel.FORBIDDEN: 2}

# Backwards-compatible alias: older call sites imported this name.
_is_within = is_within


# --------------------------------------------------------------------------- #
# Builder
# --------------------------------------------------------------------------- #

class PlanBuilder:
    """Filters candidates through policy and produces a sorted, sealed plan."""

    def __init__(
        self,
        policy: PolicyEngine,
        *,
        max_items: int = 500,
        ttl_seconds: int = DEFAULT_PLAN_TTL_SECONDS,
    ) -> None:
        self.policy = policy
        self.max_items = max_items
        self.ttl_seconds = ttl_seconds

    def build(
        self,
        root: str,
        candidates: Iterable[Candidate],
        *,
        dry_run: bool | None = None,  # accepted for compatibility; has no effect
        notes: Sequence[str] | None = None,
    ) -> CleanPlan:
        """Build the plan.

        ``dry_run`` is accepted and ignored: a plan no longer records whether
        somebody intends to execute it. That flag used to change ``plan_id``
        between preview and execution, which is exactly what P0-3 fixes.
        """

        scope = self.policy.normalize(root)
        accepted: list[Candidate] = []
        rejections: list[Rejection] = []
        seen: set[str] = set()
        excluded_files = 0
        protected_files = 0

        for cand in candidates:
            key = os.path.normcase(str(self.policy.normalize(cand.path)))
            if key in seen:
                continue
            seen.add(key)

            if cand.risk is RiskLevel.FORBIDDEN:
                rejections.append(
                    Rejection(
                        path=cand.path,
                        category=cand.category,
                        rule="candidate_forbidden",
                        reason="候选本身被标记为禁止",
                    )
                )
                continue

            # Guard 1: never accept a candidate from outside the chosen scope.
            if not is_within(cand.path, scope):
                rejections.append(
                    Rejection(
                        path=cand.path,
                        category=cand.category,
                        rule="outside_scope_root",
                        reason=f"候选不在本次扫描范围 {scope} 之内",
                    )
                )
                continue

            verdict = self.policy.verdict(cand.path, required_root=scope)
            if not verdict.allowed:
                rejections.append(
                    Rejection(
                        path=cand.path,
                        category=cand.category,
                        rule=verdict.rule,
                        reason=verdict.reason,
                    )
                )
                continue

            # Guard 2: every file must also be inside scope and under policy.
            files: list[FileRef] = []
            for ref in cand.files:
                if not is_within(ref.path, scope) or not is_within(ref.path, cand.path):
                    excluded_files += 1
                    continue
                file_verdict = self.policy.verdict(ref.path, required_root=scope)
                if not file_verdict.allowed:
                    excluded_files += 1
                    if file_verdict.rule.startswith("protected"):
                        protected_files += 1
                    continue
                files.append(ref)

            if not files:
                rejections.append(
                    Rejection(
                        path=cand.path,
                        category=cand.category,
                        rule="no_eligible_files",
                        reason="目录内没有通过校验的普通文件",
                    )
                )
                continue

            accepted.append(
                cand.model_copy(
                    update={
                        "files": tuple(files),
                        "file_count": len(files),
                        "size_bytes": sum(f.size_bytes for f in files),
                    }
                )
            )

        accepted.sort(key=lambda c: (_RISK_ORDER[c.risk], -c.size_bytes, c.path))

        truncated = 0
        if len(accepted) > self.max_items:
            truncated = len(accepted) - self.max_items
            accepted = accepted[: self.max_items]

        items = [
            PlanItem(
                item_id=new_item_id(idx),
                path=str(self.policy.normalize(c.path)),
                category=c.category,
                risk=c.risk,
                size_bytes=sum(f.size_bytes for f in c.files),
                file_count=len(c.files),
                reason=c.reason,
                rule="allow",
                files=c.files,
            )
            for idx, c in enumerate(accepted, start=1)
        ]

        plan_notes = list(notes or [])
        if truncated:
            plan_notes.append(f"候选过多，已截断 {truncated} 项（上限 {self.max_items}）")
        if rejections:
            plan_notes.append(f"{len(rejections)} 项被安全策略拦截，未纳入计划")

        plan = CleanPlan(
            root=str(scope),
            items=items,
            rejections=rejections,
            notes=plan_notes,
            ttl_seconds=self.ttl_seconds,
            excluded_files=excluded_files,
            protected_files=protected_files,
        )
        return plan.seal()


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #

class PlanStoreError(RuntimeError):
    """Raised for any refusal to load or store a plan."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


class PlanStore:
    """Content-addressed store for sealed plans, plus an execution ledger.

    The file name is derived *only* from the validated ``plan_id``; a caller can
    never point the loader at an arbitrary path.

    Layout under the application data directory::

        plans/<plan_id>.json          the sealed plan (never mutated)
        plans/<plan_id>.consumed      "has been claimed at least once"
        plans/state/<plan_id>.state   READY / IN_PROGRESS / COMPLETED / ...
        plans/locks/<plan_id>.lock    cross-process lock (never deleted)

    Two processes can hold the same plan id at the same time; only one can hold
    the lock. The lock is taken *before* the plan is re-read and the state is
    flipped to ``IN_PROGRESS`` *before* the first file moves, so a second
    executor cannot pass the same checks in the window between them.
    """

    def __init__(self, directory: str | os.PathLike[str], *, ttl_seconds: int | None = None):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.ttl_seconds = ttl_seconds
        self._lock_dir = self.directory / "locks"
        self._state_dir = self.directory / "state"
        self._lock_dir.mkdir(parents=True, exist_ok=True)
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._thread_locks: dict[str, threading.Lock] = {}
        self._thread_guard = threading.Lock()

    # -- helpers ----------------------------------------------------------- #

    @staticmethod
    def validate_id(plan_id: str) -> str:
        if not isinstance(plan_id, str) or not _PLAN_ID_RE.match(plan_id):
            raise PlanStoreError("invalid_plan_id", "必须是 64 位小写十六进制")
        return plan_id

    def path_for(self, plan_id: str) -> Path:
        return self.directory / f"{self.validate_id(plan_id)}.json"

    def _consumed_marker(self, plan_id: str) -> Path:
        return self.directory / f"{self.validate_id(plan_id)}.consumed"

    def state_path(self, plan_id: str) -> Path:
        return self._state_dir / f"{self.validate_id(plan_id)}.state"

    # -- execution state --------------------------------------------------- #

    def read_state(self, plan_id: str) -> ExecutionState:
        """Current lifecycle state. A missing file means ``READY``.

        Anything unreadable or unrecognised is reported as ``INTERRUPTED``:
        guessing "ready" from a corrupt marker is how a half-finished run gets
        replayed.
        """

        path = self.state_path(plan_id)
        try:
            text = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return ExecutionState.READY
        except OSError as exc:
            raise PlanStoreError("state_unreadable", str(exc)) from exc
        try:
            return ExecutionState(text)
        except ValueError:
            return ExecutionState.INTERRUPTED

    def _write_state(self, plan_id: str, state: ExecutionState) -> None:
        self._state_dir.mkdir(parents=True, exist_ok=True)
        target = self.state_path(plan_id)
        fd, tmp_name = tempfile.mkstemp(
            prefix=".state-", suffix=".tmp", dir=str(self._state_dir)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(state.value)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, target)
        except Exception:
            Path(tmp_name).unlink(missing_ok=True)
            raise

    def reconcile_interrupted(self) -> list[str]:
        """Mark orphaned ``IN_PROGRESS`` plans as ``INTERRUPTED``.

        A plan is orphaned when its state says ``IN_PROGRESS`` but nobody holds
        its lock — the process that claimed it died. Returns the plan ids that
        were reclassified.

        The cheap pre-check below is only an optimisation: it decides which
        plans are worth locking. **The decision is taken under the lock, on a
        fresh read.** v0.2.2 read the state, released nothing (there was no lock
        yet), checked whether the lock *looked* free, and then wrote — so a
        process that was still alive could finish in that window, write
        ``COMPLETED``, and have it overwritten with ``INTERRUPTED``.
        """

        if portalocker is None or not self._state_dir.is_dir():
            return []

        recovered: list[str] = []
        for path in sorted(self._state_dir.glob("*.state")):
            plan_id = path.stem
            if not _PLAN_ID_RE.match(plan_id):
                continue
            try:
                if self.read_state(plan_id) is not ExecutionState.IN_PROGRESS:
                    continue
            except PlanStoreError:
                continue
            if self._recover_if_orphaned(plan_id):
                recovered.append(plan_id)
        return recovered

    def _recover_if_orphaned(self, plan_id: str) -> bool:
        """Lock, re-read, then decide. Never writes without holding the lock.

        Lock order is identical to :meth:`claim_for_execution` — thread lock
        first, then the file lock — so the two can never deadlock against each
        other. Both acquisitions are non-blocking: if anything holds the plan,
        this returns ``False`` and touches nothing.
        """

        thread_lock = self._thread_lock(plan_id)
        if not thread_lock.acquire(blocking=False):
            return False
        try:
            try:
                handle = self._acquire_file_lock(plan_id)
            except PlanStoreError:
                # Someone is alive and owns this plan. Leave it alone.
                return False
            try:
                # Re-read *under* the lock: the state may have moved on between
                # the optimistic pre-check and now.
                if self.read_state(plan_id) is not ExecutionState.IN_PROGRESS:
                    return False
                self._write_state(plan_id, ExecutionState.INTERRUPTED)
                return True
            finally:
                self._release_file_lock(handle)
        finally:
            thread_lock.release()

    # -- locking ----------------------------------------------------------- #

    def _thread_lock(self, plan_id: str) -> threading.Lock:
        with self._thread_guard:
            lock = self._thread_locks.get(plan_id)
            if lock is None:
                lock = threading.Lock()
                self._thread_locks[plan_id] = lock
            return lock

    def _acquire_file_lock(self, plan_id: str):
        if portalocker is None:
            raise PlanStoreError(
                "lock_backend_unavailable",
                "未安装 portalocker，无法保证跨进程互斥，拒绝执行",
            )
        self._lock_dir.mkdir(parents=True, exist_ok=True)
        path = self._lock_dir / f"{self.validate_id(plan_id)}.lock"
        try:
            # On Windows a second handle to a byte-range-locked file can fail
            # right here, before portalocker gets a chance to say "already
            # locked". Treat it as contention, not as a crash.
            handle = open(path, "a+b")
        except OSError as exc:
            raise PlanStoreError(
                "plan_locked", f"无法打开计划锁文件（可能正在被执行）：{exc}"
            ) from exc
        try:
            portalocker.lock(handle, portalocker.LOCK_EX | portalocker.LOCK_NB)
        except Exception as exc:
            handle.close()
            raise PlanStoreError(
                "plan_locked", f"该计划正在被另一个进程执行（{exc.__class__.__name__}）"
            ) from exc
        return handle

    def _release_file_lock(self, handle) -> None:
        if handle is None:
            return
        try:
            if portalocker is not None:
                portalocker.unlock(handle)
        except Exception:  # noqa: BLE001 - closing releases the lock anyway
            pass
        finally:
            handle.close()
        # The lock file is deliberately left in place. Deleting it would let two
        # processes lock two different inodes that share one path.

    @contextmanager
    def claim_for_execution(
        self, plan_id: str, *, now: datetime | None = None
    ) -> Iterator[PlanClaim]:
        """Take exclusive, one-shot ownership of a plan.

        Ordering matters and is the point of this method:

        lock → re-load → refuse if already started → persist ``IN_PROGRESS``
        → hand the plan to the caller. Only then may a byte move.
        """

        self.validate_id(plan_id)

        # A plan left IN_PROGRESS by a dead process has no live owner. Mark it
        # first, then refuse: "we do not know how far it got" is not a state a
        # user should be able to resume by clicking again.
        if plan_id in self.reconcile_interrupted():
            raise PlanStoreError(
                "plan_interrupted",
                "该计划上次执行中断，状态不可知，已标记为 INTERRUPTED；"
                "请重新扫描并生成新计划",
            )

        thread_lock = self._thread_lock(plan_id)
        if not thread_lock.acquire(blocking=False):
            raise PlanStoreError("plan_locked", "该计划正在被本进程内的另一处执行")

        handle = None
        try:
            handle = self._acquire_file_lock(plan_id)

            plan = self.load(plan_id, now=now)

            state = self.read_state(plan_id)
            if state is ExecutionState.IN_PROGRESS:
                # We hold the lock, so the previous owner is gone: crash recovery.
                self._write_state(plan_id, ExecutionState.INTERRUPTED)
                raise PlanStoreError(
                    "plan_interrupted",
                    "该计划上次执行中断，状态不可知，已标记为 INTERRUPTED；"
                    "请重新扫描并生成新计划",
                )
            if state.is_terminal:
                raise PlanStoreError(
                    "plan_consumed", f"计划状态为 {state.value}，不能再次执行"
                )

            self._write_state(plan_id, ExecutionState.IN_PROGRESS)
            self.mark_consumed(plan_id)

            claim = PlanClaim(self, plan)
            try:
                yield claim
            except BaseException:
                if not claim.finished:
                    self._write_state(plan_id, ExecutionState.FAILED)
                raise
            else:
                if not claim.finished:
                    self._write_state(plan_id, ExecutionState.COMPLETED)
        finally:
            self._release_file_lock(handle)
            thread_lock.release()

    # -- write ------------------------------------------------------------- #

    def save(self, plan: CleanPlan) -> str:
        plan_id = self.validate_id(plan.plan_id)
        ok, reason = plan.verify_integrity()
        if not ok:
            raise PlanStoreError("plan_not_sealed", reason)

        payload = plan.model_dump(mode="json")
        target = self.path_for(plan_id)
        fd, tmp_name = tempfile.mkstemp(
            prefix=".plan-", suffix=".tmp", dir=str(self.directory)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                json.dump(payload, fh, ensure_ascii=False, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, target)
        except Exception:
            Path(tmp_name).unlink(missing_ok=True)
            raise
        return plan_id

    # -- read -------------------------------------------------------------- #

    def load(self, plan_id: str, *, now: datetime | None = None) -> CleanPlan:
        self.validate_id(plan_id)

        if self.is_consumed(plan_id):
            raise PlanStoreError("plan_consumed", "该计划已执行过，不能重复使用")

        path = self.path_for(plan_id)
        if not path.exists():
            raise PlanStoreError("plan_not_found", str(path))

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PlanStoreError("plan_unreadable", str(exc)) from exc

        try:
            plan = CleanPlan.model_validate(data)
        except Exception as exc:
            raise PlanStoreError("plan_invalid", str(exc)) from exc

        if plan.schema_version != SCHEMA_VERSION:
            raise PlanStoreError(
                "plan_schema_outdated",
                f"存储版本 {plan.schema_version}，当前 {SCHEMA_VERSION}；请重新生成计划",
            )

        ok, reason = plan.verify_integrity()
        if not ok:
            raise PlanStoreError(reason, "存储的计划内容与摘要不一致")

        if plan.is_expired(now):
            raise PlanStoreError("plan_expired", "计划已过期，请重新生成")

        return plan

    # -- consumption ------------------------------------------------------- #

    def mark_consumed(self, plan_id: str) -> None:
        marker = self._consumed_marker(plan_id)
        marker.write_text(datetime.now(UTC).isoformat(), encoding="utf-8")

    def is_consumed(self, plan_id: str) -> bool:
        return self._consumed_marker(plan_id).exists()


class PlanClaim:
    """Handle yielded by :meth:`PlanStore.claim_for_execution`.

    The caller decides the terminal state: a run that moved nothing because the
    pre-flight failed ends ``FAILED``, not ``COMPLETED``. Either way the plan is
    finished — there is no path back to ``READY``.
    """

    def __init__(self, store: PlanStore, plan: CleanPlan) -> None:
        self._store = store
        self.plan = plan
        self.finished = False

    @property
    def plan_id(self) -> str:
        return self.plan.plan_id

    def finish(self, state: ExecutionState = ExecutionState.COMPLETED) -> None:
        if state not in {ExecutionState.COMPLETED, ExecutionState.FAILED}:
            raise ValueError("执行只能以 COMPLETED 或 FAILED 结束")
        if self.finished:
            return
        self._store._write_state(self.plan.plan_id, state)
        self.finished = True


# --------------------------------------------------------------------------- #
# Approval
# --------------------------------------------------------------------------- #

class ApprovalError(RuntimeError):
    """Raised when an approval token or receipt is not acceptable."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


class ConfirmationAuthority:
    """Issues and validates one-time approval receipts.

    The user must type the complete plan id. ``EXECUTE <id>`` binds the approval
    to one exact file list; "yes" does not, and is therefore not accepted.
    """

    def __init__(
        self,
        secret: bytes | None = None,
        *,
        ttl_seconds: int = APPROVAL_TTL_SECONDS,
    ) -> None:
        self._secret = secret or secrets.token_bytes(32)
        self.ttl_seconds = ttl_seconds
        self._used_nonces: set[str] = set()
        self._lock = threading.Lock()

    # -- token helpers ----------------------------------------------------- #

    @staticmethod
    def execute_token(plan_id: str) -> str:
        return f"EXECUTE {plan_id}"

    @staticmethod
    def second_token(plan_id: str) -> str:
        return f"SECOND {plan_id}"

    # -- signing ----------------------------------------------------------- #

    def _sign(self, receipt: ApprovalReceipt) -> str:
        message = "|".join(
            [
                receipt.plan_id,
                receipt.nonce,
                receipt.issued_at.isoformat(),
                receipt.expires_at.isoformat(),
                "1" if receipt.second_confirmed else "0",
            ]
        ).encode("utf-8")
        return hmac.new(self._secret, message, hashlib.sha256).hexdigest()

    # -- issue ------------------------------------------------------------- #

    def issue(
        self,
        plan: CleanPlan,
        *,
        confirmation: str,
        second_confirmation: str | None = None,
        now: datetime | None = None,
    ) -> ApprovalReceipt:
        if confirmation != self.execute_token(plan.plan_id):
            raise ApprovalError(
                "confirmation_mismatch",
                "必须准确输入 EXECUTE 加完整 64 位 plan_id",
            )

        second_ok = False
        if plan.requires_second_confirm:
            if second_confirmation != self.second_token(plan.plan_id):
                raise ApprovalError(
                    "second_confirmation_required",
                    "大计划需要准确输入 SECOND 加完整 plan_id",
                )
            second_ok = True

        moment = now or datetime.now(UTC)
        receipt = ApprovalReceipt(
            plan_id=plan.plan_id,
            nonce=secrets.token_hex(16),
            issued_at=moment,
            expires_at=moment + timedelta(seconds=self.ttl_seconds),
            second_confirmed=second_ok,
        )
        return receipt.model_copy(update={"signature": self._sign(receipt)})

    # -- verify ------------------------------------------------------------ #

    def verify(
        self,
        receipt: ApprovalReceipt | None,
        plan: CleanPlan,
        *,
        now: datetime | None = None,
    ) -> None:
        if receipt is None:
            raise ApprovalError("approval_missing", "缺少审批凭证")
        if receipt.plan_id != plan.plan_id:
            raise ApprovalError("approval_plan_mismatch", "凭证与当前计划不匹配")
        if not hmac.compare_digest(receipt.signature, self._sign(receipt)):
            raise ApprovalError("approval_signature_invalid", "凭证签名无效")

        moment = now or datetime.now(UTC)
        if moment > receipt.expires_at:
            raise ApprovalError("approval_expired", "审批凭证已过期")

        if receipt.nonce in self._used_nonces:
            raise ApprovalError("approval_already_used", "凭证已被使用")

        if plan.requires_second_confirm and not receipt.second_confirmed:
            raise ApprovalError("second_confirmation_missing", "缺少二次确认")

    def verify_and_consume(
        self,
        receipt: ApprovalReceipt | None,
        plan: CleanPlan,
        *,
        now: datetime | None = None,
    ) -> None:
        """Check the receipt and burn its nonce in one critical section.

        Split ``verify()`` / ``consume()`` calls leave a window: two executors
        can both verify before either consumes. Doing both under the lock means
        the second caller sees ``approval_already_used`` and never reaches the
        file system.
        """

        with self._lock:
            self.verify(receipt, plan, now=now)
            if receipt is None:  # pragma: no cover - verify() already raised
                raise ApprovalError("approval_missing", "缺少审批凭证")
            self._used_nonces.add(receipt.nonce)

    def is_consumed(self, nonce: str) -> bool:
        with self._lock:
            return nonce in self._used_nonces

    def consume(self, receipt: ApprovalReceipt) -> None:
        with self._lock:
            self._used_nonces.add(receipt.nonce)
