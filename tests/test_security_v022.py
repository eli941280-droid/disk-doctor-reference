"""v0.2.2 security regression suite.

Sections follow the hardening brief:

A. TrashManager has no independent write entry
B. Execution claim: cross-process lock, atomic state, one-shot receipt
C. Reparse point / symlink handling is shared by every scanner
D. Quarantine naming never overwrites
E. Tool registry matches the real interfaces
F. Static invariants (no stale booleans, no model access to the authority)

Every filesystem operation happens under ``tmp_path``. No test touches the real
recycle bin or the user's files. Native Windows symlinks may be unavailable in
this environment; where that happens the code says so and exercises the same
branch through monkeypatch instead of skipping.
"""

from __future__ import annotations

import inspect
import json
import multiprocessing as mp
import pathlib
import re
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from v022_worker import claim_plan

from disk_doctor.agent.llm_contract import LLMContractError, parse_llm_intent
from disk_doctor.agent.orchestrator import DiskDoctorAgent
from disk_doctor.core import utils as path_utils
from disk_doctor.core.audit_logger import AuditLogger
from disk_doctor.core.clean_plan import (
    ConfirmationAuthority,
    PlanStore,
    PlanStoreError,
)
from disk_doctor.core.safe_executor import QuarantineBackend, SafeExecutor
from disk_doctor.core.schemas import (
    CleanPlan,
    ExecutionState,
    ItemStatus,
    PlanItem,
    RiskLevel,
)
from disk_doctor.core.utils import collect_files, is_symlink_or_reparse, path_size_report
from disk_doctor.tools.cache_analyzer import CacheAnalyzer
from disk_doctor.tools.disk_scanner import DiskScanner
from disk_doctor.tools.duplicate_finder import DuplicateFinder
from disk_doctor.tools.registry import ToolNotFound, ToolRegistry
from disk_doctor.tools.trash_manager import TrashManager

PACKAGE = pathlib.Path(__file__).resolve().parents[1] / "src" / "disk_doctor"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _plan_for(root: Path, directory: Path, *, ttl: int = 900) -> CleanPlan:
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
    return CleanPlan(root=str(root), items=[item], ttl_seconds=ttl).seal()


class _RecordingTrash:
    """Fake backend that records what the executor asked it to do, and when."""

    name = "recording"

    def __init__(self, *, store: PlanStore | None = None, plan_id: str | None = None):
        self.moves: list[str] = []
        self.states: list[ExecutionState] = []
        self.consumed: list[bool] = []
        self.store = store
        self.plan_id = plan_id
        self.authority: ConfirmationAuthority | None = None
        self.receipt = None
        self._lock = threading.Lock()

    def is_available(self) -> bool:
        return True

    def move(self, path: Path) -> None:
        with self._lock:
            self.moves.append(str(path))
            if self.store is not None and self.plan_id is not None:
                self.states.append(self.store.read_state(self.plan_id))
            if self.authority is not None and self.receipt is not None:
                self.consumed.append(self.authority.is_consumed(self.receipt.nonce))
        Path(path).unlink()


class _UnavailableTrash:
    name = "unavailable"

    def is_available(self) -> bool:
        return False

    def move(self, path: Path) -> None:  # pragma: no cover - must not be called
        raise AssertionError("move() must never be called when unavailable")


def _raise_oserror(*args, **kwargs):
    raise OSError("simulated failure")


def _make_symlink(link: Path, target: Path) -> bool:
    """Create a real symlink. Returns False when the OS refuses."""

    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
        return True
    except (OSError, NotImplementedError):
        return False


@pytest.fixture
def stored_plan(tmp_path, vault, make_file):
    """A persisted plan plus the file it points at."""

    cache = vault / "__pycache__"
    make_file(cache / "a.pyc", 64)
    plan = _plan_for(vault, cache)
    store = PlanStore(tmp_path / "state")
    store.save(plan)
    return SimpleNamespace(store=store, plan=plan, cache=cache)


@pytest.fixture
def agent(tmp_path, vault):
    return DiskDoctorAgent.create([str(vault)], home=tmp_path / "home")


