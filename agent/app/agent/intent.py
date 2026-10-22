"""IntentRouter: deterministic classification first, model only for ambiguous instructions.

Job actions (apply, lists, tracking, contacts) are recognised by explicit wording. Everything
else — questions, "summarise this", "draft a reply", "explain this PDF" — is ``ask``: a read-only
answer about the current page from the local model, which never clicks, types or submits.
"""

from __future__ import annotations

import re

from ..models.base import ModelRequest
from ..models.gateway import ModelGateway
from ..models.schemas import IntentOutput
from .prompts import render_prompt

_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(contacts?|recruiters?|hiring managers?|who (should I|to) (message|contact|email))\b|\bfind\b.*\b(emails?|linkedin)\b", re.I), "contacts"),
    (re.compile(r"\btrack\b|\b(status|progress|report)\b.*\b(appl\w*|jobs?)\b|how many (jobs|applications)|what (did|have) (you|we) appl", re.I), "track"),
    (re.compile(r"\b(dry[- ]?run|simulate)\b", re.I), "apply_list"),
    (re.compile(r"\b(all|each|every|whole list|one by one)\b.*\bapply\b|\bapply\b.*\b(all|each|every|whole list|one by one)\b", re.I), "apply_list"),
    # "Fill this page / form" without asking to submit or apply: fill everything, leave Submit to the user.
    (re.compile(r"^(?!.*\b(submit|apply)\b).*\b(fill|autofill|auto-fill|complete)\b.*\b(page|form|fields?|this|it|everything|all)\b", re.I), "fill_page"),
    # Imperative applying only: "What is the application deadline?" is a question, not an apply.
    (re.compile(r"\bapply\b|\bfill (in |out )?(this|the|my|that)\b.*\b(form|application)\b|\bsubmit (this|the|my) (form|application)\b", re.I), "apply"),
    (re.compile(r"\b(extract|import|save|add|score|screen)\b.*\bjob\b|\bjob\b.*\b(extract|import|save|add|score)\b", re.I), "extract_job"),
    (re.compile(r"^(go to|open|navigate to|visit)\s+\S+", re.I), "navigate"),
]


class IntentRouter:
    def __init__(self, gateway: ModelGateway):
        self._gateway = gateway

    @property
    def gateway(self) -> ModelGateway:
        return self._gateway

    async def route(self, instruction: str, url: str | None, *, task_id: str | None = None) -> IntentOutput:
        text = instruction.strip()
        m = re.search(r"https?://\S+", text)
        for pat, kind in _RULES:
            if pat.search(text):
                return IntentOutput(kind=kind, confidence=0.9, url=m.group(0) if m else url, notes="rule")  # type: ignore[arg-type]
        if not text:
            inputs = {"instruction": text, "url": url}
            system, prompt = render_prompt("intent", inputs)
            return await self._gateway.complete("intent", ModelRequest(task="intent", system=system, prompt=prompt, json_schema={}, inputs=inputs), IntentOutput, task_id=task_id)
        # Questions, summaries, drafting, explanations…: answer from the page (read-only).
        return IntentOutput(kind="ask", confidence=0.8, url=m.group(0) if m else url, notes="assistant")
