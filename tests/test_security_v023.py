"""v0.2.3 release-blocker suite.

Two fixes, both about *where* a check happens rather than whether it exists:

A. ``reconcile_interrupted`` must decide under the plan lock.
   v0.2.2 read the state, asked whether the lock looked free, released it, and
   then wrote. A live executor could finish inside that window and have its
   ``COMPLETED`` overwritten with ``INTERRUPTED``.

B. A scan root that is itself a symlink / reparse point must be refused.
   Skipping linked *entries* was already covered; a linked *root* made every
   measurement describe a directory the caller never named.

Every filesystem operation happens under ``tmp_path``. Where a native Windows
symlink cannot be created, the branch is exercised by marking the path as a
reparse point and the test says so in the assertion message.
"""

from __future__ import annotations

import pathlib

import pytest

from disk_doctor.core import clean_plan as clean_plan_module
from disk_doctor.core import utils as path_utils
from disk_doctor.core.clean_plan import PlanStore
from disk_doctor.core.policy_engine import PolicyEngine
from disk_doctor.core.schemas import (
    CleanPlan,
    ExecutionState,
    PlanItem,
    RiskLevel,
)
from disk_doctor.core.utils import (
    UnsafeScanRoot,
    collect_files,
    is_symlink_or_reparse,
    path_size_report,
)
from disk_doctor.tools.cache_analyzer import CacheAnalyzer
from disk_doctor.tools.disk_scanner import DiskScanner
from disk_doctor.tools.duplicate_finder import DuplicateFinder

PACKAGE = pathlib.Path(__file__).resolve().parents[1] / "src" / "disk_doctor"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

@pytest.fixture
def store(tmp_path) -> PlanStore:
    return PlanStore(tmp_path / "state")


@pytest.fixture
def junctions(monkeypatch):
    """Report marked paths as reparse points, without needing the privilege.

    Only ``utils.is_reparse_point`` is patched: ``is_symlink_or_reparse`` is
    defined in that module and resolves the name at call time, so every scanner
    sees the same answer. Tests using this are simulations, not evidence about
    real Windows junctions.
    """

    marked: set[str] = set()
    original = path_utils.is_reparse_point
    probe = lambda p: str(p) in marked or original(p)  # noqa: E731
    monkeypatch.setattr(path_utils, "is_reparse_point", probe)
    return marked


def _set_state(store: PlanStore, plan_id: str, state: ExecutionState) -> None:
    store.state_path(plan_id).write_text(state.value, encoding="utf-8")


def _plan_for(root: pathlib.Path, directory: pathlib.Path) -> CleanPlan:
    refs, total = collect_files(directory)
    item = PlanItem(
        item_id="I001-abc123",
        path=str(directory),
        category="测试缓存",
        risk=RiskLevel.SAFE,
        size_bytes=total,
        file_count=len(refs),
        files=refs,
    )
    return CleanPlan(root=str(root), items=[item]).seal()


def _build_outside(tmp_path: pathlib.Path) -> pathlib.Path:
    """A directory the caller must never reach through a linked root.

    ``a.bin`` and ``b.bin`` are byte-identical on purpose: if anything walks
    into this directory, a duplicate group appears and the assertion fails.
    """

    outside = tmp_path / "outside"
    (outside / "__pycache__").mkdir(parents=True)
    (outside / "secret.bin").write_bytes(b"S" * 64)
    (outside / "a.bin").write_bytes(b"D" * 4096)
    (outside / "b.bin").write_bytes(b"D" * 4096)
    (outside / "__pycache__" / "m.cpython-311.pyc").write_bytes(b"P" * 32)
    return outside


def _populate(path: pathlib.Path) -> None:
    """Give a stand-in root content, so refusing it is observable.

    Without files inside, "we returned nothing" would be true even if the
    guard were missing — a test that passes against the bug.
    """

    path.mkdir(parents=True, exist_ok=True)
    (path / "secret.bin").write_bytes(b"T" * 64)
    (path / "a.bin").write_bytes(b"E" * 4096)
    (path / "b.bin").write_bytes(b"E" * 4096)
    (path / "__pycache__").mkdir(exist_ok=True)
    (path / "__pycache__" / "n.cpython-311.pyc").write_bytes(b"Q" * 32)


