"""Rule-based intent routing.

Why rules instead of an LLM call: routing decides whether a destructive path gets
entered at all. Making that decision depend on a sampled token stream is a
safety regression, not a sophistication upgrade. The LLM is used for language,
not for authority.

v0.2.1 (P1-2): scope parsing no longer uses ``os.sep``. A Windows path typed on
Linux must normalise to the same string as on Windows, because the path belongs
to the *input's* convention, not to the host running the tests.
"""

from __future__ import annotations

import ntpath
import posixpath
import re
from dataclasses import dataclass

from ..core.schemas import Intent

# ``D:`` optionally followed by a slash and a tail. The tail stops at whitespace
# or at a character that cannot appear unquoted in a path argument.
_WIN_DRIVE_PATH = re.compile(r"\b([A-Za-z]):((?:[\\/][^\s\"'，,。；;、]*)?)")
_CN_DRIVE = re.compile(r"([A-Za-z])\s*盘")
_UNIX_PATH = re.compile(r"(/(?:[\w.\-]+/)*[\w.\-]+)")

# Ordered: the first match wins. Destructive and out-of-capability patterns come
# first so they can never be shadowed by a later "clean up" keyword.
_RULES: tuple[tuple[Intent, tuple[str, ...], str], ...] = (
    (
        Intent.OUT_OF_SCOPE,
        ("格式化", "分区", "重新分区", "diskpart", "低格", "重建分区表", "bootcamp"),
        "破坏性磁盘操作不在能力范围内",
    ),
    (
        Intent.OUT_OF_SCOPE,
        ("恢复", "找回", "误删", "数据恢复", "undelete", "recover deleted"),
        "数据恢复不在能力范围内",
    ),
    (
        Intent.OUT_OF_SCOPE,
        ("隐私", "私密", "敏感文件", "身份证", "密码文件"),
        "内容审查不在能力范围内",
    ),
    (
        Intent.OUT_OF_SCOPE,
        ("提权", "管理员权限", "sudo", "root 权限", "root权限"),
        "不请求提升权限",
    ),
    (Intent.ACHIEVEMENTS, ("成就", "里程碑", "统计", "打卡"), "成就与累计统计"),
    (Intent.TRASH_INFO, ("回收站", "trash", "撤销删除"), "回收站状态"),
    (Intent.FIND_DUPLICATE, ("重复", "重份", "duplicate"), "重复文件检测"),
    (Intent.FIND_LARGE, ("大文件", "最占", "占空间", "top", "最大"), "大文件定位"),
    (
        Intent.DEV_CLEAN,
        ("开发者", "node_modules", "gradle", "docker", "xcode", "pip 缓存", "npm"),
        "开发者环境专项清理",
    ),
    (Intent.CLEAN_DEEP, ("深度清理", "彻底", "全面清理", "深度扫描"), "深度清理"),
    (Intent.CLEAN_QUICK, ("清理", "清除", "瘦身", "腾空间", "垃圾", "缓存"), "快速清理"),
    (Intent.DIAGNOSE, ("看看", "诊断", "健康", "空间", "剩多少", "把脉", "体检"), "磁盘诊断"),
)


@dataclass(frozen=True)
class RoutedIntent:
    intent: Intent
    scope: str | None
    confidence: float
    matched: tuple[str, ...]
    reason: str

    @property
    def is_cleanup(self) -> bool:
        return self.intent.is_cleanup


def normalize_windows_path(letter: str, tail: str | None) -> str:
    """Return a canonical Windows path string, independent of the host OS."""

    rest = (tail or "").lstrip("\\/")
    raw = f"{letter.upper()}:\\{rest}" if rest else f"{letter.upper()}:\\"
    return ntpath.normpath(raw)


class IntentRouter:
    """Deterministic classifier with scope extraction."""

    def route(self, text: str) -> RoutedIntent:
        lowered = (text or "").strip().lower()
        if not lowered:
            return RoutedIntent(Intent.UNKNOWN, None, 0.0, (), "空输入")

        for intent, keywords, reason in _RULES:
            hits = tuple(k for k in keywords if k in lowered)
            if hits:
                return RoutedIntent(
                    intent=intent,
                    scope=self.extract_scope(text),
                    confidence=min(1.0, 0.5 + 0.15 * len(hits)),
                    matched=hits,
                    reason=reason,
                )

        return RoutedIntent(
            Intent.UNKNOWN, self.extract_scope(text), 0.2, (), "未匹配到已知意图"
        )

    @staticmethod
    def extract_scope(text: str) -> str | None:
        """Pull a filesystem scope out of free text, if one is stated.

        The result is always expressed in the *input's* convention: Windows
        paths come back with backslashes even when parsing runs on Linux.
        """

        match = _WIN_DRIVE_PATH.search(text)
        if match:
            return normalize_windows_path(match.group(1), match.group(2))

        match = _CN_DRIVE.search(text)
        if match:
            return normalize_windows_path(match.group(1), None)

        match = _UNIX_PATH.search(text)
        if match:
            return posixpath.normpath(match.group(1))

        return None
