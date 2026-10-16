"""Ollama provider: ``POST /api/chat`` with ``format`` set to the JSON schema."""

from __future__ import annotations

import time
from typing import Any

import httpx

from ..core.errors import ModelTimeoutError, RateLimitedError
from .base import ModelProvider, ModelRequest, ModelResponse


class OllamaProvider(ModelProvider):
    name = "ollama"

    def __init__(self, base_url: str, *, max_response_bytes: int = 65536, num_threads: int = 0, gpu: bool = False):
        self._base = base_url.rstrip("/")
        self._client = httpx.AsyncClient(base_url=self._base, timeout=httpx.Timeout(10.0, read=600.0))
        self._max_bytes = max_response_bytes
        self._threads = num_threads
        self._gpu = gpu

    async def generate(self, request: ModelRequest, model: str) -> ModelResponse:
        options: dict[str, Any] = {"temperature": request.temperature, "num_predict": request.max_output_tokens}
        if self._threads > 0:
            options["num_thread"] = self._threads
        if not self._gpu:
            options["num_gpu"] = 0
        body = {
            "model": model,
            "messages": [{"role": "system", "content": request.system}, {"role": "user", "content": request.prompt}],
            "stream": False,
            "format": request.json_schema,
            "options": options,
            "keep_alive": "30m",  # keep the model loaded between calls
        }
        t0 = time.perf_counter()
        try:
            resp = await self._client.post("/api/chat", json=body, timeout=httpx.Timeout(10.0, read=request.timeout_seconds))
        except httpx.TimeoutException as exc:
            raise ModelTimeoutError(f"ollama timed out after {request.timeout_seconds}s") from exc
        except httpx.HTTPError as exc:
            raise ModelTimeoutError(f"ollama unreachable: {exc}") from exc
        if resp.status_code == 429:
            raise RateLimitedError("ollama rate limited", retry_after_seconds=float(resp.headers.get("retry-after", 5)))
        if resp.status_code >= 400:
            raise ModelTimeoutError(f"ollama error {resp.status_code}: {resp.text[:200]}")
        if len(resp.content) > self._max_bytes:
            raise ModelTimeoutError("ollama response exceeds max size")
        data = resp.json()
        text = (data.get("message") or {}).get("content", "")
        return ModelResponse(
            text=text,
            provider=self.name,
            model=model,
            latency_ms=int((time.perf_counter() - t0) * 1000),
            prompt_tokens=data.get("prompt_eval_count"),
            output_tokens=data.get("eval_count"),
            raw={k: data.get(k) for k in ("total_duration", "load_duration", "prompt_eval_duration", "eval_duration")},
        )

    async def health(self) -> dict[str, Any]:
        try:
            r = await self._client.get("/api/tags", timeout=3.0)
            ok = r.status_code == 200
            models = [m.get("name") for m in r.json().get("models", [])] if ok else []
            return {"provider": self.name, "ok": ok, "base_url": self._base, "models": models}
        except httpx.HTTPError as exc:
            return {"provider": self.name, "ok": False, "base_url": self._base, "error": str(exc)[:200]}

    async def list_models(self) -> list[str]:
        h = await self.health()
        return list(h.get("models", []))

    async def close(self) -> None:
        await self._client.aclose()
