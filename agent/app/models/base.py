"""Provider-independent model interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ModelRequest:
    """One inference request.

    ``task`` names the structured task (``intent``, ``plan``, ``extract_job``, ``map_fields``,
    ``match_job``, ``classify_recovery``). ``inputs`` holds the structured data the prompt was
    rendered from, so deterministic providers can answer without parsing prose. ``json_schema``
    is the JSON schema the output must satisfy.
    """

    task: str
    system: str
    prompt: str
    json_schema: dict[str, Any]
    inputs: dict[str, Any] = field(default_factory=dict)
    temperature: float = 0.0
    max_output_tokens: int = 1024
    timeout_seconds: float = 60.0


@dataclass(slots=True)
class ModelResponse:
    text: str
    provider: str
    model: str
    latency_ms: int
    prompt_tokens: int | None = None
    output_tokens: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)


class ModelProvider(ABC):
    name: str = "abstract"

    @abstractmethod
    async def generate(self, request: ModelRequest, model: str) -> ModelResponse: ...

    async def health(self) -> dict[str, Any]:
        return {"provider": self.name, "ok": True}

    async def list_models(self) -> list[str]:
        return []

    async def close(self) -> None:
        return None