@pytest.fixture
def junctions(monkeypatch):
    """Simulate Windows reparse points without needing SeCreateSymbolicLinkPrivilege.

    The marked paths are reported as junctions by the shared guard, which is the
    same code path a real junction takes. Tests that use this are labelled as
    simulated in their docstring — they are not evidence about real Windows
    junctions, only about the branch.
    """

    import disk_doctor.core.policy_engine as policy_module

    marked: set[str] = set()
    original = path_utils.is_reparse_point
    probe = lambda p: str(p) in marked or original(p)  # noqa: E731
    monkeypatch.setattr(path_utils, "is_reparse_point", probe)
    # policy_engine imported the function by value, so patch that binding too.
    monkeypatch.setattr(policy_module, "is_reparse_point", probe)
    return marked


# --------------------------------------------------------------------------- #
# A. TrashManager has no independent write entry
# --------------------------------------------------------------------------- #

def test_trash_manager_has_no_public_move():
    public = {n for n in dir(TrashManager) if not n.startswith("_")}
    assert "move" not in public
    assert public == {"backend_name", "is_available", "backend_info"}, (
        "TrashManager 的公开接口发生了变化：任何新增的写能力都必须先过安全评审"
    )


def test_trash_manager_cannot_quarantine_directly():
    for name in ("quarantine", "delete", "remove", "purge", "trash"):
        assert not hasattr(TrashManager, name), f"TrashManager 不应暴露 {name}"


def test_only_safe_executor_can_write_user_data():
    """Static check: exactly one module calls a backend's ``move``."""

    callers = set()
    for path in PACKAGE.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if re.search(r"\btrash\.move\(|\bbackend\.move\(", text):
            callers.add(path.relative_to(PACKAGE).as_posix())
    assert callers == {"core/safe_executor.py"}


def test_llm_registry_contains_no_write_tools(agent):
    assert agent.registry.write_tools() == []
    assert all(tool["read_only"] for tool in agent.registry.llm_tools())


def test_trash_backend_not_exposed_as_agent_tool(agent):
    for name in ("safe_executor.execute", "trash_manager.move", "trash", "trash.move"):
        with pytest.raises(ToolNotFound):
            agent.registry.get(name)


def test_registry_rejects_write_tools_at_registration():
    registry = ToolRegistry()
    with pytest.raises(ValueError, match="write tool"):
        registry.register("x.move", "写盘", lambda: None, read_only=False)

    permissive = ToolRegistry(llm_facing=False)
    permissive.register("x.move", "写盘", lambda: None, read_only=False)
    assert permissive.write_tools() == ["x.move"]


# --------------------------------------------------------------------------- #
# B. Execution claim: lock, state, one-shot receipt
# --------------------------------------------------------------------------- #

def test_plan_claim_marks_in_progress_before_first_move(stored_plan, policy, authority):
    trash = _RecordingTrash(store=stored_plan.store, plan_id=stored_plan.plan.plan_id)
    executor = SafeExecutor(policy, trash, authority=authority)
    receipt = authority.issue(
        stored_plan.plan, confirmation=f"EXECUTE {stored_plan.plan.plan_id}"
    )

    with stored_plan.store.claim_for_execution(stored_plan.plan.plan_id) as claim:
        executor.execute(claim.plan, receipt)

    assert trash.states == [ExecutionState.IN_PROGRESS]
    assert stored_plan.store.read_state(stored_plan.plan.plan_id) is ExecutionState.COMPLETED


def test_receipt_consumed_before_first_move(stored_plan, policy, authority):
    trash = _RecordingTrash()
    receipt = authority.issue(
        stored_plan.plan, confirmation=f"EXECUTE {stored_plan.plan.plan_id}"
    )
    trash.authority = authority
    trash.receipt = receipt
    executor = SafeExecutor(policy, trash, authority=authority)

    with stored_plan.store.claim_for_execution(stored_plan.plan.plan_id) as claim:
        result = executor.execute(claim.plan, receipt)

    assert result.blocked_reason is None
    assert trash.consumed == [True], "首次移动文件时，凭证必须已经作废"


def test_same_receipt_concurrent_execution(policy, vault, make_file, authority):
    """Two threads, one receipt: exactly one may move anything."""

    cache = vault / "__pycache__"
    target = make_file(cache / "a.pyc", 64)
    plan = _plan_for(vault, cache)
    receipt = authority.issue(plan, confirmation=f"EXECUTE {plan.plan_id}")

    trash = _RecordingTrash()
    executor = SafeExecutor(policy, trash, authority=authority)
    outcomes: list[str | None] = []
    barrier = threading.Barrier(2)

    def run() -> None:
        barrier.wait(10)
        outcomes.append(executor.execute(plan, receipt).blocked_reason)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)

    assert outcomes.count(None) == 1
    assert "approval:approval_already_used" in outcomes
    assert trash.moves == [str(target)]
    assert not target.exists()


