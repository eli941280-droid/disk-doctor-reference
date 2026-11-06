"""Verification: the three quantities must never be conflated."""

from __future__ import annotations

from pathlib import Path

from disk_doctor.core.schemas import (
    CleanPlan,
    ExecutionResult,
    ItemOutcome,
    ItemStatus,
    PlanItem,
    RiskLevel,
)
from disk_doctor.core.utils import collect_files
from disk_doctor.core.validator import Validator


class _NoopTrash:
    """Reports success but leaves the file in place."""

    name = "noop"

    def is_available(self) -> bool:
        return True

    def move(self, path: Path) -> None:
        return None


def _build(root: Path, directory: Path) -> CleanPlan:
    refs, total = collect_files(directory)
    item = PlanItem(
        item_id="I001-abc123",
        path=str(directory),
        category="t",
        risk=RiskLevel.SAFE,
        size_bytes=total,
        file_count=len(refs),
        files=refs,
    )
    return CleanPlan(root=str(root), items=[item]).seal()


def test_blocked_run_reports_no_change(make_executor, vault, make_file):
    cache = vault / "__pycache__"
    make_file(cache / "a.pyc", 100)
    plan = _build(vault, cache)

    result = make_executor().execute(plan, None)
    verification = Validator().verify(plan, result)

    assert verification.bytes_moved_to_trash == 0
    assert any("阻断" in n for n in verification.notes)


def test_moved_bytes_are_measured(make_executor, vault, make_file, approve):
    cache = vault / "__pycache__"
    make_file(cache / "a.pyc", 512)
    plan = _build(vault, cache)

    result = make_executor().execute(plan, approve(plan))
    verification = Validator().verify(plan, result)

    assert verification.estimated_bytes == 512
    assert verification.bytes_moved_to_trash == 512
    assert verification.ok


def test_available_space_delta_is_measured_separately(
    make_executor, vault, make_file, approve
):
    cache = vault / "__pycache__"
    make_file(cache / "a.pyc", 512)
    plan = _build(vault, cache)

    result = make_executor().execute(plan, approve(plan))
    assert result.free_space_before is not None
    assert result.free_space_after is not None

    verification = Validator().verify(plan, result)
    assert verification.available_space_change_bytes == (
        result.free_space_after - result.free_space_before
    )


def test_moved_bytes_not_reported_as_freed_space():
    """A recycle-bin move must never be described as freed disk space."""

    plan = CleanPlan(root="C:/x", items=[]).seal()
    result = ExecutionResult(
        plan_id=plan.plan_id,
        estimated_bytes=1000,
        bytes_moved_to_trash=1000,
        free_space_before=5000,
        free_space_after=5000,
    )
    verification = Validator().verify(plan, result)

    assert verification.bytes_moved_to_trash == 1000
    assert verification.available_space_change_bytes == 0
    assert not hasattr(verification, "freed_bytes")
    assert any("不等于释放空间" in n for n in verification.notes)


def test_negative_free_space_delta_preserved():
    plan = CleanPlan(root="C:/x", items=[]).seal()
    result = ExecutionResult(
        plan_id=plan.plan_id,
        estimated_bytes=100,
        bytes_moved_to_trash=100,
        free_space_before=10_000,
        free_space_after=9_000,
    )
    verification = Validator().verify(plan, result)

    assert verification.available_space_change_bytes == -1000
    assert any("减少" in n for n in verification.notes)


def test_residual_and_failures_are_surfaced(make_executor, vault, make_file, approve):
    cache = vault / "__pycache__"
    make_file(cache / "a.pyc", 40)
    plan = _build(vault, cache)

    class _Broken:
        name = "broken"

        def is_available(self) -> bool:
            return True

        def move(self, path: Path) -> None:
            raise OSError("nope")

    result = make_executor(trash=_Broken()).execute(plan, approve(plan))
    verification = Validator().verify(plan, result)

    assert verification.bytes_moved_to_trash == 0
    assert any("失败" in n for n in verification.notes)


def test_outcome_dataclass_carries_bytes():
    outcome = ItemOutcome(
        item_id="I001#1", path="x", status=ItemStatus.MOVED_TO_TRASH, bytes_moved=7
    )
    assert outcome.bytes_moved == 7
