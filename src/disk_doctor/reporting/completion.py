"""Completion and error reporting.

Wording rules enforced here:

* estimates carry 约/预计; measurements do not;
* **three quantities are reported separately** — estimated file size, bytes
  actually moved to trash, and the measured change in free space;
* "释放空间" is never printed for a recycle-bin move. The file still occupies
  disk until the bin is emptied (P1-1);
* a blocked run says blocked, not "completed with 0".
"""

from __future__ import annotations

from ..core.schemas import ExecutionResult, ItemStatus, OutcomeStatus, Verification
from ..core.utils import human_bytes

_STATUS_TITLE = {
    OutcomeStatus.COMPLETED: "✨ 执行完成",
    OutcomeStatus.PARTIAL: "⚠️ 部分完成",
    OutcomeStatus.FAILED: "❌ 执行未成功",
    OutcomeStatus.BLOCKED: "🛡️ 操作已阻断",
}

_BLOCK_HINT = {
    "not_confirmed": "未获得确认，未改动任何文件。",
    "plan_expired": "计划已过期，请重新生成计划。",
    "plan_digest_mismatch": "计划内容与摘要不一致，已拒绝执行。",
    "plan_schema_outdated": "计划由旧版本生成，已拒绝执行。",
    "policy_version_mismatch": "安全策略已升级，旧计划需重新生成。",
    "trash_unavailable": "回收站不可用，已停止。本项目不会退化为永久删除。",
}


def _space_line(delta: int | None) -> str:
    if delta is None:
        return "磁盘可用空间变化：未能测量"
    sign = "+" if delta >= 0 else "-"
    return f"磁盘可用空间变化：{sign}{human_bytes(abs(delta))}（实测，可能为负）"


def render_completion(
    result: ExecutionResult,
    verification: Verification,
    *,
    achievements: dict | None = None,
    unlocked: list[str] | None = None,
) -> str:
    status = result.status()
    lines = [_STATUS_TITLE.get(status, "执行结果"), "━━━━━━━━━━━━━━━━━━"]

    if result.blocked_reason:
        reason = result.blocked_reason
        lines += ["", f"阻断原因：{reason}"]
        hint = _BLOCK_HINT.get(reason)
        if hint is None and reason.startswith("approval:"):
            hint = "审批凭证无效：必须是针对当前 plan_id 的一次性确认。"
        elif hint is None and reason.startswith("preflight_failed:"):
            hint = "执行前的整体校验未通过，未移动任何文件。"
        if hint:
            lines.append(hint)
        return "\n".join(lines)

    lines += [
        "",
        f"✅ 移入回收站：{result.success_count} 个文件",
        f"⏭️ 跳过：{result.skipped_count} 个",
        f"❌ 失败：{result.failure_count} 个",
        "",
        f"预计处理文件大小：约 {human_bytes(verification.estimated_bytes)}",
        f"实际移入回收站：{human_bytes(verification.bytes_moved_to_trash)}",
        _space_line(verification.available_space_change_bytes),
        "",
        "说明：移入回收站不等于释放磁盘空间，清空回收站后可用空间才会增加。",
    ]

    if result.failure_count:
        lines += ["", "失败明细："]
        for outcome in result.outcomes:
            if outcome.status is ItemStatus.FAILED:
                lines.append(f"  · {outcome.path}")
                lines.append(f"    {outcome.error}")

    if result.skipped_count:
        lines += ["", "跳过明细："]
        for outcome in result.outcomes:
            if outcome.status is ItemStatus.SKIPPED:
                lines.append(f"  · {outcome.path} — {outcome.error}")

    if verification.notes:
        lines += ["", "校验说明："] + [f"  · {n}" for n in verification.notes]

    lines += ["", "🗑️ 文件已移入回收站，需要的话可以随时恢复。"]

    if unlocked:
        lines += [""] + [f"🏆 成就解锁：{title}" for title in unlocked]

    if achievements:
        lines += [
            "",
            "📈 累计（仅统计移入回收站的文件）：",
            f"   文件 {achievements.get('total_files', 0)} 个，"
            f"共 {achievements.get('total_moved_human', '0 B')}",
        ]
        if achievements.get("next_milestone"):
            lines.append(
                f"   距离「{achievements['next_milestone']}」还差 "
                f"{achievements.get('next_milestone_remaining')}"
            )

    return "\n".join(lines)


def render_error(title: str, detail: str = "", hint: str | None = None) -> str:
    lines = [f"❌ {title}"]
    if detail:
        lines.append(f"   {detail}")
    if hint:
        lines.append(f"   → {hint}")
    return "\n".join(lines)
