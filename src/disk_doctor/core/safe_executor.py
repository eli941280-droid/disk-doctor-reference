"""Execution of a plan — the only module in the project that mutates the disk.

Invariants, checked in this order for every run:

1. the plan integrity digest matches its canonical payload;
2. the schema and policy versions are current (old plans are refused);
3. the plan has not expired;
4. ``plan.root`` is an authorised directory and is not itself a link;
5. a valid **approval receipt** is presented, bound to this exact plan id;
6. a recoverable disposal backend exists — otherwise nothing happens at all;
7. **every** file passes a full pre-flight before the first byte moves;
8. each file is re-validated (chain + identity) immediately before it is moved;
9. if the path chain changes mid-run, the remaining files are abandoned.

Two design notes that matter more than the code:

* ``execute(plan, receipt)`` has no ``confirm=True`` escape hatch. A boolean
  cannot express *which* file list was approved; a plan-bound receipt can.
* Free space is **measured**, not inferred from file sizes. Moving a file into
  the recycle bin does not free disk space, and the report must not pretend it
  does.
"""

from __future__ import annotations

import os
import secrets
import shutil
import tempfile
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

try:  # cross-process naming lock; absence is fail-closed
    import portalocker
except Exception:  # noqa: BLE001 - any import failure means "no lock backend"
    portalocker = None  # type: ignore[assignment]

from .audit_logger import AuditLogger
from .policy_engine import PolicyEngine, is_within
from .schemas import (
    POLICY_VERSION,
    ApprovalReceipt,
    CleanPlan,
    ExecutionResult,
    FileRef,
    ItemOutcome,
    ItemStatus,
)
from .utils import is_symlink_or_reparse, utc_now


class TrashUnavailable(RuntimeError):
    """Raised when no safe disposal mechanism exists."""


# One in-process mutex per quarantine directory. Needed on Windows, where
# opening a lock file whose byte range another *thread* of the same process just
# locked fails with EACCES before portalocker can even report "already locked".
_QUARANTINE_THREAD_LOCKS: dict[str, threading.Lock] = {}
_QUARANTINE_THREAD_GUARD = threading.Lock()


def _quarantine_thread_lock(key: str) -> threading.Lock:
    with _QUARANTINE_THREAD_GUARD:
        lock = _QUARANTINE_THREAD_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _QUARANTINE_THREAD_LOCKS[key] = lock
        return lock


@runtime_checkable
class TrashBackend(Protocol):
    """Anything that can move a path out of the way recoverably."""

    name: str

    def is_available(self) -> bool: ...

    def move(self, path: Path) -> None: ...


class Send2TrashBackend:
    """System recycle bin / trash via the ``send2trash`` package."""

    name = "send2trash"

    def is_available(self) -> bool:
        try:
            import send2trash  # noqa: F401
        except Exception:  # noqa: BLE001 - any import failure means "no trash"
            return False
        return True

    def move(self, path: Path) -> None:
        try:
            from send2trash import send2trash
        except Exception as exc:  # pragma: no cover - import guarded above
            raise TrashUnavailable(f"send2trash 不可用：{exc}") from exc
        send2trash(str(path))


