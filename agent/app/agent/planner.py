"""TaskPlanner: proposes the next action(s). Deterministic planners handle known page states;
the model is consulted for general tasks, and its output is strictly schema-validated."""

from __future__ import annotations

from typing import Any

from ..browser.page_state import PageClassification
from ..browser.snapshot import PageSnapshot
from ..core.actions import BrowserAction, PlannerOutput
from ..core.enums import RiskLevel
from ..models.base import ModelRequest
from ..models.gateway import ModelGateway
from .context import ContextBuilder
from .prompts import render_prompt


class TaskPlanner:
    def __init__(self, gateway: ModelGateway, context: ContextBuilder):
        self._gateway = gateway
        self._context = context

    async def next_actions(self, snap: PageSnapshot, cls: PageClassification, *, goal: str, instruction: str = "", facts: dict[str, str], history: list[dict[str, Any]], task_id: str | None) -> PlannerOutput:
        # Deterministic planners for states that must never be delegated to a model.
        if cls.kind == "challenge":
            return PlannerOutput(page_state="challenge", actions=[BrowserAction(type="ask_user", args={"question": "A verification challenge is shown. Complete it in the browser, then resume."}, risk=RiskLevel.LOW)], confidence=1.0)
        if cls.kind == "login":
            return PlannerOutput(page_state="login", actions=[BrowserAction(type="ask_user", args={"question": f"Please log in at {snap.url} in the browser, then resume."}, risk=RiskLevel.LOW)], confidence=1.0)
        if goal == "read":
            return PlannerOutput(page_state=cls.kind, actions=[BrowserAction(type="extract", args={}, risk=RiskLevel.LOW), BrowserAction(type="done", args={"summary": "page read"}, risk=RiskLevel.LOW)], confidence=1.0)
        inputs = self._context.build(snap, cls, goal=goal, instruction=instruction, facts=facts, history=history)
        system, prompt = render_prompt("plan", inputs)
        req = ModelRequest(task="plan", system=system, prompt=prompt, json_schema={}, inputs=inputs)
        return await self._gateway.complete("planner", req, PlannerOutput, task_id=task_id, use_cache=False)
