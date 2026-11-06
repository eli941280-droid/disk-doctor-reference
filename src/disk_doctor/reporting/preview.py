"""Cleanup preview rendering.

The preview lists **every file** (BUG-03). A user cannot meaningfully approve a
plan they cannot inspect.

v0.2.1: the preview no longer has a "dry run" mode — generating a plan never
moves anything, so there is nothing to simulate. Execution is a separate
command that requires the full plan id.
"""

from __future__ import annotations

from ..core.schemas import CleanPlan
from ..core.utils import human_bytes

MAX_DISPLAY_FILES = 500


def render_preview(
    plan: CleanPlan,
    *,
    max_files: int = MAX_DISPLAY_FILES,
) -> str:
    lines = [
        "🧹 清理计划（尚未执行）",
        "━━━━━━━━━━━━━━━━━━",
        "",
        f"扫描范围：{plan.root}",
        f"计划编号：{plan.plan_id}",
        (
            f"创建时间：{plan.created_at.isoformat(timespec='seconds')}"
            f"　有效期 {plan.ttl_seconds} 秒"
        ),
        "",
    ]

    if not plan.items:
        lines += [
            "没有找到可清理的文件。",
            "",
            "可能原因：该目录下没有通过正向识别的缓存目录（仅 __pycache__ 与带",
            "CACHEDIR.TAG 的 .pytest_cache / .ruff_cache 会被自动纳入），",
            "或全部被安全策略拦截。",
        ]
    else:
        lines.append("计划移动以下文件：")
        lines.append("")
        lines.append("  序号  大小        风险      动作          状态      完整路径")
        lines.append("  " + "-" * 88)

        shown = 0
        for item in plan.items:
            for ref in item.files:
                if shown >= max_files:
                    break
                shown += 1
                lines.append(
                    f"  {shown:>4}  {human_bytes(ref.size_bytes):>9}  "
                    f"{item.risk.marker} {item.risk.label:<4}  "
                    f"{item.action.label:<11}  {'待处理':<8}  {ref.path}"
                )
            if shown >= max_files:
                break

        remaining = plan.total_files - shown
        if remaining > 0:
            lines += [
                "",
                f"  … 另有 {remaining} 个文件未在终端展示（总计 {plan.total_files} 个）。",
                "     使用 --json 获取完整清单；执行时会逐项重新校验。",
            ]

    lines += [
        "",
        "━━━━━━━━━━━━━━━━━━",
        f"  总文件数量：{plan.total_files}",
        f"  预计处理文件大小：约 {human_bytes(plan.total_bytes)}",
        f"  被保护文件：{plan.protected_files}（类型受保护，未纳入）",
        f"  被排除文件：{plan.excluded_files}（范围或策略不通过）",
        f"  被拦截目录：{len(plan.rejections)}",
        f"  需要二次确认：{'是' if plan.requires_second_confirm else '否'}",
        f"  plan_id：{plan.plan_id}",
        "━━━━━━━━━━━━━━━━━━",
    ]

    if plan.rejections:
        lines += ["", "🛡️ 被拦截的目录："]
        for rej in plan.rejections[:8]:
            lines.append(f"  · {rej.path}")
            lines.append(f"    原因：{rej.reason}（{rej.rule}）")
        if len(plan.rejections) > 8:
            lines.append(f"  … 另有 {len(plan.rejections) - 8} 项")

    if plan.notes:
        lines += [""] + [f"说明：{n}" for n in plan.notes]

    lines += [
        "",
        "生成计划不会改动任何文件。要执行这一份计划，请运行：",
        "",
        f"  disk-doctor execute --plan-id {plan.plan_id} --allow-root <授权目录>",
        "",
        "执行时会要求你完整输入：",
        f"  EXECUTE {plan.plan_id}",
    ]
    if plan.requires_second_confirm:
        lines += [
            "由于文件数量或体积超过阈值，还会额外要求输入：",
            f"  SECOND {plan.plan_id}",
        ]
    lines += ["", "文件将移入回收站，可随时恢复。"]

    return "\n".join(lines)


def render_confirmation_summary(plan: CleanPlan) -> str:
    """Short block re-printed immediately before execution (BUG-03)."""

    lines = [
        "即将执行的文件清单（执行前复核）",
        "━━━━━━━━━━━━━━━━━━",
        f"plan_id：{plan.plan_id}",
        f"文件数：{plan.total_files}　预计处理文件大小：约 {human_bytes(plan.total_bytes)}",
        "",
    ]
    for idx, ref in enumerate(plan.all_files(), start=1):
        lines.append(f"  {idx:>4}  {human_bytes(ref.size_bytes):>9}  {ref.path}")
    return "\n".join(lines)
