"""OpenAI-compatible chat client.

Works with any provider exposing ``/chat/completions``. DeepSeek is the default
example because the project was built against it. Keys are read from the
environment only — never from code, never from a committed file.
"""

from __future__ import annotations

import os

from .base import LLMClient, LLMConfigError, LLMError

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"
_ENV_KEYS = ("DISK_DOCTOR_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY")


def resolve_api_key(explicit: str | None = None) -> str | None:
    if explicit:
        return explicit
    for name in _ENV_KEYS:
        value = os.getenv(name)
        if value:
            return value
    return None


class OpenAICompatibleClient(LLMClient):
    name = "openai-compatible"

    def __init__(
        self,
        *,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 60.0,
        temperature: float = 0.2,
        max_tokens: int = 800,
    ) -> None:
        key = resolve_api_key(api_key)
        if not key:
            raise LLMConfigError(
                "未找到 API Key。请设置 DISK_DOCTOR_API_KEY（或 DEEPSEEK_API_KEY / OPENAI_API_KEY），"
                "或改用 --provider mock。"
            )
        self._key = key
        self.model = model or os.getenv("DISK_DOCTOR_MODEL", DEFAULT_MODEL)
        self.base_url = base_url or os.getenv("DISK_DOCTOR_BASE_URL", DEFAULT_BASE_URL)
        self.timeout = timeout
        self.temperature = temperature
        self.max_tokens = max_tokens
        self._client = None

    def _ensure_client(self):
        if self._client is not None:
            return self._client
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover - depends on env
            raise LLMError(
                "缺少 openai 依赖。安装：pip install 'disk-doctor-reference[llm]'"
            ) from exc
        self._client = OpenAI(api_key=self._key, base_url=self.base_url, timeout=self.timeout)
        return self._client

    def complete(self, system: str, user: str, *, temperature: float | None = None) -> str:
        client = self._ensure_client()
        try:
            response = client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=self.temperature if temperature is None else temperature,
                max_tokens=self.max_tokens,
            )
        except Exception as exc:
            raise LLMError(f"模型调用失败：{exc}") from exc

        try:
            content = response.choices[0].message.content
        except (AttributeError, IndexError) as exc:
            raise LLMError("模型响应格式异常") from exc
        return (content or "").strip()

    def describe(self) -> dict[str, str]:
        return {"provider": self.name, "model": self.model, "base_url": self.base_url}
