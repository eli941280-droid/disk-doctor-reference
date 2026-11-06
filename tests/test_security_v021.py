"""v0.2.1 security regression suite.

Covers the four P0 findings and the two P1 fixes from
``docs/SECURITY_AUDIT_v0.2.0.md``:

A. parent-directory symlink / junction escape (P0-1)
B. Temp/tmp generalisation (P0-2)
C. exact plan-id confirmation + PlanStore + receipts (P0-3)
D. quarantine write probe (P0-4)
E. space accounting (P1-1)
F. Windows path parsing (P1-2)

Everything runs in ``tmp_path`` with an in-memory quarantine. No test touches a
real recycle bin or a real user directory.
"""

from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

import pytest

from disk_doctor.agent.intent_router import IntentRouter, normalize_windows_path
from disk_doctor.agent.orchestrator import DiskDoctorAgent
from disk_doctor.cli import main as cli_main
from disk_doctor.core.clean_plan import (
    ApprovalError,
    ConfirmationAuthority,
    PlanStore,
    PlanStoreError,
)
from disk_doctor.core.policy_engine import PolicyEngine
from disk_doctor.core.safe_executor import QuarantineBackend
from disk_doctor.core.schemas import CleanPlan, PlanItem, RiskLevel
from disk_doctor.core.utils import collect_files, utc_now
from disk_doctor.tools.cache_analyzer import CacheAnalyzer, has_valid_cachedir_tag

# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _pycache(root: Path, name: str = "a.pyc", size: int = 32) -> Path:
    cache = root / "__pycache__"
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / name
    target.write_bytes(b"x" * size)
    return target


def _plan_for(root: Path, directory: Path, *, ttl: int = 900) -> CleanPlan:
    refs, total = collect_files(directory)
    item = PlanItem(
        item_id="I001-abc123",
        path=str(directory),
        category="Python 字节码",
        risk=RiskLevel.SAFE,
        size_bytes=total,
        file_count=len(refs),
        files=refs,
    )
    return CleanPlan(root=str(root), items=[item], ttl_seconds=ttl).seal()


@pytest.fixture
def two_roots(tmp_path):
    a = tmp_path / "A"
    b = tmp_path / "B"
    a.mkdir()
    b.mkdir()
    return a, b


# --------------------------------------------------------------------------- #
# A. parent-directory escape (P0-1)
# --------------------------------------------------------------------------- #

def test_parent_symlink_to_other_allowed_root_rejected(
    two_roots, monkeypatch
):
    """A\\__pycache__ replaced by a link into B must not be followed."""

    import disk_doctor.core.policy_engine as pe

    a, b = two_roots
    _pycache(b, "b.pyc")
    cache_a = a / "__pycache__"
    cache_a.mkdir()
    target = cache_a / "a.pyc"
    target.write_bytes(b"a" * 16)

    policy = PolicyEngine([a, b])
    monkeypatch.setattr(pe.os.path, "islink", lambda p: str(p) == str(cache_a))

    verdict = policy.validate_path_chain(target, a)
    assert not verdict.allowed
    assert verdict.rule == "symlink_in_path"

    # And the plan builder must refuse the candidate entirely.
    from disk_doctor.core.clean_plan import PlanBuilder
    from disk_doctor.core.schemas import Candidate, FileRef

    ref = FileRef(path=str(target), size_bytes=16, mtime_ns=1, device=1, inode=1)
    cand = Candidate(
        path=str(cache_a), category="t", risk=RiskLevel.SAFE,
        size_bytes=16, file_count=1, files=(ref,),
    )
    plan = PlanBuilder(policy).build(str(a), [cand])
    assert plan.items == []


def test_nested_symlink_parent_rejected(two_roots, monkeypatch):
    import disk_doctor.core.policy_engine as pe

    a, _b = two_roots
    nested = a / "sub"
    cache = nested / "__pycache__"
    cache.mkdir(parents=True)
    target = cache / "x.pyc"
    target.write_bytes(b"x" * 8)

    policy = PolicyEngine([a])
    monkeypatch.setattr(pe.os.path, "islink", lambda p: str(p) == str(nested))

    verdict = policy.validate_path_chain(target, a)
    assert not verdict.allowed
    assert verdict.rule == "symlink_in_path"


def test_symlink_root_rejected(two_roots, monkeypatch):
    import disk_doctor.core.policy_engine as pe

    a, _b = two_roots
    policy = PolicyEngine([a])
    monkeypatch.setattr(pe.os.path, "islink", lambda p: str(p) == str(a))

    verdict = policy.validate_path_chain(a, a)
    assert not verdict.allowed
    assert verdict.rule == "symlink_root"


