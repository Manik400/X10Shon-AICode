"""OpenAI-compatible chat completions provider (llama.cpp server, LM Studio, vLLM, ...)."""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

from ..core.errors import ModelTimeoutError, RateLimitedError
from .base import ModelProvider, ModelRequest, ModelResponse


class OpenAICompatibleProvider(ModelProvider):
    name = "openai_compatible"

    def __init__(self, base_url: str, *, api_key: str | None = None, max_response_bytes: int = 65536):
        self._base = base_url.rstrip("/")
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = httpx.AsyncClient(base_url=self._base, headers=headers, timeout=httpx.Timeout(10.0, read=600.0))
        self._max_bytes = max_response_bytes

    async def generate(self, request: ModelRequest, model: str) -> ModelResponse:
        body: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": request.system + "\nRespond with a single JSON object matching this schema:\n" + json.dumps(request.json_schema)},
                {"role": "user", "content": request.prompt},
            ],
            "temperature": request.temperature,
            "max_tokens": request.max_output_tokens,
            "response_format": {"type": "json_schema", "json_schema": {"name": request.task, "schema": request.json_schema, "strict": True}},
        }
        t0 = time.perf_counter()
        try:
            resp = await self._client.post("/chat/completions", json=body, timeout=httpx.Timeout(10.0, read=request.timeout_seconds))
        except httpx.TimeoutException as exc:
            raise ModelTimeoutError(f"model server timed out after {request.timeout_seconds}s") from exc
        except httpx.HTTPError as exc:
            raise ModelTimeoutError(f"model server unreachable: {exc}") from exc
        if resp.status_code == 429:
            raise RateLimitedError("model server rate limited", retry_after_seconds=float(resp.headers.get("retry-after", 5)))
        if resp.status_code == 400 and "response_format" in resp.text:
            # Server does not support json_schema response_format: retry with json_object.
            body["response_format"] = {"type": "json_object"}
            resp = await self._client.post("/chat/completions", json=body, timeout=httpx.Timeout(10.0, read=request.timeout_seconds))
        if resp.status_code >= 400:
            raise ModelTimeoutError(f"model server error {resp.status_code}: {resp.text[:200]}")
        if len(resp.content) > self._max_bytes:
            raise ModelTimeoutError("model response exceeds max size")
        data = resp.json()
        choice = (data.get("choices") or [{}])[0]
        text = (choice.get("message") or {}).get("content", "") or ""
        usage = data.get("usage") or {}
        return ModelResponse(
            text=text,
            provider=self.name,
            model=model,
            latency_ms=int((time.perf_counter() - t0) * 1000),
            prompt_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
        )

    async def health(self) -> dict[str, Any]:
        try:
            r = await self._client.get("/models", timeout=3.0)
            ok = r.status_code == 200
            models = [m.get("id") for m in r.json().get("data", [])] if ok else []
            return {"provider": self.name, "ok": ok, "base_url": self._base, "models": models}
        except httpx.HTTPError as exc:
            return {"provider": self.name, "ok": False, "base_url": self._base, "error": str(exc)[:200]}

    async def list_models(self) -> list[str]:
        return list((await self.health()).get("models", []))

    async def close(self) -> None:
        await self._client.aclose()
