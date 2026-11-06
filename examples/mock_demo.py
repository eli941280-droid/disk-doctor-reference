"""End-to-end demo that runs with no API key and touches nothing real.

Everything happens inside a temporary directory that is deleted on exit. The
"recycle bin" is a local quarantine folder, so even the destructive step is
reversible and inspectable.

v0.2.1 flow: plan → exact-id confirmation → execute. There is no "just do it"
shortcut, which is the point of the demo.

Run:
    python examples/mock_demo.py
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
from disk_doctor.core.utils import human_bytes  # noqa: E402

BAR = "=" * 62


def build_sandbox(root: Path) -> None:
    """Create a believable, entirely fake disk layout."""

    files = {
        "__pycache__/session.cpython-311.pyc": 4 * 1024 * 1024,
        "__pycache__/log.cpython-311.pyc": 2 * 1024 * 1024,
        "project/__pycache__/module.cpython-311.pyc": 512 * 1024,
        "Documents/thesis.docx": 900 * 1024,  # protected on purpose
        "Temp/notes.txt": 1024,  # NOT cleanable any more (P0-2)
        "Downloads/very-large-video.mp4": 6 * 1024 * 1024,
    }
    for rel, size in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"\0" * size)


def main() -> int:
    sandbox = Path(tempfile.mkdtemp(prefix="disk-doctor-demo-"))
    try:
        build_sandbox(sandbox)

        agent = DiskDoctorAgent.create(
            [str(sandbox)],
            home=sandbox / ".state",
            quarantine_dir=sandbox / ".quarantine",
        )

        print(BAR)
        print("1. 诊断（只读）")
        print(BAR)
        print(agent.handle("看看我的磁盘状况", scope=str(sandbox)).message)

        print()
        print(BAR)
        print("2. 生成计划（仍然只读，且已持久化）")
        print(BAR)
        plan_turn = agent.handle("清理", scope=str(sandbox))
        print(plan_turn.message)

        print()
        print(BAR)
        print("3. 用错误的确认字符串执行 → 必须被拒绝")
        print(BAR)
        rejected = agent.execute_plan(plan_turn.plan_id, confirmation="yes")
        print(rejected.message)

        print()
        print(BAR)
        print("4. 用完整 plan_id 确认后执行（移入本地隔离区）")
        print(BAR)
        result = agent.execute_plan(
            plan_turn.plan_id,
            confirmation=f"EXECUTE {plan_turn.plan_id}",
        )
        print(result.message)

        print()
        print(BAR)
        print("5. 核对")
        print(BAR)
        thesis = sandbox / "Documents" / "thesis.docx"
        notes = sandbox / "Temp" / "notes.txt"
        print(f"  thesis.docx 仍存在：{thesis.exists()}（受保护类型）")
        print(f"  Temp/notes.txt 仍存在：{notes.exists()}（Temp 不再是清理目标）")
        print(f"  __pycache__/*.pyc 仍存在：{(sandbox / '__pycache__' / 'session.cpython-311.pyc').exists()}")
        quarantined = sorted(p.name for p in (sandbox / ".quarantine").iterdir())
        print(f"  隔离区文件数：{len(quarantined)}")
        for name in quarantined:
            print(f"    · {name}")

        print()
        print(BAR)
        print("6. 重复执行同一计划 → 必须被拒绝")
        print(BAR)
        again = agent.execute_plan(
            plan_turn.plan_id,
            confirmation=f"EXECUTE {plan_turn.plan_id}",
        )
        print(again.message)

        print()
        print(BAR)
        print("7. 成就统计（仅统计移入回收站的文件）")
        print(BAR)
        print(agent.handle("我的成就").message)

        total = sum(
            p.stat().st_size for p in (sandbox / ".quarantine").rglob("*") if p.is_file()
        )
        print()
        print(f"演示结束。实际移入隔离区的字节数：{human_bytes(total)}")
        print("注意：移入回收站不等于释放磁盘空间，清空回收站后可用空间才会增加。")
        return 0
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
