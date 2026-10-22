"""FormFiller: turns a snapshot + profile into a FillPlan and executes the auto-fillable part."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from ..browser.driver import BrowserDriver
from ..browser.snapshot import PageSnapshot
from ..browser.verifier import values_match
from ..core.actions import ActionResult, BrowserAction, Target
from ..core.enums import RiskLevel
from ..models.base import ModelRequest
from ..models.schemas import AnswerOutput
from .field_mapping import _equivalent_option
from ..models.gateway import ModelGateway
from ..models.schemas import FieldMappingOutput
from .field_mapping import FieldPlan, FillPlan, build_fill_plan
from .profile import CandidateProfile

# Answers whose wording differs per site; the closest listed option is acceptable.
_LOOSE_KEYS = frozenset({"preferences.referral_source"})

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
        self._map_cache: dict[str, dict[str, dict[str, Any]]] = {}  # task id -> field label -> model mapping
        self._compose_cache: dict[tuple[str, str], str] = {}  # (task id, label) -> composed open answer

    async def plan(self, snap: PageSnapshot, profile: CandidateProfile, answers: list[dict[str, Any]], resumes: list[dict[str, Any]], *, submit_names: list[str], task_id: str | None = None, job_location: str | None = None, job: dict[str, Any] | None = None) -> FillPlan:
        facts = profile.facts()
        fields = [n for n in snap.form_fields() if n.visible]
        model_mappings: list[dict[str, Any]] | None = None
        # Rules first; the model is consulted only for fields the rules could not place.
        from .field_mapping import heuristic_map_fields

        rule_inputs = [{"node": n.i, "label": n.name or n.label or n.placeholder or n.name_attr or "", "type": n.type or n.role, "options": n.options, "required": n.required} for n in fields]
        unmapped = {m["node"] for m in heuristic_map_fields(rule_inputs, list(facts.keys()), answers) if not m.get("profile_key") and not m.get("answer_id") and m.get("category") not in ("upload", "consent", "legal")}
        leftovers = [n for n in fields if n.i in unmapped]
        # Mappings the model produced earlier in this task are reused by label: a form redrawn
        # after an upload, or the next pass of a multi-step form, costs no second model call.
        cache = self._map_cache.setdefault(task_id or "", {})
        label_of = {n.i: (n.name or n.label or n.placeholder or n.name_attr or "") for n in fields}
        reused = [{**cache[label_of[n.i]], "node": n.i} for n in leftovers if label_of[n.i] in cache]
        leftovers = [n for n in leftovers if label_of[n.i] not in cache]
        if reused:
            model_mappings = reused
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
                fresh = [m.model_dump() for m in out.mappings]
                for m in fresh:
                    lbl = label_of.get(m.get("node"))
                    if lbl:
                        cache[lbl] = {k: v for k, v in m.items() if k != "node"}
                model_mappings = (model_mappings or []) + fresh
            except Exception as exc:
                log.warning("model field mapping failed; rules only: %s", exc)
        plan = build_fill_plan(snap.nodes, facts, answers, model_mappings, resumes=resumes, default_resume_id=profile.default_resume_id, submit_names=submit_names, job_location=job_location)
        await self._compose_open_answers(plan, profile, job, task_id=task_id)
        return plan

    _OPEN_QUESTION_KEYS = ("preferences.why_join",)

    async def _compose_open_answers(self, plan: FillPlan, profile: CandidateProfile, job: dict[str, Any] | None, *, task_id: str | None) -> None:
        """"Why us?" / "What motivates you?" boxes get a short answer written for THIS job: the
        model reads the posting (title, company, description) and the candidate's background and
        says what genuinely fits, in the candidate's voice. The stored stance is the fallback."""
        targets = [f for f in plan.fields if f.profile_key in self._OPEN_QUESTION_KEYS and f.field_type in ("textarea", "text") and not f.options]
        # The assistant model when one is configured, else the extractor (the local Ollama model).
        role = "assistant" if self._gateway.role_config("assistant").provider != "heuristic" else "extractor"
        if not targets or not job or self._gateway.role_config(role).provider == "heuristic":
            return
        for f in targets:
            key = (task_id or "", f.label)
            cached = self._compose_cache.get(key)
            if cached:
                f.value, f.status, f.reason = cached, "auto", "written for this job (cached)"
                continue
            facts = profile.facts()
            background = facts.get("summary", ("", None))[0] or facts.get("employment[0].title", ("", None))[0]
            system = (
                "You write answers for job-application questions on behalf of a candidate, in the first person. "
                "Read the job posting and the candidate's background; say concretely what about THIS company and role "
                "attracts the candidate (product/domain, scale, technology, growth) and how their background fits. "
                "60-110 words, plain and specific, no clichés, no claims that are not in the background. "
                'Reply only JSON: {"answer": "<text>"}.'
            )
            prompt = (
                f"Question on the form: {f.label}\n"
                f"Job title: {job.get('title') or ''}\nCompany: {job.get('company') or ''}\nLocation: {job.get('location') or ''}\n"
                f"Job description (excerpt): {(job.get('description') or '')[:1800]}\n\n"
                f"Candidate background: {background}\nCandidate's own stance: {f.value or ''}\n"
                f"Skills: {facts.get('skills', ('', None))[0][:300]}"
            )
            try:
                out = await self._gateway.complete(role, ModelRequest(task="compose", system=system, prompt=prompt, json_schema={}, inputs={"label": f.label}), AnswerOutput, task_id=task_id, use_cache=False, max_output_tokens=400)
                text = (out.answer or "").strip()
            except Exception as exc:  # noqa: BLE001 - keep the stored stance
                log.info("could not compose an answer for %r: %s", f.label, exc)
                continue
            if 40 <= len(text) <= 1200:
                self._compose_cache[key] = text
                f.value, f.status, f.reason = text, "auto", "written for this job from the posting and your background"
                f.confidence = max(f.confidence, 0.8)

    @staticmethod
    def action_for(fp: FieldPlan) -> BrowserAction | None:
        target = Target(strategy="node", value=str(fp.node))
        if fp.field_type == "file":
            return BrowserAction(type="upload", target=target, args={"resume_id": fp.resume_id or ""}, risk=RiskLevel.MEDIUM, verification="value_set", timeout_ms=20_000)
        if fp.field_type in ("select", "select-multiple", "combobox", "listbox"):
            args: dict[str, Any] = {"value": fp.value or ""}
            if fp.profile_key in _LOOSE_KEYS:
                args["loose"] = True  # "Company website" may be listed as "Careers page" etc.
            return BrowserAction(type="select", target=target, args=args, risk=RiskLevel.MEDIUM, verification="none" if args.get("loose") else "value_set")
        if fp.field_type == "radio":
            if fp.option_nodes and fp.value in fp.option_nodes:
                return BrowserAction(type="check", target=Target(strategy="node", value=str(fp.option_nodes[fp.value])), args={"checked": True}, risk=RiskLevel.MEDIUM, verification="value_set")
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
            prefilled = fp.current_value not in (None, "")
            # A value the site already holds (typed earlier, or parsed from the resume: "GGSIP
            # University" for GGSIPU) is kept rather than cleared and re-typed.
            if fp.field_type != "file" and prefilled and (values_match("text", fp.value or "", fp.current_value or "") or (fp.field_type == "combobox" and _equivalent_option([fp.current_value or ""], fp.value or "") is not None)):
                report.filled.append({"node": fp.node, "label": fp.label, "profile_key": fp.profile_key, "answer_id": fp.answer_id, "provenance": fp.provenance.value, "observed": fp.current_value, "ok": True, "message": "already filled"})
                continue
            action = self.action_for(fp)
            if action is None:
                report.skipped.append({"node": fp.node, "label": fp.label, "status": "unsupported"})
                continue
            if action.type == "select" and "\n" in (fp.value or ""):
                # Multi-select dropdown ("select all that apply"): pick each value in turn.
                picked: list[str] = []
                failed_msg = ""
                for part in [p for p in (fp.value or "").split("\n") if p.strip()]:
                    r = await execute(action.model_copy(update={"args": {"value": part}}), None)
                    shown = [s.strip() for s in str(r.value or "").split(",") if s.strip()]
                    if r.ok and any(values_match("select", part, s) for s in shown):
                        picked.append(part)
                    else:
                        failed_msg = r.message or f"could not pick '{part}'"
                entry = {"node": fp.node, "label": fp.label, "profile_key": fp.profile_key, "answer_id": fp.answer_id, "provenance": fp.provenance.value, "observed": ", ".join(picked), "ok": not failed_msg, "message": failed_msg}
                (report.failed if failed_msg else report.filled).append(entry)
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
                elif fp.field_type == "file" or (fp.profile_key in _LOOSE_KEYS and action.type == "select"):
                    ok = bool(observed)
                else:
                    ok = values_match("select" if action.type == "select" else "text", expected, observed)
                (report.filled if ok else report.failed).append(entry)
            else:
                report.failed.append(entry)
        return report
