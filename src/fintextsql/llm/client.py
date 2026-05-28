from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

from fintextsql.core.config import Settings


class LLMError(RuntimeError):
    pass


@dataclass(slots=True)
class LLMMessage:
    role: str
    content: str


# How long to remember a recent health-check result so we don't probe on every request.
_HEALTH_TTL_SECONDS = 5.0
# Short timeout for the pre-flight probe (TCP connect + tiny GET). Must be far below
# the chat timeout so a dead endpoint fails fast (~3s) instead of hanging the full 60s.
_HEALTH_PROBE_TIMEOUT = 3.0


class LLMClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.base_url = settings.llm_base_url.rstrip("/")
        self._health: tuple[float, bool, str] | None = None  # (checked_at, ok, last_error)

    async def ensure_alive(self) -> None:
        """Quick pre-flight check; raises LLMError if endpoint is unreachable.

        Result is cached for `_HEALTH_TTL_SECONDS` so a flurry of requests in the
        same conversation doesn't probe the endpoint each time.
        """
        now = time.monotonic()
        if self._health and now - self._health[0] < _HEALTH_TTL_SECONDS:
            checked_at, ok, last_error = self._health
            if ok:
                return
            raise LLMError(f"LLM endpoint unhealthy (cached): {last_error}")

        # Probe the OpenAI-compat `/models` endpoint with a tight timeout.
        # On most local LLM servers this returns instantly when alive and refuses
        # quickly when dead. If the server lacks `/models` we fall back to a
        # plain TCP connect via httpx, which still rejects fast.
        parsed = urlparse(self.base_url)
        probe_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path or ''}/models"
        try:
            async with httpx.AsyncClient(timeout=_HEALTH_PROBE_TIMEOUT) as client:
                resp = await client.get(probe_url)
                # Anything that responds — even 401/404 — means the server is up.
                _ = resp.status_code
            self._health = (now, True, "")
        except httpx.HTTPError as exc:
            err = f"{type(exc).__name__}: {exc}"
            self._health = (now, False, err)
            raise LLMError(f"LLM endpoint unreachable: {err}") from exc

    async def chat(
        self,
        messages: list[LLMMessage],
        *,
        temperature: float = 0.1,
        max_tokens: int = 1200,
    ) -> str:
        if not self.settings.llm_api_key:
            raise LLMError("LLM_API_KEY is not configured")

        # Fail fast if the endpoint is unreachable (cached probe).
        await self.ensure_alive()

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
            # Invalidate health cache so the next call re-probes instead of
            # blindly retrying a 60s chat against a dead endpoint.
            self._health = (time.monotonic(), False, str(exc))
            raise LLMError(f"LLM request failed: {exc}") from exc

        data = response.json()
        try:
            return str(data["choices"][0]["message"]["content"]).strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"Unexpected LLM response shape: {data}") from exc

    async def chat_stream(
        self,
        messages: list[LLMMessage],
        *,
        temperature: float = 0.1,
        max_tokens: int = 1200,
    ) -> AsyncIterator[str]:
        """Stream chat completion content chunks (yields text fragments).

        Uses OpenAI-compatible SSE: each chunk is `data: {json}\\n\\n`. Yields
        only the incremental content text; terminator `data: [DONE]` is consumed
        silently. The full response is never accumulated in memory.
        """
        if not self.settings.llm_api_key:
            raise LLMError("LLM_API_KEY is not configured")
        await self.ensure_alive()

        payload: dict[str, Any] = {
            "model": self.settings.llm_model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        headers = {
            "Authorization": f"Bearer {self.settings.llm_api_key}",
            "Accept": "text/event-stream",
        }
        try:
            async with httpx.AsyncClient(timeout=self.settings.llm_timeout_seconds) as client:
                async with client.stream(
                    "POST",
                    f"{self.base_url}/chat/completions",
                    json=payload,
                    headers=headers,
                ) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        data_str = line[5:].strip()
                        if data_str == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data_str)
                            delta = chunk["choices"][0]["delta"].get("content") or ""
                        except (json.JSONDecodeError, KeyError, IndexError):
                            continue
                        if delta:
                            yield delta
        except httpx.HTTPError as exc:
            self._health = (time.monotonic(), False, str(exc))
            raise LLMError(f"LLM stream failed: {exc}") from exc