def test_same_plan_concurrent_execution(stored_plan):
    """Four processes, one plan: exactly one claim succeeds."""

    ctx = mp.get_context("spawn")
    queue = ctx.Queue()
    start = ctx.Event()
    store_dir = str(stored_plan.store.directory)

    processes = [
        ctx.Process(
            target=claim_plan,
            args=(store_dir, stored_plan.plan.plan_id, start, queue, 0.6),
        )
        for _ in range(4)
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(60)

    assert all(p.exitcode == 0 for p in processes), [p.exitcode for p in processes]
    outcomes = [queue.get(timeout=10) for _ in processes]
    claimed = [o for o in outcomes if o[0] == "claimed"]
    rejected = [o for o in outcomes if o[0] != "claimed"]

    assert len(claimed) == 1, outcomes
    assert len(rejected) == 3, outcomes
    assert all(o[0] == "rejected" for o in rejected)
    assert all(o[1] in {"plan_locked", "plan_consumed", "plan_interrupted"} for o in rejected)


def test_plan_lock_rejects_second_process(stored_plan):
    ctx = mp.get_context("spawn")
    queue = ctx.Queue()

    with stored_plan.store.claim_for_execution(stored_plan.plan.plan_id):
        process = ctx.Process(
            target=claim_plan,
            args=(str(stored_plan.store.directory), stored_plan.plan.plan_id, None, queue),
        )
        process.start()
        process.join(60)

    assert process.exitcode == 0
    assert queue.get(timeout=10) == ("rejected", "plan_locked")


def test_plan_in_progress_cannot_be_replayed(stored_plan):
    plan_id = stored_plan.plan.plan_id
    stored_plan.store.state_path(plan_id).write_text(
        ExecutionState.IN_PROGRESS.value, encoding="utf-8"
    )

    with pytest.raises(PlanStoreError) as first:
        with stored_plan.store.claim_for_execution(plan_id):
            pass
    assert first.value.code == "plan_interrupted"
    assert stored_plan.store.read_state(plan_id) is ExecutionState.INTERRUPTED

    with pytest.raises(PlanStoreError) as second:
        with stored_plan.store.claim_for_execution(plan_id):
            pass
    assert second.value.code == "plan_consumed"


def test_interrupted_plan_rejected(stored_plan):
    plan_id = stored_plan.plan.plan_id
    stored_plan.store.state_path(plan_id).write_text(
        ExecutionState.INTERRUPTED.value, encoding="utf-8"
    )
    with pytest.raises(PlanStoreError) as exc:
        with stored_plan.store.claim_for_execution(plan_id):
            pass
    assert exc.value.code == "plan_consumed"


def test_reconcile_interrupted_marks_orphans(stored_plan):
    plan_id = stored_plan.plan.plan_id
    stored_plan.store.state_path(plan_id).write_text(
        ExecutionState.IN_PROGRESS.value, encoding="utf-8"
    )
    recovered = stored_plan.store.reconcile_interrupted()
    assert recovered == [plan_id]
    assert stored_plan.store.read_state(plan_id) is ExecutionState.INTERRUPTED


def test_completed_plan_cannot_be_claimed_again(stored_plan):
    with stored_plan.store.claim_for_execution(stored_plan.plan.plan_id):
        pass
    with pytest.raises(PlanStoreError) as exc:
        with stored_plan.store.claim_for_execution(stored_plan.plan.plan_id):
            pass
    assert exc.value.code == "plan_consumed"


def test_plan_id_is_stable_across_execution(stored_plan):
    path = stored_plan.store.path_for(stored_plan.plan.plan_id)
    before = path.read_text(encoding="utf-8")
    with stored_plan.store.claim_for_execution(stored_plan.plan.plan_id):
        pass
    after = path.read_text(encoding="utf-8")

    assert before == after, "执行状态不得写进计划本身"
    assert CleanPlan.model_validate(json.loads(after)).compute_plan_id() == stored_plan.plan.plan_id


def test_failed_execution_cannot_reuse_receipt(policy, vault, make_file, authority):
    cache = vault / "__pycache__"
    target = make_file(cache / "a.pyc", 64)
    plan = _plan_for(vault, cache)
    receipt = authority.issue(plan, confirmation=f"EXECUTE {plan.plan_id}")
    executor = SafeExecutor(policy, _RecordingTrash(), authority=authority)

    target.write_bytes(b"changed after the plan was sealed")
    first = executor.execute(plan, receipt)
    assert first.blocked_reason is not None
    assert first.blocked_reason.startswith("preflight_failed")

    second = executor.execute(plan, receipt)
    assert second.blocked_reason == "approval:approval_already_used"


def test_trash_unavailable_blocks_before_any_state_change(policy, vault, make_file, authority):
    cache = vault / "__pycache__"
    make_file(cache / "a.pyc", 16)
    plan = _plan_for(vault, cache)
    receipt = authority.issue(plan, confirmation=f"EXECUTE {plan.plan_id}")

    result = SafeExecutor(policy, _UnavailableTrash(), authority=authority).execute(plan, receipt)
    assert result.blocked_reason == "trash_unavailable"
    assert (cache / "a.pyc").exists()


# --------------------------------------------------------------------------- #
# C. Shared reparse / symlink guard
# --------------------------------------------------------------------------- #

def test_disk_scanner_skips_symlink(vault, make_file, monkeypatch):
    target = vault / "target"
    make_file(target / "big.bin", 4096)
    link = vault / "linked"
    native = _make_symlink(link, target)
    if not native:
        # No SeCreateSymbolicLinkPrivilege here: exercise the same branch.
        link.mkdir()
        make_file(link / "big.bin", 4096)
        original = is_symlink_or_reparse
        monkeypatch.setattr(
            "disk_doctor.tools.disk_scanner.is_symlink_or_reparse",
            lambda p: Path(p) == link or original(p),
        )

    entries = DiskScanner().directory_sizes(vault)
    assert "linked" not in {Path(e.path).name for e in entries}
    assert "target" in {Path(e.path).name for e in entries}


def test_disk_scanner_skips_reparse_directory(vault, make_file, junctions):
    """Simulated junction: the shared guard reports it, the scanner must obey."""

    fake = vault / "junctioned"
    make_file(fake / "payload.bin", 4096)
    junctions.add(str(fake))

    scanner = DiskScanner()
    assert "junctioned" not in {Path(e.path).name for e in scanner.directory_sizes(vault)}
    assert all("junctioned" not in f.path for f in scanner.top_files(vault, limit=10))


def test_path_size_skips_reparse_directory(tmp_path, make_file, junctions):
    root = tmp_path / "root"
    make_file(root / "plain.bin", 100)
    fake = root / "junctioned"
    make_file(fake / "payload.bin", 4096)
    junctions.add(str(fake))

    report = path_size_report(root)
    assert report.bytes == 100
    assert report.files == 1
    assert report.skipped_redirects == 1


def test_duplicate_finder_skips_symlink(tmp_path, make_file, monkeypatch):
    real = tmp_path / "real"
    one = make_file(real / "one.bin", 2048, fill=b"a")
    make_file(tmp_path / "other" / "two.bin", 2048, fill=b"a")

    link_dir = tmp_path / "linked"
    native = _make_symlink(link_dir, real)
    if not native:
        link_dir.mkdir()
        make_file(link_dir / "one.bin", 2048, fill=b"a")
        original = is_symlink_or_reparse
        monkeypatch.setattr(
            "disk_doctor.tools.duplicate_finder.is_symlink_or_reparse",
            lambda p: Path(p) == link_dir or original(p),
        )

    groups = DuplicateFinder(min_size=1024).find([tmp_path])
    found = {p for g in groups for p in g.paths}
    assert {str(one), str(tmp_path / "other" / "two.bin")} <= found
    assert all(str(link_dir) not in p for p in found)


def test_duplicate_finder_skips_reparse_directory(tmp_path, make_file, junctions):
    """Simulated junction inside the scan root."""

    make_file(tmp_path / "a" / "same.bin", 2048, fill=b"z")
    fake = tmp_path / "junctioned"
    make_file(fake / "same.bin", 2048, fill=b"z")
    junctions.add(str(fake))

    groups = DuplicateFinder(min_size=1024).find([tmp_path])
    paths = {p for g in groups for p in g.paths}
    assert not any("junctioned" in p for p in paths)


def test_cache_analyzer_uses_shared_path_guard(vault, make_file, junctions, policy):
    """One implementation of the guard, and the analyzer honours it."""

    source = (PACKAGE / "tools" / "cache_analyzer.py").read_text(encoding="utf-8")
    assert "def is_reparse_point" not in source
    assert "is_symlink_or_reparse" in source

    fake = vault / "__pycache__"
    make_file(fake / "a.pyc", 32)
    junctions.add(str(fake))

    analyzer = CacheAnalyzer(policy)
    assert analyzer.discover([vault]) == []


def test_policy_rejects_reparse_root(tmp_path, junctions):
    from disk_doctor.core.policy_engine import PolicyEngine

    root = tmp_path / "root"
    root.mkdir()
    junctions.add(str(root))

    verdict = PolicyEngine([root]).validate_path_chain(root, root)
    assert not verdict.allowed
    assert verdict.rule == "reparse_root"


# --------------------------------------------------------------------------- #
# D. Quarantine naming
# --------------------------------------------------------------------------- #

def test_quarantine_preserves_existing_target(tmp_path):
    backend = QuarantineBackend(tmp_path / "q")
    existing = backend.directory / "payload.bin"
    existing.write_bytes(b"user-owned-data")

    source = tmp_path / "src" / "payload.bin"
    source.parent.mkdir()
    source.write_bytes(b"new-payload")

    backend.move(source)

    assert existing.read_bytes() == b"user-owned-data"
    assert not source.exists()
    assert len(list(backend.directory.iterdir())) == 2


def test_quarantine_collision_does_not_overwrite(tmp_path):
    backend = QuarantineBackend(tmp_path / "q")
    src_dir = tmp_path / "src"
    src_dir.mkdir()

    for index in range(3):
        source = src_dir / "payload.bin"
        source.write_bytes(b"payload-%d" % index)
        backend.move(source)

    contents = sorted(p.read_bytes() for p in backend.directory.iterdir())
    assert len(contents) == 3
    assert contents == [b"payload-0", b"payload-1", b"payload-2"]


def test_quarantine_concurrent_same_filename(tmp_path):
    backend = QuarantineBackend(tmp_path / "q")
    sources = []
    for index in range(4):
        directory = tmp_path / "src" / str(index)
        directory.mkdir(parents=True)
        source = directory / "same.bin"
        source.write_bytes(b"payload-%d" % index)
        sources.append(source)

    errors: list[BaseException] = []

    def run(path: Path) -> None:
        try:
            backend.move(path)
        except BaseException as exc:  # noqa: BLE001 - collected, then asserted
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(p,)) for p in sources]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)

    assert errors == []
    assert all(not p.exists() for p in sources)
    names = [p.name for p in backend.directory.iterdir()]
    assert len(names) == 4
    assert len(set(names)) == 4
    assert sorted(p.read_bytes() for p in backend.directory.iterdir()) == [
        b"payload-0",
        b"payload-1",
        b"payload-2",
        b"payload-3",
    ]