def _linked_root(tmp_path: pathlib.Path, outside: pathlib.Path, marked: set[str]):
    """Create a root that resolves to *outside*. Returns ``(path, mode)``.

    In simulated mode the directory is populated with files of its own. That
    matters: an empty stand-in would make "returns nothing" true even without
    the guard, and the test would pass against the buggy code.
    """

    link = tmp_path / "selected"
    try:
        link.symlink_to(outside, target_is_directory=True)
        return link, "native"
    except (OSError, NotImplementedError):
        # No SeCreateSymbolicLinkPrivilege here: a directory marked as a
        # reparse point takes the same branch in is_symlink_or_reparse().
        _populate(link)
        marked.add(str(link))
        return link, "simulated"


# --------------------------------------------------------------------------- #
# A. reconcile_interrupted decides under the lock
# --------------------------------------------------------------------------- #

def test_reconcile_does_not_overwrite_completed_state(store):
    plan_id = "a" * 64
    _set_state(store, plan_id, ExecutionState.COMPLETED)

    assert store.reconcile_interrupted() == []
    assert store.read_state(plan_id) is ExecutionState.COMPLETED


def test_reconcile_does_not_overwrite_failed_state(store):
    plan_id = "b" * 64
    _set_state(store, plan_id, ExecutionState.FAILED)

    assert store.reconcile_interrupted() == []
    assert store.read_state(plan_id) is ExecutionState.FAILED


def test_reconcile_rechecks_state_under_lock(store, monkeypatch):
    """The interleaving that v0.2.2 got wrong.

    1. state is IN_PROGRESS;
    2. our optimistic read returns IN_PROGRESS;
    3. the real executor finishes and writes COMPLETED;
    4. we take the lock;
    5. we re-read — and must see COMPLETED, not our stale value.
    """

    plan_id = "c" * 64
    _set_state(store, plan_id, ExecutionState.IN_PROGRESS)

    real_read = store.read_state
    calls = {"n": 0}

    def stale_first_read(pid: str) -> ExecutionState:
        observed = real_read(pid)
        calls["n"] += 1
        if calls["n"] == 1:
            # The live executor completes between our read and our lock.
            _set_state(store, pid, ExecutionState.COMPLETED)
            return ExecutionState.IN_PROGRESS  # what we thought we saw
        return observed

    monkeypatch.setattr(store, "read_state", stale_first_read)

    assert store.reconcile_interrupted() == []
    assert real_read(plan_id) is ExecutionState.COMPLETED, "恢复过程不得覆盖已完成的计划"


def test_reconcile_does_not_change_active_plan(tmp_path, vault, make_file):
    """A plan whose lock is held is left exactly as it is."""

    cache = vault / "__pycache__"
    make_file(cache / "a.pyc", 16)
    plan = _plan_for(vault, cache)
    store = PlanStore(tmp_path / "state")
    store.save(plan)

    # (a) same process holds the claim: the in-process lock blocks reconcile.
    with store.claim_for_execution(plan.plan_id):
        assert store.read_state(plan.plan_id) is ExecutionState.IN_PROGRESS
        assert store.reconcile_interrupted() == []
        assert store.read_state(plan.plan_id) is ExecutionState.IN_PROGRESS

    # (b) a different owner holds only the file lock: reconcile must back off.
    other_id = "d" * 64
    _set_state(store, other_id, ExecutionState.IN_PROGRESS)
    other = PlanStore(tmp_path / "state")
    handle = other._acquire_file_lock(other_id)
    try:
        assert store.reconcile_interrupted() == []
        assert store.read_state(other_id) is ExecutionState.IN_PROGRESS
    finally:
        other._release_file_lock(handle)


def test_reconcile_marks_only_orphaned_in_progress(tmp_path):
    store = PlanStore(tmp_path / "state")
    orphan = "1" * 64
    completed = "2" * 64
    failed = "3" * 64
    ready = "4" * 64
    interrupted = "5" * 64

    _set_state(store, orphan, ExecutionState.IN_PROGRESS)
    _set_state(store, completed, ExecutionState.COMPLETED)
    _set_state(store, failed, ExecutionState.FAILED)
    _set_state(store, interrupted, ExecutionState.INTERRUPTED)
    # ``ready`` has no state file at all.

    assert store.reconcile_interrupted() == [orphan]
    assert store.read_state(orphan) is ExecutionState.INTERRUPTED
    assert store.read_state(completed) is ExecutionState.COMPLETED
    assert store.read_state(failed) is ExecutionState.FAILED
    assert store.read_state(interrupted) is ExecutionState.INTERRUPTED
    assert store.read_state(ready) is ExecutionState.READY

    # Second pass is a no-op: INTERRUPTED is terminal, not a fresh orphan.
    assert store.reconcile_interrupted() == []


