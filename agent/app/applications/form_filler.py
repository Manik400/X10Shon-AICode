"""FormFiller: turns a snapshot + profile into a FillPlan and executes the auto-fillable part."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from ..browser.driver import BrowserDriver
from ..browser.snapshot import PageSnapshot
from ..core.actions import ActionResult, BrowserAction, Target
from ..core.enums import RiskLevel
from ..models.base import ModelRequest
from ..models.gateway import ModelGateway
from ..models.schemas import FieldMappingOutput
from .field_mapping import FieldPlan, FillPlan, build_fill_plan
from .profile import CandidateProfile

log = logging.getLogger("lfa.forms")


@dataclass(slots=True)
class FillReport:
    filled: list[dict[str, Any]] = field(default_factory=list)
    failed: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"filled": self.filled, "failed": self.failed, "skipped": self.skipped}


class FormFiller:
    def __init__(self, gateway: ModelGateway):
        self._gateway = gateway

    async def plan(self, snap: PageSnapshot, profile: CandidateProfile, answers: list[dict[str, Any]], resumes: list[dict[str, Any]], *, submit_names: list[str], task_id: str | None = None) -> FillPlan:
        facts = profile.facts()
        fields = [n for n in snap.form_fields() if n.visible]
        model_mappings: list[dict[str, Any]] | None = None
        # Rules first; the model is consulted only for fields the rules could not place.
        from .field_mapping import heuristic_map_fields

        rule_inputs = [{"node": n.i, "label": n.name or n.label or n.placeholder or n.name_attr or "", "type": n.type or n.role, "options": n.options, "required": n.required} for n in fields]
        unmapped = {m["node"] for m in heuristic_map_fields(rule_inputs, list(facts.keys()), answers) if not m.get("profile_key") and not m.get("answer_id") and m.get("category") not in ("upload", "consent", "legal")}
        leftovers = [n for n in fields if n.i in unmapped]
        if leftovers and self._gateway.role_config("field_mapper").provider != "heuristic":
            from ..agent.prompts import render_prompt

            inputs = {
                "fields": [{"node": n.i, "label": n.name or n.label or n.placeholder or n.name_attr or "", "type": n.type or n.role, "options": (n.options or [])[:20], "required": n.required, "section": n.section} for n in leftovers][:60],
                "fact_keys": sorted(facts.keys()),
                "answers": [{"id": a["id"], "question": a["question"], "category": a["category"]} for a in answers][:60],
            }
            system, prompt = render_prompt("map_fields", inputs)
            try:
                out = await self._gateway.complete("field_mapper", ModelRequest(task="map_fields", system=system, prompt=prompt, json_schema={}, inputs=inputs), FieldMappingOutput, task_id=task_id)
                model_mappings = [m.model_dump() for m in out.mappings]
            except Exception as exc:
                log.warning("model field mapping failed; rules only: %s", exc)
        return build_fill_plan(snap.nodes, facts, answers, model_mappings, resumes=resumes, default_resume_id=profile.default_resume_id, submit_names=submit_names)

    @staticmethod
    def action_for(fp: FieldPlan) -> BrowserAction | None:
        target = Target(strategy="node", value=str(fp.node))
        if fp.field_type == "file":
            return BrowserAction(type="upload", target=target, args={"resume_id": fp.resume_id or ""}, risk=RiskLevel.MEDIUM, verification="value_set", timeout_ms=20_000)
        if fp.field_type in ("select", "select-multiple") or (fp.options is not None and fp.field_type in ("combobox", "listbox")):
            return BrowserAction(type="select", target=target, args={"value": fp.value or ""}, risk=RiskLevel.MEDIUM, verification="value_set")
        if fp.field_type == "radio":
            # Target the radio whose label equals the chosen option: resolved by label within the section.
            return BrowserAction(type="check", target=Target(strategy="label", value=fp.value or "", section=fp.label or None), args={"checked": True}, risk=RiskLevel.MEDIUM, verification="value_set")
        if fp.field_type == "checkbox":
            return BrowserAction(type="check", target=target, args={"checked": (fp.value or "").lower() == "yes"}, risk=RiskLevel.MEDIUM, verification="value_set")
        return BrowserAction(type="type", target=target, args={"text": fp.value or ""}, risk=RiskLevel.LOW, verification="value_set")

    async def fill(self, driver: BrowserDriver, tab_id: str, plan: FillPlan, *, resolve_payload, execute) -> FillReport:  # type: ignore[no-untyped-def]
        """Execute auto fields through the supplied ``execute(action, payload)`` coroutine (the
        TaskRunner's policy-checked executor)."""
        report = FillReport()
        for fp in plan.fields:
            if fp.status != "auto":
                report.skipped.append({"node": fp.node, "label": fp.label, "status": fp.status, "reason": fp.reason})
                continue
            if fp.field_type != "file" and fp.current_value not in (None, "") and (fp.current_value or "").strip() == (fp.value or "").strip():
                report.filled.append({"node": fp.node, "label": fp.label, "profile_key": fp.profile_key, "answer_id": fp.answer_id, "provenance": fp.provenance.value, "observed": fp.current_value, "ok": True, "message": "already filled"})
                continue
            action = self.action_for(fp)
            if action is None:
                report.skipped.append({"node": fp.node, "label": fp.label, "status": "unsupported"})
                continue
            payload = await resolve_payload(action) if action.type == "upload" else None
            result: ActionResult = await execute(action, payload)
            entry = {"node": fp.node, "label": fp.label, "profile_key": fp.profile_key, "answer_id": fp.answer_id, "provenance": fp.provenance.value, "observed": result.value, "ok": result.ok, "message": result.message}
            if result.ok:
                expected = (fp.value or "").strip()
                observed = str(result.value).strip() if result.value is not None else ""
                if fp.field_type == "checkbox":
                    ok = (observed.lower() == "true") == (expected.lower() == "yes")
                elif fp.field_type == "radio":
                    ok = observed.lower() in ("true", expected.lower())
                else:
                    ok = observed == expected or (fp.field_type == "file" and bool(observed))
                (report.filled if ok else report.failed).append(entry)
            else:
                report.failed.append(entry)
        return report
