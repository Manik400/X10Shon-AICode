"""IntentRouter: deterministic classification first, model only for ambiguous instructions."""

from __future__ import annotations

import re

from ..models.base import ModelRequest
from ..models.gateway import ModelGateway
from ..models.schemas import IntentOutput
from .prompts import render_prompt

_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(contacts?|recruiters?|hiring manager|hr|emails?|reach out|who to (message|contact))\b", re.I), "contacts"),
    (re.compile(r"\b(track|status|progress|how many|report|summary|what (did|have) (you|we) appl)", re.I), "track"),
    (re.compile(r"\b(dry[- ]?run|preview|simulate)\b", re.I), "apply_list"),
    (re.compile(r"\b(all|each|every|whole list|one by one)\b.*\b(apply|application)|\b(apply|application)\b.*\b(all|each|every|whole list|one by one)\b", re.I), "apply_list"),
    (re.compile(r"\b(apply|application|fill (in|out)?|submit)\b", re.I), "apply"),
    (re.compile(r"\b(extract|import|save|add|score|screen)\b.*\bjob\b|\bjob\b.*\b(extract|import|save|add|score)\b", re.I), "extract_job"),
    (re.compile(r"^(go to|open|navigate to|visit)\s+\S+", re.I), "navigate"),
    (re.compile(r"\b(read|summari[sz]e|what does|what is on|describe)\b", re.I), "read"),
]


class IntentRouter:
    def __init__(self, gateway: ModelGateway):
        self._gateway = gateway

    async def route(self, instruction: str, url: str | None, *, task_id: str | None = None) -> IntentOutput:
        text = instruction.strip()
        for pat, kind in _RULES:
            if pat.search(text):
                m = re.search(r"https?://\S+", text)
                return IntentOutput(kind=kind, confidence=0.9, url=m.group(0) if m else url, notes="rule")  # type: ignore[arg-type]
        if self._gateway.role_config("intent").provider == "heuristic":
            inputs = {"instruction": text, "url": url}
            system, prompt = render_prompt("intent", inputs)
            return await self._gateway.complete("intent", ModelRequest(task="intent", system=system, prompt=prompt, json_schema={}, inputs=inputs), IntentOutput, task_id=task_id)
        inputs = {"instruction": text, "url": url}
        system, prompt = render_prompt("intent", inputs)
        return await self._gateway.complete("intent", ModelRequest(task="intent", system=system, prompt=prompt, json_schema={}, inputs=inputs), IntentOutput, task_id=task_id)
