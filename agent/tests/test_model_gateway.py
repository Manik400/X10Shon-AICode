from __future__ import annotations

import pytest

from app.config import ModelRoleConfig, Settings
from app.core.actions import PlannerOutput
from app.core.errors import ModelOutputInvalidError
from app.models.base import ModelProvider, ModelRequest, ModelResponse
from app.models.gateway import ModelGateway
from app.models.schemas import IntentOutput, JobExtraction


class ScriptedProvider(ModelProvider):
    name = "scripted"

    def __init__(self, outputs: list[str]):
        self.outputs = list(outputs)
        self.calls = 0

    async def generate(self, request: ModelRequest, model: str) -> ModelResponse:
        self.calls += 1
        text = self.outputs.pop(0) if self.outputs else "{}"
        return ModelResponse(text=text, provider=self.name, model=model, latency_ms=1)


def gateway(settings: Settings, provider: ModelProvider, role: str = "planner") -> ModelGateway:
    settings.model_roles[role] = ModelRoleConfig(provider="ollama", model="test")
    return ModelGateway(settings, None, providers={"ollama": provider})


@pytest.mark.asyncio
async def test_heuristic_provider_outputs_validate(settings) -> None:
    g = ModelGateway(settings, None)
    out = await g.complete("intent", ModelRequest(task="intent", system="", prompt="", json_schema={}, inputs={"instruction": "apply to this job", "url": "https://x"}), IntentOutput)
    assert out.kind == "apply"
    plan = await g.complete("planner", ModelRequest(task="plan", system="", prompt="", json_schema={}, inputs={"page_kind": "login", "goal": "apply"}), PlannerOutput)
    assert plan.actions[0].type == "ask_user"


@pytest.mark.asyncio
async def test_invalid_output_is_repaired_once(settings) -> None:
    p = ScriptedProvider(['{"kind": "nonsense"}', '{"kind": "read", "confidence": 0.5}'])
    g = gateway(settings, p, "intent")
    out = await g.complete("intent", ModelRequest(task="intent", system="s", prompt="p", json_schema={}), IntentOutput)
    assert out.kind == "read" and p.calls == 2


@pytest.mark.asyncio
async def test_invalid_output_twice_raises(settings) -> None:
    p = ScriptedProvider(["not json", "still not json"])
    g = gateway(settings, p, "intent")
    with pytest.raises(ModelOutputInvalidError):
        await g.complete("intent", ModelRequest(task="intent", system="s", prompt="p", json_schema={}), IntentOutput)
    assert g.failures == 1


@pytest.mark.asyncio
async def test_planner_output_rejects_forbidden_actions(settings) -> None:
    bad = '{"page_state": "form", "actions": [{"type": "navigate", "args": {"url": "javascript:x"}}], "confidence": 1}'
    bad2 = '{"page_state": "form", "actions": [{"type": "click", "target": {"strategy": "css", "value": "b"}, "args": {"script": "x"}}], "confidence": 1}'
    g = gateway(settings, ScriptedProvider([bad, bad2]))
    with pytest.raises(ModelOutputInvalidError):
        await g.complete("planner", ModelRequest(task="plan", system="s", prompt="p", json_schema={}), PlannerOutput, use_cache=False)


@pytest.mark.asyncio
async def test_cache_avoids_repeat_inference(settings) -> None:
    p = ScriptedProvider(['{"title": "X", "confidence": 0.9}'])
    g = gateway(settings, p, "extractor")
    req = ModelRequest(task="extract_job", system="s", prompt="same", json_schema={})
    a = await g.complete("extractor", req, JobExtraction)
    b = await g.complete("extractor", ModelRequest(task="extract_job", system="s", prompt="same", json_schema={}), JobExtraction)
    assert a.title == b.title == "X" and p.calls == 1 and g.cache_hits == 1


@pytest.mark.asyncio
async def test_context_is_bounded(settings) -> None:
    settings.model_context_chars = 100
    captured: list[str] = []

    class Capture(ScriptedProvider):
        async def generate(self, request: ModelRequest, model: str) -> ModelResponse:
            captured.append(request.prompt)
            return await super().generate(request, model)

    g = gateway(settings, Capture(['{"kind": "read", "confidence": 1}']), "intent")
    await g.complete("intent", ModelRequest(task="intent", system="s", prompt="x" * 1000, json_schema={}), IntentOutput)
    assert len(captured[0]) < 130


@pytest.mark.asyncio
async def test_code_fences_are_stripped(settings) -> None:
    g = gateway(settings, ScriptedProvider(['```json\n{"kind": "read", "confidence": 1}\n```']), "intent")
    out = await g.complete("intent", ModelRequest(task="intent", system="s", prompt="p", json_schema={}), IntentOutput)
    assert out.kind == "read"


@pytest.mark.asyncio
async def test_metrics_are_recorded(ctx) -> None:
    await ctx.gateway.complete("intent", ModelRequest(task="intent", system="", prompt="", json_schema={}, inputs={"instruction": "read this", "url": None}), IntentOutput)
    summary = await ctx.repos.metrics.summary()
    assert summary["total_calls"] == 1 and summary["roles"][0]["role"] == "intent"
