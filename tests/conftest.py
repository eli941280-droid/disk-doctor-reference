"""Shared fixtures. Every test operates inside ``tmp_path`` — never the real disk."""

from __future__ import annotations

from pathlib import Path

import pytest

from disk_doctor.core.clean_plan import ConfirmationAuthority
from disk_doctor.core.policy_engine import PolicyEngine
from disk_doctor.core.safe_executor import QuarantineBackend, SafeExecutor
from disk_doctor.core.schemas import Candidate, CleanPlan, FileRef, PlanItem, RiskLevel
from disk_doctor.core.utils import collect_files


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    """The only directory the policy engine is allowed to touch."""

    root = tmp_path / "allowed"
    root.mkdir()
    return root


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    root = tmp_path / "outside"
    root.mkdir()
    return root


@pytest.fixture
def policy(vault: Path) -> PolicyEngine:
    return PolicyEngine([vault])


@pytest.fixture
def quarantine(tmp_path: Path) -> QuarantineBackend:
    return QuarantineBackend(tmp_path / "quarantine")


@pytest.fixture
def make_file():
    def _make(path: Path, size: int = 128, fill: bytes = b"x") -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(fill * size)
        return path

    return _make


@pytest.fixture
def make_candidate():
    """Build a Candidate that carries its real file inventory."""

    def _make(
        directory: Path,
        *,
        category: str = "测试缓存",
        risk: RiskLevel = RiskLevel.SAFE,
    ) -> Candidate:
        directory.mkdir(parents=True, exist_ok=True)
        refs, total = collect_files(directory)
        return Candidate(
            path=str(directory),
            category=category,
            risk=risk,
            size_bytes=total,
            file_count=len(refs),
            reason="fixture",
            files=refs,
        )

    return _make


@pytest.fixture
def make_ref():
    """A FileRef that need not exist on disk (planning-only tests)."""

    def _make(path: Path, size: int = 16, mtime_ns: int = 1, inode: int = 1) -> FileRef:
        return FileRef(
            path=str(path),
            size_bytes=size,
            mtime_ns=mtime_ns,
            device=1,
            inode=inode,
        )

    return _make


@pytest.fixture
def make_plan():
    """Assemble and seal a plan by hand, for executor-level tests."""

    def _make(
        root: Path,
        files: tuple[FileRef, ...],
        *,
        item_dir: Path | None = None,
        ttl_seconds: int = 900,
        risk: RiskLevel = RiskLevel.SAFE,
        category: str = "测试缓存",
    ) -> CleanPlan:
        target_dir = item_dir or (Path(files[0].path).parent if files else root)
        item = PlanItem(
            item_id="I001-abc123",
            path=str(target_dir),
            category=category,
            risk=risk,
            size_bytes=sum(f.size_bytes for f in files),
            file_count=len(files),
            reason="fixture",
            files=tuple(files),
        )
        plan = CleanPlan(
            root=str(root),
            items=[item],
            ttl_seconds=ttl_seconds,
        )
        return plan.seal()

    return _make


@pytest.fixture
def authority() -> ConfirmationAuthority:
    # Deterministic secret: tests must not depend on process randomness.
    return ConfirmationAuthority(secret=b"unit-test-secret")


@pytest.fixture
def approve(authority):
    """Issue a valid receipt for a plan, as the CLI would after confirmation."""

    def _approve(plan: CleanPlan, *, second: bool = False):
        return authority.issue(
            plan,
            confirmation=f"EXECUTE {plan.plan_id}",
            second_confirmation=f"SECOND {plan.plan_id}" if second else None,
        )

    return _approve


@pytest.fixture
def make_executor(policy, quarantine, authority):
    def _make(*, trash=None, cleanable=None) -> SafeExecutor:
        return SafeExecutor(
            policy,
            trash or quarantine,
            authority=authority,
            cleanable=cleanable,
        )

    return _make