def test_reparse_parent_rejected(two_roots, monkeypatch):
    import disk_doctor.core.policy_engine as pe

    a, _b = two_roots
    cache = a / "__pycache__"
    cache.mkdir()
    target = cache / "a.pyc"
    target.write_bytes(b"a" * 8)

    policy = PolicyEngine([a])
    monkeypatch.setattr(pe, "is_reparse_point", lambda p: str(p) == str(cache))

    verdict = policy.validate_path_chain(target, a)
    assert not verdict.allowed
    assert verdict.rule == "reparse_in_path"


def test_resolved_path_must_remain_in_plan_root(two_roots, monkeypatch):
    import disk_doctor.core.policy_engine as pe

    a, b = two_roots
    cache = a / "__pycache__"
    cache.mkdir()
    target = cache / "a.pyc"
    target.write_bytes(b"a" * 8)
    outside = b / "elsewhere.pyc"
    outside.write_bytes(b"b" * 8)

    def fake_realpath(p, *args, **kwargs):
        text = str(p)
        # Only the cache subtree resolves elsewhere; the root stays canonical.
        if "__pycache__" in text:
            return str(outside)
        return os.path.abspath(text)

    policy = PolicyEngine([a, b])
    monkeypatch.setattr(pe.os.path, "realpath", fake_realpath)

    verdict = policy.validate_path_chain(target, a)
    assert not verdict.allowed
    assert verdict.rule == "resolved_outside_root"


def test_all_files_prevalidated_before_first_move(make_executor, vault, make_file, approve):
    """A plan with one bad file must move nothing at all."""

    cache = vault / "__pycache__"
    first = make_file(cache / "a.pyc", 16)
    second = make_file(cache / "b.pyc", 16)
    plan = _plan_for(vault, cache)

    second.write_bytes(b"x" * 64)  # changed after planning

    result = make_executor().execute(plan, approve(plan))

    assert first.exists(), "预检失败时不得移动任何文件"
    assert second.exists()
    assert result.blocked_reason == "preflight_failed:size_changed"
    assert result.outcomes == []


# --------------------------------------------------------------------------- #
# B. Temp/tmp generalisation (P0-2)
# --------------------------------------------------------------------------- #

def test_generic_temp_directory_not_cleanable(vault, policy):
    (vault / "Temp").mkdir()
    (vault / "Temp" / "a.bin").write_bytes(b"x" * 10)
    analyzer = CacheAnalyzer(policy)
    assert analyzer.discover([str(vault)]) == []


def test_generic_tmp_directory_not_cleanable(vault, policy):
    (vault / "tmp").mkdir()
    (vault / "tmp" / "a.bin").write_bytes(b"x" * 10)
    analyzer = CacheAnalyzer(policy)
    assert analyzer.discover([str(vault)]) == []


def test_temp_with_user_txt_not_cleanable(vault, policy):
    temp = vault / "Temp"
    temp.mkdir()
    (temp / "notes.txt").write_text("user data", encoding="utf-8")
    (temp / "data.csv").write_text("a,b\n", encoding="utf-8")

    analyzer = CacheAnalyzer(policy)
    plan = __import__(
        "disk_doctor.core.clean_plan", fromlist=["PlanBuilder"]
    ).PlanBuilder(policy).build(str(vault), analyzer.discover([str(vault)]))

    assert plan.items == []
    assert plan.total_files == 0
    assert (temp / "notes.txt").exists()


def test_unknown_cache_directory_not_cleanable(vault, policy):
    cache = vault / "cache"
    cache.mkdir()
    (cache / "blob.bin").write_bytes(b"x" * 10)
    analyzer = CacheAnalyzer(policy)
    assert analyzer.discover([str(vault)]) == []


def test_pycache_only_accepts_pyc(vault, policy):
    cache = vault / "__pycache__"
    cache.mkdir()
    (cache / "m.pyc").write_bytes(b"x" * 10)
    (cache / "m.py").write_text("print(1)", encoding="utf-8")
    (cache / "notes.txt").write_text("keep", encoding="utf-8")

    candidates = CacheAnalyzer(policy).discover([str(vault)])
    assert len(candidates) == 1
    names = {Path(f.path).name for f in candidates[0].files}
    assert names == {"m.pyc"}


