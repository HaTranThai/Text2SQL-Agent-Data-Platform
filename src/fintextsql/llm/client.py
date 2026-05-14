from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from fintextsql.core.config import Settings


class LLMError(RuntimeError):
    pass


@dataclass(slots=True)
class LLMMessage:
    role: str
    content: str


class LLMClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.base_url = settings.llm_base_url.rstrip("/")

    async def chat(
        self,
        messages: list[LLMMessage],
        *,
        temperature: float = 0.1,
        max_tokens: int = 1200,
    ) -> str:
        if not self.settings.llm_api_key:
            raise LLMError("LLM_API_KEY is not configured")

        payload: dict[str, Any] = {
            "model": self.settings.llm_model,
            "messages": [{"role": message.role, "content": message.content} for message in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        headers = {"Authorization": f"Bearer {self.settings.llm_api_key}"}

        try:
            async with httpx.AsyncClient(timeout=self.settings.llm_timeout_seconds) as client:
                response = await client.post(
                    f"{self.base_url}/chat/completions", json=payload, headers=headers
                )
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise LLMError(f"LLM request failed: {exc}") from exc

        data = response.json()
        try:
            return str(data["choices"][0]["message"]["content"]).strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"Unexpected LLM response shape: {data}") from exc