def test_quarantine_copy_failure_preserves_source(tmp_path, monkeypatch):
    backend = QuarantineBackend(tmp_path / "q")
    source = tmp_path / "a.bin"
    source.write_bytes(b"x" * 32)

    monkeypatch.setattr(QuarantineBackend, "_direct_replace", staticmethod(_raise_oserror))
    monkeypatch.setattr("disk_doctor.core.safe_executor.shutil.copyfileobj", _raise_oserror)

    with pytest.raises(OSError):
        backend.move(source)

    assert source.exists()
    assert source.read_bytes() == b"x" * 32
    assert list(backend.directory.iterdir()) == [], "失败后不得留下占位文件"


def test_quarantine_partial_failure_logged(policy, vault, make_file, tmp_path, monkeypatch):
    cache = vault / "__pycache__"
    make_file(cache / "a.pyc", 64)
    plan = _plan_for(vault, cache)

    authority = ConfirmationAuthority(secret=b"v022-partial")
    receipt = authority.issue(plan, confirmation=f"EXECUTE {plan.plan_id}")
    audit = AuditLogger(tmp_path / "audit.jsonl")
    backend = QuarantineBackend(tmp_path / "q")
    executor = SafeExecutor(policy, backend, authority=authority, audit=audit)

    monkeypatch.setattr(QuarantineBackend, "_direct_replace", staticmethod(_raise_oserror))
    monkeypatch.setattr(QuarantineBackend, "_remove_source", staticmethod(_raise_oserror))

    result = executor.execute(plan, receipt)

    assert result.outcomes[0].status is ItemStatus.FAILED
    assert (cache / "a.pyc").exists(), "源文件必须保留"
    assert len(list(backend.directory.iterdir())) == 1, "副本应留在隔离区"
    assert audit.of_event("file_failed"), "部分完成必须留下审计记录"