def test_pytest_cache_requires_valid_tag(vault, policy):
    cache = vault / ".pytest_cache"
    cache.mkdir()
    (cache / "v" / "cache").mkdir(parents=True)
    (cache / "v" / "cache" / "nodeids").write_text("x", encoding="utf-8")
    (cache / "CACHEDIR.TAG").write_text(
        "Signature: 8a477f597d28d172789f06886806bc55\n# cache dir\n",
        encoding="utf-8",
    )
    assert has_valid_cachedir_tag(cache)

    candidates = CacheAnalyzer(policy).discover([str(vault)])
    assert len(candidates) == 1
    assert candidates[0].category.startswith("pytest")


def test_invalid_cache_tag_rejected(vault, policy):
    cache = vault / ".pytest_cache"
    cache.mkdir()
    (cache / "CACHEDIR.TAG").write_text("Signature: deadbeef\n", encoding="utf-8")
    assert not has_valid_cachedir_tag(cache)
    assert CacheAnalyzer(policy).discover([str(vault)]) == []


def test_cache_symlink_rejected(vault, policy, monkeypatch):
    import disk_doctor.tools.cache_analyzer as ca

    cache = vault / "__pycache__"
    cache.mkdir()
    (cache / "m.pyc").write_bytes(b"x" * 10)

    monkeypatch.setattr(ca.os.path, "islink", lambda p: str(p) == str(cache))
    assert CacheAnalyzer(policy).discover([str(vault)]) == []


# --------------------------------------------------------------------------- #
# C. exact plan-id confirmation (P0-3)
# --------------------------------------------------------------------------- #

def test_plan_id_same_between_preview_and_execute(vault, tmp_path):
    cache = vault / "__pycache__"
    cache.mkdir()
    (cache / "m.pyc").write_bytes(b"x" * 32)

    agent = DiskDoctorAgent.create(
        [str(vault)], home=tmp_path / "h", quarantine_dir=tmp_path / "q"
    )
    preview = agent.handle("清理", scope=str(vault))
    stored = agent.plan_store.load(preview.plan_id)

    assert stored.plan_id == preview.plan_id
    assert stored.canonical_json() == agent.context.get_plan(preview.plan_id).canonical_json()


def test_plan_store_roundtrip(vault, tmp_path):
    cache = vault / "__pycache__"
    cache.mkdir()
    (cache / "m.pyc").write_bytes(b"x" * 32)
    plan = _plan_for(vault, cache)

    store = PlanStore(tmp_path / "plans")
    store.save(plan)
    loaded = store.load(plan.plan_id)

    assert loaded.plan_id == plan.plan_id
    assert loaded.canonical_json() == plan.canonical_json()


@pytest.mark.parametrize("bad", ["", "abc", "A" * 64, "0" * 63, "0" * 65, "../escape"])
def test_plan_store_rejects_invalid_id(tmp_path, bad):
    store = PlanStore(tmp_path / "plans")
    with pytest.raises(PlanStoreError) as exc:
        store.load(bad)
    assert exc.value.code == "invalid_plan_id"


def test_plan_store_rejects_expired_plan(vault, tmp_path):
    cache = vault / "__pycache__"
    cache.mkdir()
    (cache / "m.pyc").write_bytes(b"x" * 8)
    plan = _plan_for(vault, cache, ttl=1)

    store = PlanStore(tmp_path / "plans")
    store.save(plan)
    with pytest.raises(PlanStoreError) as exc:
        store.load(plan.plan_id, now=utc_now() + timedelta(seconds=60))
    assert exc.value.code == "plan_expired"


def test_plan_store_rejects_consumed_plan(vault, tmp_path):
    cache = vault / "__pycache__"
    cache.mkdir()
    (cache / "m.pyc").write_bytes(b"x" * 8)
    plan = _plan_for(vault, cache)

    store = PlanStore(tmp_path / "plans")
    store.save(plan)
    store.mark_consumed(plan.plan_id)

    with pytest.raises(PlanStoreError) as exc:
        store.load(plan.plan_id)
    assert exc.value.code == "plan_consumed"


def test_wrong_plan_id_confirmation_rejected(authority, vault):
    cache = vault / "__pycache__"
    cache.mkdir()
    (cache / "m.pyc").write_bytes(b"x" * 8)
    plan = _plan_for(vault, cache)

    with pytest.raises(ApprovalError) as exc:
        authority.issue(plan, confirmation="EXECUTE " + "0" * 64)
    assert exc.value.code == "confirmation_mismatch"


def test_partial_plan_id_confirmation_rejected(authority, vault):
    cache = vault / "__pycache__"
    cache.mkdir()
    (cache / "m.pyc").write_bytes(b"x" * 8)
    plan = _plan_for(vault, cache)

    with pytest.raises(ApprovalError) as exc:
        authority.issue(plan, confirmation=f"EXECUTE {plan.plan_id[:16]}")
    assert exc.value.code == "confirmation_mismatch"


