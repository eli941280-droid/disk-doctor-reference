"""Diagnosis construction and rendering.

Two deliberate departures from the behaviour we studied:

* the health score formula is our own and is documented as SUBSTITUTED;
* no "disk will be full in N days" prediction is produced. Without historical
  samples any such number would be invented, and an invented number is worse
  than an honest gap.
"""

from __future__ import annotations

from ..core.schemas import (
    Diagnosis,
    DiskVolume,
    FileEntry,
    Finding,
    Recommendation,
    RiskLevel,
)
from ..core.utils import human_bytes


def health_score(percent_used: float) -> int:
    """Piecewise-linear score. SUBSTITUTED: no public evidence for the original."""

    p = max(0.0, min(100.0, percent_used))
    if p < 60:
        score = 100 - p * 0.15
    elif p < 80:
        score = 91 - (p - 60) * 1.0
    elif p < 90:
        score = 71 - (p - 80) * 2.0
    else:
        score = 51 - (p - 90) * 4.0
    return int(max(5, min(100, round(score))))


def score_verdict(score: int) -> str:
    if score >= 85:
        return "健康"
    if score >= 70:
        return "尚可，有优化空间"
    if score >= 50:
        return "偏紧张，建议清理"
    return "危险，需要立即处理"


def build_diagnosis(
    volume: DiskVolume,
    directories: list[FileEntry] | None = None,
    files: list[FileEntry] | None = None,
) -> Diagnosis:
    directories = directories or []
    files = files or []
    score = health_score(volume.percent_used)

    findings: list[Finding] = []
    if volume.percent_free < 10:
        findings.append(
            Finding(
                level="critical",
                title=f"可用空间仅剩 {volume.percent_free}%",
                detail=f"剩余 {human_bytes(volume.free_bytes)}，低可用空间会明显拖慢系统",
            )
        )
    elif volume.percent_free < 20:
        findings.append(
            Finding(
                level="warn",
                title=f"可用空间偏低（{volume.percent_free}%）",
                detail="建议尽快释放部分空间，避免系统临时文件写入失败",
            )
        )
    else:
        findings.append(
            Finding(level="ok", title="可用空间充足", detail=f"剩余 {human_bytes(volume.free_bytes)}")
        )

    if directories:
        top = directories[0]
        share = (top.size_bytes / volume.used_bytes * 100) if volume.used_bytes else 0
        if share >= 20:
            findings.append(
                Finding(
                    level="info",
                    title=f"占用最多：{top.path}",
                    detail=f"约 {human_bytes(top.size_bytes)}，占已用空间 {share:.0f}%",
                )
            )

    big_files = [f for f in files if f.size_bytes >= 1024**3]
    if big_files:
        findings.append(
            Finding(
                level="info",
                title=f"发现 {len(big_files)} 个超过 1 GB 的文件",
                detail="单个大文件通常不是缓存，需要人工判断是否还需要保留",
            )
        )

    findings.append(
        Finding(
            level="info",
            title="未做空间耗尽预测",
            detail="缺少历史采样，无法可靠推算增长速度；本项目选择不给出编造的天数",
        )
    )

    recommendations = [
        Recommendation(
            action="清理已知缓存与临时文件",
            impact="通常可释放数 GB，且可自动重建",
            risk=RiskLevel.SAFE,
        ),
        Recommendation(
            action="检查大文件是否需要保留",
            impact="单个文件即可释放 GB 级空间",
            risk=RiskLevel.CAUTION,
        ),
        Recommendation(
            action="每月做一次体检",
            impact="避免缓存再次堆积到影响系统",
            risk=RiskLevel.SAFE,
        ),
    ]

    return Diagnosis(
        volume=volume,
        health_score=score,
        findings=findings,
        recommendations=recommendations,
        largest_directories=directories,
        largest_files=files,
        days_until_full=None,
    )


_LEVEL_MARK = {"ok": "✅", "warn": "⚠️", "info": "💡", "critical": "🔴"}


def render_diagnosis(diagnosis: Diagnosis) -> str:
    v = diagnosis.volume
    lines = [
        "🩺 磁盘健康诊断报告",
        "━━━━━━━━━━━━━━━━━━",
        "",
        "📊 基本信息",
        f"  扫描位置：{v.path}",
        f"  总空间：{human_bytes(v.total_bytes)}",
        f"  已使用：{human_bytes(v.used_bytes)}（{v.percent_used:.0f}%）",
        f"  剩余：{human_bytes(v.free_bytes)}",
        "",
        f"💯 健康评分：{diagnosis.health_score} / 100（{score_verdict(diagnosis.health_score)}）",
        "",
        "🔍 主要发现",
    ]
    for f in diagnosis.findings:
        mark = _LEVEL_MARK.get(f.level, "•")
        lines.append(f"  {mark} {f.title}")
        if f.detail:
            lines.append(f"     {f.detail}")

    if diagnosis.largest_directories:
        lines += ["", "📁 占用最多的目录"]
        for entry in diagnosis.largest_directories[:5]:
            lines.append(f"  {human_bytes(entry.size_bytes):>10}  {entry.path}")

    if diagnosis.largest_files:
        lines += ["", "📄 最大的文件"]
        for entry in diagnosis.largest_files[:5]:
            lines.append(f"  {human_bytes(entry.size_bytes):>10}  {entry.path}")

    lines += ["", "📈 空间预测", "  数据不足，未做预测（避免给出编造的天数）", "", "💡 优化建议"]
    for idx, rec in enumerate(diagnosis.recommendations, start=1):
        lines.append(f"  {idx}. {rec.action}（{rec.impact}，{rec.risk.label}）")
    return "\n".join(lines)
