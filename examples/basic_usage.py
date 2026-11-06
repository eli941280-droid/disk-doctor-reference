"""Embedding the agent in your own code.

Shows the three things a library user usually needs:

1. constructing an agent against an explicitly authorised directory;
2. the two-phase flow — plan, then execute with the exact plan id;
3. reading structured results instead of parsing printed text.

Run:
    python examples/basic_usage.py [target-directory]

Without an argument it uses a temporary sandbox so nothing real is touched.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))

from disk_doctor.agent.orchestrator import DiskDoctorAgent  # noqa: E402
from disk_doctor.core.schemas import Intent  # noqa: E402
from disk_doctor.llm import build_llm  # noqa: E402


def make_agent(target: Path, home: Path) -> DiskDoctorAgent:
    # provider="auto" -> real model if a key is configured, otherwise MockLLM.
    llm = build_llm("auto")
    return DiskDoctorAgent.create(
        [str(target)],
        home=home,
        quarantine_dir=home / "quarantine",
        llm=llm,
    )


def main(argv: list[str]) -> int:
    sandbox = None
    if argv:
        target = Path(argv[0]).expanduser().resolve()
        if not target.is_dir():
            print(f"目录不存在：{target}")
            return 2
        home = target / ".disk_doctor_demo"
    else:
        sandbox = Path(tempfile.mkdtemp(prefix="disk-doctor-basic-"))
        cache = sandbox / "__pycache__"
        cache.mkdir()
        (cache / "junk.cpython-311.pyc").write_bytes(b"\0" * 8192)
        target = sandbox
        home = sandbox / ".state"

    try:
        agent = make_agent(target, home)
        print(f"范围：{target}")
        print(f"模型：{agent.llm.describe()}")

        turn = agent.handle("看看磁盘", scope=str(target))
        assert turn.intent is Intent.DIAGNOSE
        print(f"\n健康评分：{turn.data['health_score']}/100")

        # Phase 1: produce and persist a plan. Nothing is moved.
        plan_turn = agent.handle("清理", scope=str(target))
        print(f"\nplan_id：{plan_turn.plan_id}")
        print(f"计划文件数：{plan_turn.data['total_files']}")

        stored = agent.plan_store.load(plan_turn.plan_id)
        for item in stored.items:
            for ref in item.files:
                print(f"  {ref.size_bytes:>8}  {ref.path}")

        # Phase 2: execute the *stored* plan, bound to its exact id.
        executed = agent.execute_plan(
            plan_turn.plan_id,
            confirmation=f"EXECUTE {plan_turn.plan_id}",
        )
        result = executed.data.get("result", {})
        print(
            f"\n估计处理：{result.get('estimated_bytes', 0)} 字节"
            f"　实际移入回收站：{result.get('bytes_moved_to_trash', 0)} 字节"
        )
        print(f"磁盘可用空间变化：{result.get('available_space_change_bytes')} 字节")
        return 0
    finally:
        if sandbox is not None:
            shutil.rmtree(sandbox, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