def test_quarantine_rejects_directories(tmp_path):
    backend = QuarantineBackend(tmp_path / "q")
    directory = tmp_path / "folder"
    directory.mkdir()
    with pytest.raises(OSError):
        backend.move(directory)
    assert directory.is_dir()


# --------------------------------------------------------------------------- #
# E. Registry matches reality
# --------------------------------------------------------------------------- #

def test_tool_registry_matches_real_signatures(agent):
    for spec in agent.registry.describe():
        handler = agent.registry.get(spec["name"]).handler
        signature = inspect.signature(handler)
        params = set(signature.parameters)
        accepts_kwargs = any(
            p.kind is p.VAR_KEYWORD for p in signature.parameters.values()
        )
        declared = set(spec["inputs"])
        assert declared <= params or accepts_kwargs, (
            f"{spec['name']} 声明的输入 {declared} 与真实签名 {params} 不一致"
        )


def test_no_stale_confirm_boolean_api():
    """No ``confirm: bool`` / ``require_confirmation`` in the code, not in prose.

    Parsed with ``ast`` rather than grepped: docstrings legitimately talk about
    the boolean this project removed, and a text scan would either fail on the
    explanation or tempt someone to delete the explanation.
    """

    import ast

    banned = {"confirm", "second_confirm", "require_confirmation"}
    offenders: list[str] = []

    for path in PACKAGE.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rel = path.relative_to(PACKAGE).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                params = {a.arg for a in node.args.args + node.args.kwonlyargs}
                for name in banned & params:
                    offenders.append(f"{rel}:{node.lineno} 形参 {name}")
            elif isinstance(node, ast.Call):
                for keyword in node.keywords:
                    if keyword.arg in banned:
                        offenders.append(f"{rel}:{node.lineno} 实参 {keyword.arg}")

    assert offenders == []

    parameters = inspect.signature(SafeExecutor.execute).parameters
    assert "confirm" not in parameters
    assert "receipt" in parameters

    agent_parameters = inspect.signature(DiskDoctorAgent.execute_plan).parameters
    assert {"plan_id", "confirmation", "second_confirmation"} <= set(agent_parameters)


