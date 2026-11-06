"""Post-execution verification.

The rule this module enforces: **three different numbers must never be merged
into one.**

* ``estimated_bytes`` — what the plan said it would touch;
* ``bytes_moved_to_trash`` — what actually landed in the recycle bin;
* ``available_space_change_bytes`` — what the filesystem reports before/after.

Moving a file into the recycle bin does not free space; the file is still on
disk until the bin is emptied. v0.2.0 printed the second number labelled as
"释放空间", which was simply wrong (P1-1).

A negative free-space delta is legitimate: other processes write to the disk
while we work. It is reported as-is, never clamped.
"""

from __future__ import annotations

from .schemas import CleanPlan, ExecutionResult, Verification


class Validator:
    """Reconciles a plan against what actually happened on disk."""

    def __init__(self, *, tolerance_bytes: int = 0) -> None:
        self.tolerance_bytes = tolerance_bytes

    def verify(self, plan: CleanPlan, result: ExecutionResult) -> Verification:
        estimated = plan.total_bytes
        moved = result.bytes_moved_to_trash
        delta = result.available_space_change_bytes

        notes: list[str] = []

        if result.blocked_reason:
            return Verification(
                estimated_bytes=estimated,
                bytes_moved_to_trash=0,
                available_space_change_bytes=delta,
                ok=True,
                notes=[f"执行被阻断（{result.blocked_reason}），磁盘未发生变化"],
            )

        if result.failure_count:
            notes.append(f"{result.failure_count} 个文件失败，未计入移入量")
        if result.skipped_count:
            notes.append(f"{result.skipped_count} 个文件跳过，未计入移入量")

        ok = True
        if estimated and moved > estimated * 2:
            notes.append("移入回收站的总量超过计划估算两倍，建议人工核查")
            ok = False

        if delta is None:
            notes.append("未能测量磁盘可用空间变化（文件系统调用失败）")
        elif delta < 0:
            notes.append(
                "磁盘可用空间在本次操作期间减少；回收站中的文件仍占用空间，"
                "且其他进程可能同时在写入。这不代表清理失败"
            )
        elif delta == 0 and moved > 0:
            notes.append("文件已移入回收站，但可用空间没有变化（需清空回收站才会释放）")

        if moved > 0:
            notes.append(
                "移入回收站不等于释放空间：清空回收站后磁盘可用空间才会真正增加"
            )

        return Verification(
            estimated_bytes=estimated,
            bytes_moved_to_trash=moved,
            available_space_change_bytes=delta,
            ok=ok,
            notes=notes,
        )
