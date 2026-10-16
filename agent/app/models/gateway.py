"""ModelGateway: one entry point for every inference call.

Responsibilities: role → provider/model resolution, concurrency limiting, timeouts, response
size limits, strict schema validation with one bounded repair attempt, content-hash caching, and
metrics persistence. Providers are created once and reused (connections and model stay warm).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from ..config import ModelRoleConfig, Settings
from ..core.errors import AgentError, ModelOutputInvalidError
from ..core.util import stable_hash
from ..persistence.repositories import MetricsRepository
from .base import ModelProvider, ModelRequest, ModelResponse
from .heuristic import HeuristicProvider
from .ollama import OllamaProvider
from .openai_compatible import OpenAICompatibleProvider

log = logging.getLogger("lfa.models")
T = TypeVar("T", bound=BaseModel)


class _Cache:
    def __init__(self, ttl: float, max_items: int = 512):
        self._ttl = ttl
        self._max = max_items
        self._items: dict[str, tuple[float, str]] = {}

    def get(self, key: str) -> str | None:
        hit = self._items.get(key)
        if not hit:
            return None
        ts, val = hit
        if time.time() - ts > self._ttl:
            self._items.pop(key, None)
            return None
        return val

    def put(self, key: str, val: str) -> None:
        if len(self._items) >= self._max:
            oldest = min(self._items, key=lambda k: self._items[k][0])
            self._items.pop(oldest, None)
        self._items[key] = (time.time(), val)

    def clear(self) -> None:
        self._items.clear()


class ModelGateway:
    def __init__(self, settings: Settings, metrics: MetricsRepository | None = None, providers: dict[str, ModelProvider] | None = None):
        self._settings = settings
        self._metrics = metrics
        self._providers: dict[str, ModelProvider] = providers or {}
        self._semaphore = asyncio.Semaphore(max(1, settings.model_max_concurrency))
        self._cache = _Cache(settings.model_cache_ttl_seconds)
        self.calls = 0
        self.cache_hits = 0
        self.failures = 0

    # ------------------------------------------------------------------ providers
    def provider(self, name: str) -> ModelProvider:
        if name not in self._providers:
            s = self._settings
            if name == "ollama":
                self._providers[name] = OllamaProvider(s.ollama_base_url, max_response_bytes=s.model_max_response_bytes, num_threads=s.cpu_threads, gpu=s.gpu_enabled)
            elif name == "openai_compatible":
                self._providers[name] = OpenAICompatibleProvider(s.openai_compatible_base_url, max_response_bytes=s.model_max_response_bytes)
            else:
                self._providers[name] = HeuristicProvider()
        return self._providers[name]

    def role_config(self, role: str) -> ModelRoleConfig:
        return self._settings.model_roles.get(role) or ModelRoleConfig()

    def update_roles(self, roles: dict[str, dict[str, Any]]) -> None:
        for role, cfg in roles.items():
            self._settings.model_roles[role] = ModelRoleConfig(**cfg)
        self._cache.clear()

    async def health(self) -> dict[str, Any]:
        out: dict[str, Any] = {"roles": {r: c.model_dump() for r, c in self._settings.model_roles.items()}, "providers": {}}
        for name in sorted({c.provider for c in self._settings.model_roles.values()}):
            out["providers"][name] = await self.provider(name).health()
        return out

    async def close(self) -> None:
        for p in self._providers.values():
            await p.close()

    # ------------------------------------------------------------------ inference
    async def complete(self, role: str, request: ModelRequest, output_model: type[T], *, task_id: str | None = None, use_cache: bool = True) -> T:
        cfg = self.role_config(role)
        provider = self.provider(cfg.provider)
        request.json_schema = output_model.model_json_schema()
        request.temperature = cfg.temperature
        request.max_output_tokens = cfg.max_output_tokens
        request.timeout_seconds = self._settings.model_request_timeout_seconds
        if len(request.prompt) > self._settings.model_context_chars:
            request.prompt = request.prompt[: self._settings.model_context_chars] + "\n…(truncated)"
        key = stable_hash({"role": role, "p": cfg.provider, "m": cfg.model, "task": request.task, "prompt": request.prompt, "inputs": request.inputs})
        if use_cache:
            cached = self._cache.get(key)
            if cached is not None:
                self.cache_hits += 1
                await self._record(role, provider.name, cfg.model, 0, request, cached, None, None, cached=True, ok=True, error=None, task_id=task_id)
                return output_model.model_validate_json(cached)

        async with self._semaphore:
            self.calls += 1
            try:
                resp = await provider.generate(request, cfg.model)
            except AgentError as exc:
                self.failures += 1
                await self._record(role, provider.name, cfg.model, 0, request, "", None, None, cached=False, ok=False, error=str(exc)[:200], task_id=task_id)
                raise
            parsed, repaired = await self._validate_or_repair(provider, cfg.model, request, resp, output_model)
            await self._record(role, provider.name, cfg.model, resp.latency_ms, request, resp.text, resp.prompt_tokens, resp.output_tokens, cached=False, ok=True, error="repaired" if repaired else None, task_id=task_id)
            if use_cache:
                self._cache.put(key, parsed.model_dump_json())
            return parsed

    async def _validate_or_repair(self, provider: ModelProvider, model: str, request: ModelRequest, resp: ModelResponse, output_model: type[T]) -> tuple[T, bool]:
        try:
            return output_model.model_validate_json(_strip_fences(resp.text)), False
        except (ValidationError, ValueError) as first_err:
            if provider.name == "heuristic":
                raise ModelOutputInvalidError(f"heuristic output invalid: {first_err}") from first_err
            repair = ModelRequest(
                task=request.task,
                system=request.system,
                prompt=(
                    request.prompt
                    + "\n\nYour previous answer was not valid JSON for the schema. Error:\n"
                    + str(first_err)[:800]
                    + "\n\nReturn ONLY a corrected JSON object."
                ),
                json_schema=request.json_schema,
                inputs=request.inputs,
                temperature=0.0,
                max_output_tokens=request.max_output_tokens,
                timeout_seconds=request.timeout_seconds,
            )
            resp2 = await provider.generate(repair, model)
            try:
                return output_model.model_validate_json(_strip_fences(resp2.text)), True
            except (ValidationError, ValueError) as second_err:
                self.failures += 1
                raise ModelOutputInvalidError(f"model output invalid after repair: {str(second_err)[:300]}") from second_err

    async def _record(self, role: str, provider: str, model: str, latency_ms: int, request: ModelRequest, text: str, pt: int | None, ot: int | None, *, cached: bool, ok: bool, error: str | None, task_id: str | None) -> None:
        if self._metrics is None:
            return
        try:
            await self._metrics.record_model_call(role=role, provider=provider, model=model or "-", latency_ms=latency_ms, prompt_chars=len(request.prompt), output_chars=len(text), prompt_tokens=pt, output_tokens=ot, cached=cached, ok=ok, error=error, task_id=task_id)
        except Exception:
            log.debug("metrics write failed", exc_info=True)


def _strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t[3:]
        if t.endswith("```"):
            t = t[:-3]
    # Some models wrap JSON in prose; take the outermost object.
    if not t.startswith("{"):
        start, end = t.find("{"), t.rfind("}")
        if start != -1 and end > start:
            t = t[start : end + 1]
    try:
        json.loads(t)
    except json.JSONDecodeError:
        pass
    return t
