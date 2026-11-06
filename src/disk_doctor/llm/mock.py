"""Deterministic offline substitute for a language model.

Purpose: let the whole pipeline run, be tested, and be demoed with no API key and
no network. Output is explicitly prefixed so a reader can never mistake it for a
real model response.
"""

from __future__ import annotations

import hashlib
import re

from .base import LLMClient

_STOPWORDS = ("的", "了", "帮我", "一下", "请", "看看", "我")


class MockLLM(LLMClient):
    name = "mock"
    model = "deterministic-v1"

    def __init__(self, *, max_chars: int = 320) -> None:
        self.max_chars = max_chars

    def complete(self, system: str, user: str, *, temperature: float | None = None) -> str:
        digest = hashlib.sha256((system + "\x00" + user).encode("utf-8")).hexdigest()[:8]
        gist = self._gist(user)
        return (
            f"[mock:{digest}] 已根据结构化结果生成说明。用户诉求要点：{gist}。"
            "以上文字由本地确定性模板产生，未调用任何模型服务。"
        )

    def _gist(self, user: str) -> str:
        text = re.sub(r"\s+", " ", user or "").strip()
        for word in _STOPWORDS:
            text = text.replace(word, "")
        text = text.strip(" ，,。.")
        if not text:
            return "（空）"
        return text[: self.max_chars]