def test_old_approval_cannot_authorize_new_plan(authority, vault):
    cache = vault / "__pycache__"
    cache.mkdir()
    (cache / "m.pyc").write_bytes(b"x" * 8)
    plan_a = _plan_for(vault, cache)

    receipt = authority.issue(plan_a, confirmation=f"EXECUTE {plan_a.plan_id}")

    (cache / "n.pyc").write_bytes(b"y" * 8)
    plan_b = _plan_for(vault, cache)
    assert plan_b.plan_id != plan_a.plan_id

    with pytest.raises(ApprovalError) as exc:
        authority.verify(receipt, plan_b)
    assert exc.value.code == "approval_plan_mismatch"


def test_approval_is_single_use(authority, vault):
    cache = vault / "__pycache__"
    cache.mkdir()
    (cache / "m.pyc").write_bytes(b"x" * 8)
    plan = _plan_for(vault, cache)

    receipt = authority.issue(plan, confirmation=f"EXECUTE {plan.plan_id}")
    authority.verify(receipt, plan)
    authority.consume(receipt)

    with pytest.raises(ApprovalError) as exc:
        authority.verify(receipt, plan)
    assert exc.value.code == "approval_already_used"


def test_second_confirmation_required(authority, vault, make_file):
    cache = vault / "__pycache__"
    refs = []
    for i in range(51):
        p = make_file(cache / f"f{i}.pyc", 1)
        refs.append(p)

    from disk_doctor.core.schemas import FileRef

    file_refs = tuple(
        FileRef(
            path=str(p),
            size_bytes=p.stat().st_size,
            mtime_ns=p.stat().st_mtime_ns,
            device=p.stat().st_dev,
            inode=p.stat().st_ino,
        )
        for p in refs
    )
    item = PlanItem(
        item_id="I001-abc123", path=str(cache), category="t", risk=RiskLevel.SAFE,
        size_bytes=51, file_count=51, files=file_refs,
    )
    plan = CleanPlan(root=str(vault), items=[item]).seal()
    assert plan.requires_second_confirm

    with pytest.raises(ApprovalError) as exc:
        authority.issue(plan, confirmation=f"EXECUTE {plan.plan_id}")
    assert exc.value.code == "second_confirmation_required"


def test_noninteractive_execution_rejected(vault, tmp_path, capsys):
    cache = vault / "__pycache__"
    cache.mkdir()
    (cache / "m.pyc").write_bytes(b"x" * 8)

    home = ["--home", str(tmp_path / "h")]
    assert (
        cli_main([*home, "--json", "plan", "--root", str(vault), "--allow-root", str(vault)])
        == 0
    )
    import json as _json

    plan_id = _json.loads(capsys.readouterr().out)["plan_id"]

    code = cli_main(
        [*home, "execute", "--plan-id", plan_id, "--allow-root", str(vault)]
    )
    assert code == 2
    assert "交互式终端" in capsys.readouterr().err
    assert (cache / "m.pyc").exists()


def test_yes_flag_cannot_bypass_plan_confirmation(vault, tmp_path, capsys):
    cache = vault / "__pycache__"
    cache.mkdir()
    (cache / "m.pyc").write_bytes(b"x" * 8)

    code = cli_main(
        [
            "--home", str(tmp_path / "h"),
            "clean",
            "--root", str(vault),
            "--allow-root", str(vault),
            "--no-dry-run",
            "--yes",
        ]
    )
    assert code == 0
    assert (cache / "m.pyc").exists()
    assert "已弃用" in capsys.readouterr().err


def test_llm_cannot_issue_confirmation(vault, tmp_path):
    agent = DiskDoctorAgent.create([str(vault)], home=tmp_path / "h")
    assert not hasattr(agent.llm, "issue")
    assert not hasattr(agent.llm, "approve")
    assert not isinstance(agent.llm, ConfirmationAuthority)


def test_llm_cannot_call_safe_executor(vault, tmp_path):
    agent = DiskDoctorAgent.create([str(vault)], home=tmp_path / "h")
    assert not hasattr(agent.llm, "execute")
    assert not hasattr(agent.llm, "tools")


# --------------------------------------------------------------------------- #
# D. quarantine probe (P0-4)
# --------------------------------------------------------------------------- #