def test_reconcile_is_fail_closed_without_lock_backend(store, monkeypatch):
    plan_id = "e" * 64
    _set_state(store, plan_id, ExecutionState.IN_PROGRESS)
    monkeypatch.setattr(clean_plan_module, "portalocker", None)

    assert store.reconcile_interrupted() == []
    assert store.read_state(plan_id) is ExecutionState.IN_PROGRESS


# --------------------------------------------------------------------------- #
# B. Linked scan roots are refused
# --------------------------------------------------------------------------- #

def test_disk_scanner_rejects_symlink_root(tmp_path, junctions):
    outside = _build_outside(tmp_path)
    root, mode = _linked_root(tmp_path, outside, junctions)
    scanner = DiskScanner()

    for entry in (
        scanner.volume,
        scanner.directory_sizes,
        scanner.top_files,
        scanner.scan,
    ):
        with pytest.raises(UnsafeScanRoot):
            entry(root)

    assert (outside / "secret.bin").exists(), "拒绝不得变成删除或移动"
    assert mode in {"native", "simulated"}, f"mode={mode}"


def test_disk_scanner_rejects_reparse_root(tmp_path, junctions, monkeypatch):
    """Simulated junction: proves the check runs *before* any measurement."""

    outside = _build_outside(tmp_path)
    root = tmp_path / "junctioned"
    root.mkdir()
    junctions.add(str(root))

    def boom(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("disk_usage 不得在根检查之前被调用")

    monkeypatch.setattr("disk_doctor.tools.disk_scanner.shutil.disk_usage", boom)

    with pytest.raises(UnsafeScanRoot):
        DiskScanner().volume(root)
    assert (outside / "secret.bin").exists()


def test_duplicate_finder_rejects_symlink_root(tmp_path, junctions):
    outside = _build_outside(tmp_path)
    root, mode = _linked_root(tmp_path, outside, junctions)
    finder = DuplicateFinder(min_size=1)

    assert finder.find([root]) == []
    # The fixture really does contain duplicates, so the empty result above is
    # evidence of refusal rather than of an empty tree.
    assert finder.find([outside]), f"夹具应包含重复文件（mode={mode}）"


def test_duplicate_finder_rejects_reparse_root(tmp_path, junctions):
    outside = _build_outside(tmp_path)
    root = tmp_path / "junctioned"
    _populate(root)
    junctions.add(str(root))
    finder = DuplicateFinder(min_size=1)

    assert finder.find([root]) == []
    assert finder.find([outside])


def test_cache_analyzer_rejects_symlink_root(tmp_path, junctions):
    outside = _build_outside(tmp_path)
    root, mode = _linked_root(tmp_path, outside, junctions)
    analyzer = CacheAnalyzer(PolicyEngine([tmp_path]))

    assert analyzer.discover([root]) == []
    assert analyzer.discover([outside]), f"夹具应包含 __pycache__（mode={mode}）"


def test_cache_analyzer_rejects_reparse_root(tmp_path, junctions):
    outside = _build_outside(tmp_path)
    root = tmp_path / "junctioned"
    _populate(root)
    junctions.add(str(root))
    analyzer = CacheAnalyzer(PolicyEngine([tmp_path]))

    assert analyzer.discover([root]) == []
    assert analyzer.discover([outside])


def test_collect_files_rejects_symlink_root(tmp_path, junctions):
    outside = _build_outside(tmp_path)
    root, mode = _linked_root(tmp_path, outside, junctions)

    refs, total = collect_files(root)
    assert (refs, total) == ((), 0), f"mode={mode}"
    assert collect_files(outside)[0], "夹具应包含文件"


def test_path_size_rejects_symlink_root(tmp_path, junctions):
    outside = _build_outside(tmp_path)
    root, mode = _linked_root(tmp_path, outside, junctions)

    report = path_size_report(root)
    assert (report.bytes, report.files, report.skipped_redirects) == (0, 0, 1), f"mode={mode}"
    assert path_size_report(outside).bytes > 0


def test_linked_root_detection_uses_shared_guard(tmp_path, junctions):
    """Regression guard: the check is the shared one, not a local re-implementation."""

    root = tmp_path / "junctioned"
    root.mkdir()
    assert not is_symlink_or_reparse(root)
    junctions.add(str(root))
    assert is_symlink_or_reparse(root)