def test_llm_has_no_execution_tools(agent):
    assert agent.registry.write_tools() == []
    assert all(tool["read_only"] for tool in agent.registry.llm_tools())
    assert not hasattr(agent.llm, "tools")
    assert not hasattr(agent.llm, "call_tool")


def test_llm_cannot_access_confirmation_authority():
    forbidden = ("ConfirmationAuthority", "PlanStore", "SafeExecutor", "TrashBackend")
    source = (PACKAGE / "agent" / "llm_contract.py").read_text(encoding="utf-8")
    for name in forbidden:
        assert name not in source

    with pytest.raises(LLMContractError):
        parse_llm_intent(
            {
                "intent": "plan",
                "confirmation": "EXECUTE " + "0" * 64,
            }
        )

    with pytest.raises(LLMContractError):
        parse_llm_intent({"intent": "execute", "plan_id": "0" * 64})


# --------------------------------------------------------------------------- #
# F. End-to-end: the agent path still refuses what it should
# --------------------------------------------------------------------------- #

def test_agent_execute_requires_stored_plan(tmp_path, vault):
    agent = DiskDoctorAgent.create([str(vault)], home=tmp_path / "home")
    turn = agent.execute_plan("0" * 64, confirmation="EXECUTE " + "0" * 64)
    assert turn.data["error"] in {"plan_not_found", "plan_consumed"}


def test_agent_execute_consumes_plan_once(tmp_path, vault, make_file):
    cache = vault / "__pycache__"
    make_file(cache / "a.pyc", 32)

    agent = DiskDoctorAgent.create(
        [str(vault)], home=tmp_path / "home", quarantine_dir=tmp_path / "q"
    )
    preview = agent.handle("清理", scope=str(vault))
    assert preview.plan_id

    first = agent.execute_plan(preview.plan_id, confirmation=f"EXECUTE {preview.plan_id}")
    assert first.data["result"]["blocked_reason"] is None
    assert not (cache / "a.pyc").exists()

    second = agent.execute_plan(preview.plan_id, confirmation=f"EXECUTE {preview.plan_id}")
    assert second.data["error"] == "plan_consumed"
    assert "result" not in second.data