def test_quarantine_probe_preserves_existing_write_probe(tmp_path):
    directory = tmp_path / "q"
    directory.mkdir()
    legacy = directory / ".write_probe"
    legacy.write_text("user-owned-data", encoding="utf-8")

    backend = QuarantineBackend(directory)
    assert backend.is_available() is True

    assert legacy.exists()
    assert legacy.read_text(encoding="utf-8") == "user-owned-data"


def test_quarantine_probe_uses_unique_filename(tmp_path):
    directory = tmp_path / "q"
    directory.mkdir()
    backend = QuarantineBackend(directory)

    before = set(os.listdir(directory))
    backend.is_available()
    backend.is_available()
    after = set(os.listdir(directory))

    assert before == after, "探测不应留下任何文件"


def test_quarantine_probe_cleans_only_its_own_file(tmp_path):
    directory = tmp_path / "q"
    directory.mkdir()
    keeper = directory / "keep.txt"
    keeper.write_text("keep", encoding="utf-8")

    QuarantineBackend(directory).is_available()

    assert keeper.exists()
    assert keeper.read_text(encoding="utf-8") == "keep"


def test_quarantine_probe_failure_preserves_existing_files(tmp_path, monkeypatch):
    directory = tmp_path / "q"
    directory.mkdir()
    keeper = directory / ".write_probe"
    keeper.write_text("user-owned-data", encoding="utf-8")

    import disk_doctor.core.safe_executor as se

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(se.tempfile, "mkstemp", boom)

    assert QuarantineBackend(directory).is_available() is False
    assert keeper.read_text(encoding="utf-8") == "user-owned-data"


# --------------------------------------------------------------------------- #
# E. space accounting (P1-1)
# --------------------------------------------------------------------------- #

def test_moved_bytes_not_reported_as_freed_space(make_executor, vault, make_file, approve):
    cache = vault / "__pycache__"
    make_file(cache / "m.pyc", 256)
    plan = _plan_for(vault, cache)

    from disk_doctor.core.validator import Validator

    result = make_executor().execute(plan, approve(plan))
    verification = Validator().verify(plan, result)

    assert verification.bytes_moved_to_trash == 256
    assert not hasattr(verification, "released_bytes")


def test_recycle_bin_does_not_assume_free_space_increase(make_executor, vault, make_file, approve):
    from disk_doctor.core.validator import Validator

    cache = vault / "__pycache__"
    make_file(cache / "m.pyc", 128)
    plan = _plan_for(vault, cache)

    result = make_executor().execute(plan, approve(plan))
    verification = Validator().verify(plan, result)

    assert any("不等于释放空间" in n for n in verification.notes)


def test_available_space_delta_is_measured_separately(make_executor, vault, make_file, approve):
    cache = vault / "__pycache__"
    make_file(cache / "m.pyc", 64)
    plan = _plan_for(vault, cache)

    result = make_executor().execute(plan, approve(plan))
    assert result.free_space_before is not None
    assert result.free_space_after is not None
    assert result.available_space_change_bytes == (
        result.free_space_after - result.free_space_before
    )


def test_negative_free_space_delta_preserved():
    from disk_doctor.core.schemas import ExecutionResult
    from disk_doctor.core.validator import Validator

    plan = CleanPlan(root="C:/x", items=[]).seal()
    result = ExecutionResult(
        plan_id=plan.plan_id,
        estimated_bytes=10,
        bytes_moved_to_trash=10,
        free_space_before=1000,
        free_space_after=900,
    )
    verification = Validator().verify(plan, result)
    assert verification.available_space_change_bytes == -100
    assert any("减少" in n for n in verification.notes)


# --------------------------------------------------------------------------- #
# F. Windows path parsing (P1-2)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "text,expected",
    [
        (r"清理 D:\work\temp", r"D:\work\temp"),
        ("清理 D:/work/temp", r"D:\work\temp"),
        ("清理 D:/", "D:\\"),
        ("清理 D盘", "D:\\"),
    ],
)
def test_windows_path_forms(text, expected):
    assert IntentRouter.extract_scope(text) == expected


def test_windows_drive_path_parsing():
    assert normalize_windows_path("D", r"\work\temp") == r"D:\work\temp"


def test_windows_forward_slash_parsing():
    assert IntentRouter.extract_scope("清 D:/work/temp") == r"D:\work\temp"


def test_windows_drive_root_parsing():
    assert IntentRouter.extract_scope("清 D:/") == "D:\\"


def test_chinese_drive_name_parsing():
    assert IntentRouter.extract_scope("C盘满了") == "C:\\"


def test_unix_path_parsing():
    assert IntentRouter.extract_scope("清理 /var/tmp/x") == "/var/tmp/x"
