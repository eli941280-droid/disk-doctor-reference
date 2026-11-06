"""Regression suite for the v0.2.0 security audit (BUG-01 … BUG-06).

Each test maps to one acceptance item. None of them skip: every branch below can
be exercised with a temporary directory or a monkeypatch, so a skip here would
mean an unverified safety claim.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from disk_doctor.agent.llm_contract import LLMContractError, parse_llm_intent
from disk_doctor.agent.orchestrator import DiskDoctorAgent
from disk_doctor.core.clean_plan import ConfirmationAuthority
from disk_doctor.core.policy_engine import PolicyEngine
from disk_doctor.core.safe_executor import SafeExecutor
from disk_doctor.core.schemas import (
    CleanPlan,
    PlanItem,
    RiskLevel,
)
from disk_doctor.core.utils import collect_files, utc_now
from disk_doctor.reporting.preview import render_preview
from disk_doctor.tools.cache_analyzer import CacheAnalyzer

_AUTH = ConfirmationAuthority(secret=b"regression-suite")


def _receipt(plan, *, second=False):
    return _AUTH.issue(
        plan,
        confirmation=f"EXECUTE {plan.plan_id}",
        second_confirmation=f"SECOND {plan.plan_id}" if second else None,
    )


def _plan_for(root: Path, directory: Path, *, ttl=900, item_dir=None) -> CleanPlan:
    refs, total = collect_files(directory)
    item = PlanItem(
        item_id="I001-abc123",
        path=str(item_dir or directory),
        category="测试缓存",
        risk=RiskLevel.SAFE,
        size_bytes=total,
        file_count=len(refs),
        files=refs,
    )
    return CleanPlan(root=str(root), items=[item], ttl_seconds=ttl).seal()


# --------------------------------------------------------------------------- #
# BUG-01 — scanning must never leave the selected root
# --------------------------------------------------------------------------- #

def test_scan_only_selected_root(tmp_path):
    root_a = tmp_path / "A"
    root_b = tmp_path / "B"
    (root_a / "__pycache__").mkdir(parents=True)
    (root_b / "__pycache__").mkdir(parents=True)
    (root_a / "__pycache__" / "a.pyc").write_bytes(b"a" * 100)
    (root_b / "__pycache__" / "b.pyc").write_bytes(b"b" * 100)

    policy = PolicyEngine([root_a, root_b])
    analyzer = CacheAnalyzer(policy)

    candidates = analyzer.discover([root_a])
    scanned = {ref.path for c in candidates for ref in c.files}

    assert any("a.pyc" in p for p in scanned), "目标根目录内的缓存必须被发现"
    assert not any("b.pyc" in p for p in scanned), "不得扫描未选中的根目录"


# --------------------------------------------------------------------------- #
# BUG-02 — execution must re-check root, scope and file identity
# --------------------------------------------------------------------------- #

def test_executor_rejects_item_outside_plan_root(tmp_path, quarantine):
    root_a = tmp_path / "A"
    root_b = tmp_path / "B"
    (root_a / "__pycache__").mkdir(parents=True)
    (root_b / "__pycache__").mkdir(parents=True)
    (root_b / "__pycache__" / "b.pyc").write_bytes(b"b" * 64)

    policy = PolicyEngine([root_a, root_b])
    plan = _plan_for(root_a, root_b / "__pycache__")

    result = SafeExecutor(policy, quarantine, authority=_AUTH).execute(plan, _receipt(plan))

    assert (root_b / "__pycache__" / "b.pyc").exists()
    assert result.blocked_reason == "preflight_failed:outside_plan_root"


def test_file_changed_after_plan(tmp_path, quarantine):
    root = tmp_path / "A"
    cache = root / "__pycache__"
    cache.mkdir(parents=True)
    target = cache / "a.pyc"
    target.write_bytes(b"x" * 100)

    policy = PolicyEngine([root])
    plan = _plan_for(root, cache)

    target.write_bytes(b"x" * 400)  # size changed after review

    result = SafeExecutor(policy, quarantine, authority=_AUTH).execute(plan, _receipt(plan))

    assert target.exists()
    assert result.blocked_reason == "preflight_failed:size_changed"


def test_symlink_rejected(policy, vault, make_file, monkeypatch):
    import disk_doctor.core.policy_engine as policy_module

    target = make_file(vault / "looks_normal.tmp", 10)
    monkeypatch.setattr(
        policy_module.os.path, "islink", lambda p: str(p) == str(target)
    )
    verdict = policy.verdict(target)
    assert not verdict.allowed
    assert verdict.rule == "symlink_denied"


def test_reparse_point_rejected(policy, vault, make_file, monkeypatch):
    import disk_doctor.core.policy_engine as policy_module

    target = make_file(vault / "junction_target.tmp", 10)
    monkeypatch.setattr(policy_module, "is_reparse_point", lambda p: True)
    verdict = policy.verdict(target)
    assert not verdict.allowed
    assert verdict.rule == "reparse_point_denied"


def test_protected_suffix_rejected(policy, vault, make_file):
    doc = make_file(vault / "thesis.docx", 10)
    assert policy.verdict(doc).rule == "protected_suffix"

    archive = make_file(vault / "backup.zip", 10)
    assert policy.verdict(archive).rule == "protected_suffix"


# --------------------------------------------------------------------------- #
# BUG-03 — preview must show the actual files
# --------------------------------------------------------------------------- #

def test_preview_contains_full_file_paths(tmp_path):
    root = tmp_path / "A"
    cache = root / "__pycache__"
    cache.mkdir(parents=True)
    files = [cache / f"f{i}.tmp" for i in range(3)]
    for f in files:
        f.write_bytes(b"y" * 32)

    plan = _plan_for(root, cache)
    text = render_preview(plan)

    for f in files:
        assert str(f) in text, f"预览必须列出 {f}"
    assert plan.plan_id in text
    assert "总文件数量" in text
    assert "需要二次确认" in text


# --------------------------------------------------------------------------- #
# BUG-04 — plan identity is a full SHA-256 over canonical content
# --------------------------------------------------------------------------- #

def test_plan_id_full_sha256(tmp_path):
    root = tmp_path / "A"
    cache = root / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "a.pyc").write_bytes(b"z" * 10)

    plan = _plan_for(root, cache)
    assert len(plan.plan_id) == 64
    int(plan.plan_id, 16)  # valid hex
    ok, reason = plan.verify_integrity()
    assert ok, reason


def test_modified_plan_rejected(tmp_path, quarantine):
    root = tmp_path / "A"
    cache = root / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "a.pyc").write_bytes(b"z" * 10)

    policy = PolicyEngine([root])
    plan = _plan_for(root, cache)

    # PlanItem is frozen, so tamper by replacing the item.
    plan.items[0] = plan.items[0].model_copy(update={"size_bytes": 999_999})

    ok, reason = plan.verify_integrity()
    assert not ok and reason == "plan_digest_mismatch"

    result = SafeExecutor(policy, quarantine, authority=_AUTH).execute(plan, _receipt(plan))
    assert result.blocked_reason == "plan_digest_mismatch"
    assert (cache / "a.pyc").exists()


# --------------------------------------------------------------------------- #
# BUG-05 — stale plans must not execute
# --------------------------------------------------------------------------- #

def test_expired_plan_rejected(tmp_path, quarantine):
    root = tmp_path / "A"
    cache = root / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "a.pyc").write_bytes(b"z" * 10)

    policy = PolicyEngine([root])
    plan = _plan_for(root, cache, ttl=0)

    result = SafeExecutor(policy, quarantine, authority=_AUTH).execute(
        plan, _receipt(plan), now=utc_now() + timedelta(seconds=5)
    )

    assert result.blocked_reason == "plan_expired"
    assert (cache / "a.pyc").exists()


# --------------------------------------------------------------------------- #
# BUG-06 — path comparison is component-wise
# --------------------------------------------------------------------------- #

def test_path_boundary_work_vs_workshop(tmp_path):
    work = tmp_path / "work"
    workshop = tmp_path / "workshop"
    work.mkdir()
    workshop.mkdir()

    policy = PolicyEngine([work])

    assert policy.contains(work / "sub")
    assert not policy.contains(workshop)
    assert not policy.contains(workshop / "file.pyc")
    assert not policy.verdict(workshop / "file.pyc").allowed


# --------------------------------------------------------------------------- #
# Section 5 — the model has no execution authority
# --------------------------------------------------------------------------- #

def test_no_llm_execution_authority(vault, tmp_path, make_file):
    from disk_doctor.llm.base import LLMClient

    class EvilLLM(LLMClient):
        name = "evil"
        model = "evil"

        def complete(self, system: str, user: str, *, temperature=None) -> str:
            return '{"intent":"plan","command":"rm -rf /","paths":["/etc/passwd"]}'

    cache = vault / "__pycache__"
    make_file(cache / "junk.pyc", 64)

    agent = DiskDoctorAgent.create(
        [str(vault)], home=tmp_path / "h", quarantine_dir=tmp_path / "q", llm=EvilLLM()
    )
    turn = agent.handle("清理", scope=str(vault), narrate=True)

    # The hostile payload is rejected by the contract…
    with pytest.raises(LLMContractError):
        parse_llm_intent('{"intent":"plan","command":"rm -rf /"}')

    # …and nothing was executed even though the model "asked" for it.
    assert (cache / "junk.pyc").exists()
    assert turn.awaiting_confirmation is True
    assert not hasattr(agent.llm, "tools")
    assert not hasattr(agent.llm, "execute")


def test_llm_contract_rejects_unknown_intent():
    with pytest.raises(LLMContractError):
        parse_llm_intent('{"intent":"delete_everything"}')

    with pytest.raises(LLMContractError):
        parse_llm_intent("not json at all")


def test_llm_contract_accepts_minimal_payload():
    result = parse_llm_intent('{"intent":"diagnose"}')
    assert result.intent.value == "diagnose"


# --------------------------------------------------------------------------- #
# Section 3.2 — one writer for user data
# --------------------------------------------------------------------------- #

def test_only_one_user_data_writer(vault, tmp_path):
    """No registry entry writes, and exactly one module calls ``trash.move``.

    The second half is a source scan rather than a runtime check: the point is
    that no *other* module in the package has a call site that moves a file.
    """

    import pathlib

    agent = DiskDoctorAgent.create([str(vault)], home=tmp_path / "h")
    caps = agent.capabilities()
    assert [t for t in caps["tools"] if not t["read_only"]] == []
    assert caps["write_tools"] == []

    package = pathlib.Path(__file__).resolve().parents[1] / "src" / "disk_doctor"
    call_sites = {
        p.relative_to(package).as_posix()
        for p in package.rglob("*.py")
        if ".move(" in p.read_text(encoding="utf-8")
    }
    assert call_sites == {"core/safe_executor.py"}


# --------------------------------------------------------------------------- #
# Section 6 — fail closed
# --------------------------------------------------------------------------- #

def test_trash_failure_closed(tmp_path):
    root = tmp_path / "A"
    cache = root / "__pycache__"
    cache.mkdir(parents=True)
    target = cache / "a.pyc"
    target.write_bytes(b"z" * 10)

    class _Dead:
        name = "dead"

        def is_available(self) -> bool:
            return False

        def move(self, path: Path) -> None:  # pragma: no cover
            raise AssertionError("must not be called")

    policy = PolicyEngine([root])
    plan = _plan_for(root, cache)
    result = SafeExecutor(policy, _Dead(), authority=_AUTH).execute(plan, _receipt(plan))

    assert result.blocked_reason == "trash_unavailable"
    assert target.exists()


def test_second_confirmation_required(policy, vault, quarantine, make_ref):
    """A large plan cannot even obtain a receipt without the second token."""

    from disk_doctor.core.clean_plan import ApprovalError

    directory = vault / "bulk"
    directory.mkdir()
    refs = tuple(make_ref(directory / f"f{i}.tmp", 1, inode=i + 1) for i in range(51))
    item = PlanItem(
        item_id="I001-abc123",
        path=str(directory),
        category="t",
        risk=RiskLevel.SAFE,
        size_bytes=51,
        file_count=51,
        files=refs,
    )
    plan = CleanPlan(root=str(vault), items=[item]).seal()
    assert plan.requires_second_confirm

    try:
        _AUTH.issue(plan, confirmation=f"EXECUTE {plan.plan_id}")
    except ApprovalError as exc:
        assert exc.code == "second_confirmation_required"
    else:  # pragma: no cover - the guard must fire
        raise AssertionError("second confirmation was not enforced")

    receipt = _AUTH.issue(
        plan,
        confirmation=f"EXECUTE {plan.plan_id}",
        second_confirmation=f"SECOND {plan.plan_id}",
    )
    assert receipt.second_confirmed is True


def test_exact_plan_confirmation(vault, tmp_path, make_file):
    """A plan is single-use, and confirmation never leaks to another plan."""

    cache = vault / "__pycache__"
    make_file(cache / "mod.cpython-311.pyc", 32)

    agent = DiskDoctorAgent.create(
        [str(vault)], home=tmp_path / "h", quarantine_dir=tmp_path / "q"
    )
    first = agent.handle("清理", scope=str(vault))
    assert first.plan_id

    wrong = agent.execute_plan(first.plan_id, confirmation="yes")
    assert (cache / "mod.cpython-311.pyc").exists()
    assert wrong.data["error"] == "confirmation_mismatch"

    executed = agent.execute_plan(
        first.plan_id, confirmation=f"EXECUTE {first.plan_id}"
    )
    assert executed.data["result"]["plan_id"] == first.plan_id

    again = agent.execute_plan(first.plan_id, confirmation=f"EXECUTE {first.plan_id}")
    assert again.data["error"] == "plan_consumed"