class QuarantineBackend:
    """Moves files into a local quarantine directory.

    Never selected automatically: the caller has to ask for it explicitly
    (``--quarantine``).

    v0.2.2 fixes two separate defects here:

    * the write probe used a fixed ``.write_probe`` name (v0.2.0) and could
      destroy a user file that happened to be called that — it now uses
      ``mkstemp`` and only ever unlinks its own file;
    * ``while target.exists(): counter += 1`` followed by ``shutil.move`` is a
      check-then-act race: two runs can pick the same free name, and one
      silently overwrites the other. Names are now reserved with ``O_EXCL``
      under a cross-process lock, and the reservation is replaced atomically.

    Residual risk, stated rather than hidden: a process that does not use this
    backend can still write into the directory. The lock protects cooperating
    instances, not arbitrary third parties.
    """

    name = "quarantine"

    LOCK_SUFFIX = ".lock"
    PROBE_PREFIX = ".disk-doctor-probe-"
    COPY_PREFIX = ".disk-doctor-copy-"
    LOCK_TIMEOUT_SECONDS = 10.0

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        # Sibling lock file, deliberately outside the directory: everything
        # inside is content the user will see, and a lock file there would show
        # up in listings and break "how many files are quarantined" checks.
        self._lock_path = self.directory.with_name(self.directory.name + self.LOCK_SUFFIX)

    # -- availability ------------------------------------------------------ #

    def is_available(self) -> bool:
        if not self.directory.is_dir():
            return False
        try:
            fd, probe_name = tempfile.mkstemp(
                prefix=self.PROBE_PREFIX, dir=str(self.directory)
            )
        except OSError:
            return False
        probe = Path(probe_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write("ok")
            probe.unlink()
            return True
        except OSError:
            # Only ever remove the file this call created.
            try:
                probe.unlink()
            except OSError:
                pass
            return False

    # -- naming ------------------------------------------------------------ #

    @contextmanager
    def _exclusive(self):
        if portalocker is None:
            raise OSError("portalocker 不可用，无法保证隔离区命名的互斥")

        thread_lock = _quarantine_thread_lock(str(self._lock_path))
        if not thread_lock.acquire(timeout=self.LOCK_TIMEOUT_SECONDS):
            raise OSError("等待隔离区锁超时，已放弃移动")
        try:
            lock = portalocker.Lock(
                str(self._lock_path),
                mode="a+b",
                timeout=self.LOCK_TIMEOUT_SECONDS,
                flags=portalocker.LOCK_EX | portalocker.LOCK_NB,
            )
            with lock:
                yield
        finally:
            thread_lock.release()

    def _reserve(self, name: str) -> Path:
        """Create an empty, uniquely named placeholder. Never overwrites."""

        stem = Path(name).stem or "file"
        suffix = Path(name).suffix
        for _ in range(64):
            candidate = (
                self.directory
                / f"{stem}__{utc_now():%Y%m%dT%H%M%S}-{secrets.token_hex(4)}{suffix}"
            )
            try:
                fd = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                continue
            else:
                os.close(fd)
                return candidate
        raise OSError("无法为隔离区分配唯一文件名，已放弃移动")

    # -- moving ------------------------------------------------------------ #

    def move(self, path: Path) -> None:
        source = Path(path)

        # Only regular files. A directory or a link here would mean the plan
        # validation upstream is broken; refuse rather than guess.
        try:
            if is_symlink_or_reparse(source) or not source.is_file():
                raise OSError(f"隔离区只接受普通文件：{source}")
        except OSError as exc:
            raise OSError(f"无法确认文件类型：{exc}") from exc

        with self._exclusive():
            target = self._reserve(source.name)
            try:
                self._direct_replace(source, target)
            except OSError:
                self._copy_then_replace(source, target)

    @staticmethod
    def _direct_replace(source: Path, target: Path) -> None:
        """Atomic same-volume rename over our own empty reservation."""

        QuarantineBackend._replace_with_retry(source, target)

    @staticmethod
    def _replace_with_retry(source: Path, target: Path, *, attempts: int = 5) -> None:
        """``os.replace`` with a bounded retry for transient Windows locks.

        A freshly created file can be held open for a few milliseconds by an
        indexer or an anti-virus scanner, which surfaces as ``PermissionError``
        even though nobody asked for the file. Retrying is safe because
        ``os.replace`` is atomic: either the rename happened or it did not.
        """

        delay = 0.02
        for attempt in range(attempts):
            try:
                os.replace(source, target)
                return
            except PermissionError:
                if attempt == attempts - 1:
                    raise
                time.sleep(delay)
                delay *= 2

    def _copy_then_replace(self, source: Path, target: Path) -> None:
        """Cross-volume fallback: copy, fsync, atomic rename, then unlink."""

        fd, tmp_name = tempfile.mkstemp(
            prefix=self.COPY_PREFIX, dir=str(self.directory)
        )
        try:
            with os.fdopen(fd, "wb") as out, source.open("rb") as src:
                shutil.copyfileobj(src, out)
                out.flush()
                os.fsync(out.fileno())
            shutil.copystat(source, tmp_name)
            QuarantineBackend._replace_with_retry(Path(tmp_name), target)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            # Remove only the reservation this call created.
            target.unlink(missing_ok=True)
            raise

        try:
            self._remove_source(source)
        except OSError as exc:
            # The bytes are safe in quarantine and the source is untouched:
            # a partial success, reported as such instead of being hidden.
            raise OSError(
                f"已复制到隔离区但无法删除源文件（源文件未丢失）：{source}"
            ) from exc

    @staticmethod
    def _remove_source(source: Path) -> None:
        source.unlink()


class Approver(Protocol):
    """The slice of :class:`ConfirmationAuthority` the executor needs.

    Only ``verify_and_consume`` is required. There is deliberately no
    "verify now, consume later" pair in this interface: the two steps have to
    be atomic, or two concurrent runs can both pass verification.
    """

    def verify_and_consume(
        self, receipt: ApprovalReceipt | None, plan: CleanPlan, *, now: datetime | None = None
    ) -> None: ...


class _DenyAllApprover:
    """Default: no authority configured means no execution. Ever."""

    def verify_and_consume(self, receipt, plan, *, now=None):
        raise RuntimeError("no approval authority configured: execution refused")


class SafeExecutor:
    """Executes a :class:`CleanPlan` under a fixed set of safety invariants."""

    def __init__(
        self,
        policy: PolicyEngine,
        trash: TrashBackend,
        *,
        audit: AuditLogger | None = None,
        authority: Approver | None = None,
        cleanable: Callable[[str], bool] | None = None,
    ) -> None:
        self.policy = policy
        self.trash = trash
        self.audit = audit or AuditLogger()
        self.authority: Approver = authority or _DenyAllApprover()
        self.cleanable = cleanable

    # -- pre-flight -------------------------------------------------------- #

    def preflight(self, plan: CleanPlan) -> list[tuple[str, str]]:
        """Check every file against the plan root. Returns ``[(path, rule)]``.

        Runs before any move. An empty list means the whole plan is executable.
        """

        failures: list[tuple[str, str]] = []
        for item in plan.items:
            for ref in item.files:
                ok, rule = self._validate_file(plan, item.path, ref)
                if not ok:
                    failures.append((ref.path, rule))
        return failures

    def _validate_file(
        self, plan: CleanPlan, item_path: str, ref: FileRef
    ) -> tuple[bool, str]:
        path = Path(ref.path)

        if not is_within(ref.path, plan.root):
            return False, "outside_plan_root"
        if not is_within(ref.path, item_path):
            return False, "outside_item_dir"

        chain = self.policy.validate_path_chain(path, plan.root)
        if not chain.allowed:
            return False, chain.rule

        if self.cleanable is not None and not self.cleanable(item_path):
            return False, "not_cleanable"

        verdict = self.policy.verdict(path, required_root=plan.root)
        if not verdict.allowed:
            return False, verdict.rule

        ok, shape = self.policy.inspect_file(path)
        if not ok:
            return False, f"file_shape:{shape}"

        try:
            info = os.lstat(path)
        except OSError:
            return False, "missing"

        if info.st_size != ref.size_bytes:
            return False, "size_changed"
        if info.st_mtime_ns != ref.mtime_ns:
            return False, "mtime_changed"
        if info.st_ino != ref.inode or info.st_dev != ref.device:
            return False, "identity_changed"
        return True, "ok"

    # -- execution --------------------------------------------------------- #

    def execute(
        self,
        plan: CleanPlan,
        receipt: ApprovalReceipt | None,
        *,
        now: datetime | None = None,
    ) -> ExecutionResult:
        result = ExecutionResult(
            plan_id=plan.plan_id,
            estimated_bytes=plan.total_bytes,
        )
        self.audit.log(
            "plan_start",
            plan_id=plan.plan_id,
            items=len(plan.items),
            files=plan.total_files,
            estimated_bytes=plan.total_bytes,
        )

        # 1. integrity
        ok, reason = plan.verify_integrity()
        if not ok:
            return self._block(result, reason)

        # 2. policy drift
        if plan.policy_version != POLICY_VERSION:
            return self._block(result, "policy_version_mismatch")

        # 3. expiry
        if plan.is_expired(now):
            return self._block(result, "plan_expired")

        # 4. root
        root_verdict = self.policy.verdict(plan.root)
        if not root_verdict.allowed or not Path(plan.root).is_dir():
            return self._block(result, "plan_root_not_authorized")
        root_chain = self.policy.validate_path_chain(plan.root, plan.root)
        if not root_chain.allowed:
            return self._block(result, f"plan_root_{root_chain.rule}")

        # 5. approval — verified *and* burned before anything moves. A receipt
        #    that has been checked but not consumed is a replay waiting to happen.
        try:
            self.authority.verify_and_consume(receipt, plan, now=now)
        except Exception as exc:  # noqa: BLE001 - uniform refusal surface
            code = getattr(exc, "code", "approval_rejected")
            return self._block(result, f"approval:{code}")

        # 6. recoverable disposal only
        if not self.trash.is_available():
            return self._block(result, "trash_unavailable", failed=True)

        # 7. whole-plan pre-flight, before the first move
        failures = self.preflight(plan)
        if failures:
            self.audit.log(
                "preflight_failed", plan_id=plan.plan_id, count=len(failures),
                first=failures[0][1],
            )
            return self._block(result, f"preflight_failed:{failures[0][1]}")

        # 8. measured baseline
        result.free_space_before = self._free_space(plan.root)

        # 9. move
        aborted = False
        for item in plan.items:
            for idx, ref in enumerate(item.files, start=1):
                outcome_id = f"{item.item_id}#{idx}"
                if aborted:
                    result.outcomes.append(
                        ItemOutcome(
                            item_id=outcome_id,
                            path=ref.path,
                            status=ItemStatus.SKIPPED,
                            error="chain_changed: 执行中检测到路径变化，已停止剩余操作",
                        )
                    )
                    continue

                ok, rule = self._validate_file(plan, item.path, ref)
                if not ok:
                    result.outcomes.append(self._reject(outcome_id, ref, rule))
                    if rule in {
                        "symlink_in_path",
                        "reparse_in_path",
                        "resolved_outside_root",
                        "outside_plan_root",
                        "outside_item_dir",
                        "identity_changed",
                    }:
                        aborted = True
                    continue

                result.outcomes.append(self._move_one(outcome_id, ref))

        # 10. measured result
        result.free_space_after = self._free_space(plan.root)
        result.bytes_moved_to_trash = sum(
            o.bytes_moved for o in result.outcomes if o.status is ItemStatus.MOVED_TO_TRASH
        )
        result.finished_at = utc_now()

        self.audit.log(
            "plan_end",
            plan_id=plan.plan_id,
            bytes_moved=result.bytes_moved_to_trash,
            success=result.success_count,
            failed=result.failure_count,
            skipped=result.skipped_count,
        )
        return result

    # -- internals --------------------------------------------------------- #

    @staticmethod
    def _free_space(path: str | Path) -> int | None:
        try:
            return shutil.disk_usage(str(path)).free
        except OSError:
            return None

    def _block(
        self, result: ExecutionResult, reason: str, *, failed: bool = False
    ) -> ExecutionResult:
        result.blocked_reason = reason
        result.finished_at = utc_now()
        self.audit.log("plan_blocked", plan_id=result.plan_id, reason=reason)
        return result

    def _reject(self, item_id: str, ref: FileRef, rule: str) -> ItemOutcome:
        self.audit.log("file_skipped", item_id=item_id, path=ref.path, rule=rule)
        return ItemOutcome(
            item_id=item_id,
            path=ref.path,
            status=ItemStatus.SKIPPED,
            error=rule,
        )

    def _move_one(self, outcome_id: str, ref: FileRef) -> ItemOutcome:
        path = Path(ref.path)
        self.audit.log(
            "file_move_begin", item_id=outcome_id, path=str(path), bytes=ref.size_bytes
        )
        try:
            self.trash.move(path)
        except Exception as exc:  # noqa: BLE001 - one failure must not abort the batch
            self.audit.log("file_failed", item_id=outcome_id, path=str(path), error=str(exc))
            return ItemOutcome(
                item_id=outcome_id,
                path=str(path),
                status=ItemStatus.FAILED,
                error=str(exc) or exc.__class__.__name__,
            )

        self.audit.log("file_moved", item_id=outcome_id, path=str(path), bytes=ref.size_bytes)
        return ItemOutcome(
            item_id=outcome_id,
            path=str(path),
            status=ItemStatus.MOVED_TO_TRASH,
            bytes_moved=ref.size_bytes,
        )
