"""FormFiller: turns a snapshot + profile into a FillPlan and executes the auto-fillable part."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from ..browser.driver import BrowserDriver
from ..browser.snapshot import PageSnapshot
from ..browser.verifier import values_match
from ..core.actions import ActionResult, BrowserAction, Target
from ..core.enums import Provenance, RiskLevel
from ..models.base import ModelRequest
from ..models.schemas import AnswerOutput
from .field_mapping import _equivalent_option
from ..models.gateway import ModelGateway
from ..models.schemas import FieldMappingOutput, OptionPicksOutput
from .field_mapping import FieldPlan, FillPlan, build_fill_plan
from .profile import CandidateProfile

# Answers whose wording differs per site; the closest listed option is acceptable.
_LOOSE_KEYS = frozenset({"preferences.referral_source"})
_HONEYPOT_RE = re.compile(r"for robots|robots only|honeypot|do not (enter|fill)|leave (this )?(blank|empty)", re.I)

log = logging.getLogger("lfa.forms")


@dataclass(slots=True)
class FillReport:
    filled: list[dict[str, Any]] = field(default_factory=list)
    failed: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"filled": self.filled, "failed": self.failed, "skipped": self.skipped}


@dataclass(slots=True)
class _PendingMapping:
    task: "asyncio.Task[FieldMappingOutput]"
    label_of: dict[int, str]
    cache: dict[str, dict[str, Any]]


class FormFiller:
    def __init__(self, gateway: ModelGateway):
        self._gateway = gateway
        self._map_cache: dict[str, dict[str, dict[str, Any]]] = {}  # task id -> field label -> model mapping
        self._compose_cache: dict[tuple[str, str], str] = {}  # (task id, label) -> composed open answer

    async def plan(self, snap: PageSnapshot, profile: CandidateProfile, answers: list[dict[str, Any]], resumes: list[dict[str, Any]], *, submit_names: list[str], task_id: str | None = None, job_location: str | None = None, job: dict[str, Any] | None = None, model_budget_seconds: float | None = None) -> FillPlan:
        """Plan the fill from rules first; the model is asked only about fields the rules could not
        place, and only waited for up to ``model_budget_seconds`` (default: the budget setting).
        A plan returned with ``pending_model`` set is completed later by finish_plan()."""
        facts = profile.facts()
        # Bot traps ("Enter website. This input is for robots only") are left empty on purpose.
        fields = [n for n in snap.form_fields() if n.visible and not _HONEYPOT_RE.search(f"{n.name or ''} {n.placeholder or ''}")]
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
        pending: _PendingMapping | None = None
        if leftovers and self._gateway.role_config("field_mapper").provider != "heuristic":
            from ..agent.prompts import render_prompt

            inputs = {
                "fields": [{"node": n.i, "label": n.name or n.label or n.placeholder or n.name_attr or "", "type": n.type or n.role, "options": (n.options or [])[:12], "required": n.required, "section": n.section} for n in leftovers][:60],
                "fact_keys": sorted(facts.keys()),
                "answers": [{"id": a["id"], "question": a["question"], "category": a["category"]} for a in answers][:60],
            }
            system, prompt = render_prompt("map_fields", inputs)
            # The local model takes 40-120 s on this hardware. The rules' part of the form is filled
            # right away; the model keeps working in the background and its picks for the rest are
            # applied by finish_plan() once they arrive (or dropped when they are too late).
            budget = self._budget(model_budget_seconds)
            task = asyncio.create_task(self._gateway.complete("field_mapper", ModelRequest(task="map_fields", system=system, prompt=prompt, json_schema={}, inputs=inputs), FieldMappingOutput, task_id=task_id))
            try:
                out = await asyncio.wait_for(asyncio.shield(task), timeout=budget)
                model_mappings = (model_mappings or []) + self._remember(out, label_of, cache)
            except asyncio.TimeoutError:
                log.info("model field mapping still running after %.0fs: filling from the rules now, the model's picks follow when they arrive", budget)
                pending = _PendingMapping(task=task, label_of=label_of, cache=cache)
            except Exception as exc:
                log.warning("model field mapping failed; rules only: %s", exc)
        plan = build_fill_plan(snap.nodes, facts, answers, model_mappings, resumes=resumes, default_resume_id=profile.default_resume_id, submit_names=submit_names, job_location=job_location)
        plan.pending_model = pending
        await self._pick_options_with_model(plan, task_id=task_id)
        await self._compose_open_answers(plan, profile, job, task_id=task_id)
        return plan

    async def _pick_options_with_model(self, plan: FillPlan, *, task_id: str | None) -> None:
        """Dropdowns whose options do not literally contain the candidate's answer ("Company
        careers website" vs "Career Site", "Job Board", "Referral"…): the model reads the options
        like a person and picks the one that means the same, or none. One bounded call for all
        such fields; a required field is worth waiting the grace for, an optional one the budget."""
        if self._gateway.role_config("field_mapper").provider == "heuristic":
            return
        targets = [f for f in plan.fields if f.status == "needs_review" and f.value and f.options and (f.reason or "").startswith("value '") and "does not match any option" in (f.reason or "")]
        if not targets:
            return
        listing = "\n".join(f"- node {f.node}: question \"{f.label[:100]}\"; the candidate's answer: \"{f.value[:100]}\"; options: {[o[:60] for o in (f.options or [])[:40]]}" for f in targets[:20])
        system = (
            "You fill job-application forms for a candidate. For each dropdown question below, choose the ONE listed option that means the same as "
            "the candidate's answer (or is the closest honest match: 'Company careers website' → 'Career Site'; 'LinkedIn' → 'Job Board' only if no LinkedIn option exists). "
            "If no option fits, leave that question out. Copy the option text exactly. "
            'Reply only JSON: {"picks": [{"node": <number>, "option": "<exact option text>"}]}.'
        )
        wait = self.grace_seconds() if any(f.required for f in targets) else self._budget(None)
        try:
            out = await asyncio.wait_for(self._gateway.complete("field_mapper", ModelRequest(task="pick_option", system=system, prompt=listing, json_schema={}, inputs={"nodes": [f.node for f in targets]}), OptionPicksOutput, task_id=task_id, max_output_tokens=400), timeout=wait)
        except Exception as exc:  # noqa: BLE001 - the fields stay for review / unattended handling
            log.info("option picking by the model unavailable: %s", exc)
            return
        by_node = {f.node: f for f in targets}
        for p in out.picks:
            f = by_node.get(int(p.node))
            if f is None or not f.options:
                continue
            chosen = next((o for o in f.options if o.strip().lower() == p.option.strip().lower()), None) or _equivalent_option(f.options, p.option)
            if chosen is None:
                continue
            f.value, f.status, f.reason = chosen, "auto", f"closest option to '{f.value[:40]}' (model)"
            f.provenance = Provenance.INFERRED
            f.confidence = max(f.confidence, 0.7)

    def _budget(self, override: float | None) -> float:
        if override is not None:
            return max(0.0, float(override))
        s = getattr(self._gateway, "_settings", None)
        return float(getattr(s, "field_mapper_budget_seconds", 15.0))

    def grace_seconds(self) -> float:
        """How long a run waits for the model's late picks after the rules-only fill."""
        s = getattr(self._gateway, "_settings", None)
        return float(getattr(s, "field_mapper_grace_seconds", 45.0))

    @staticmethod
    def _remember(out: FieldMappingOutput, label_of: dict[int, str], cache: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        fresh = [m.model_dump() for m in out.mappings]
        for m in fresh:
            lbl = label_of.get(m.get("node"))
            if lbl:
                cache[lbl] = {k: v for k, v in m.items() if k != "node"}
        return fresh

    def discard_pending(self, plan: FillPlan) -> None:
        """Drop a still-running model mapping (the run is over; it must not hold the model queue)."""
        pending = plan.pending_model
        plan.pending_model = None
        if pending is not None and not pending.task.done():
            pending.task.cancel()

    async def finish_plan(self, plan: FillPlan, snap: PageSnapshot, profile: CandidateProfile, answers: list[dict[str, Any]], resumes: list[dict[str, Any]], *, submit_names: list[str], job_location: str | None = None, extra_wait: float | None = None) -> FillPlan | None:
        """Apply the model's field picks that were still being computed when the rules-only plan
        was filled: waits up to ``extra_wait`` seconds (default: the grace setting) for them, then
        returns a plan holding only the fields that became fillable, or None. The original plan's
        fields are updated in place so reports and the dashboard show the final statuses."""
        pending = plan.pending_model
        if pending is None:
            return None
        plan.pending_model = None
        wait = self.grace_seconds() if extra_wait is None else max(0.0, extra_wait)
        try:
            out = pending.task.result() if pending.task.done() else await asyncio.wait_for(asyncio.shield(pending.task), timeout=wait)
        except asyncio.TimeoutError:
            log.info("model field mapping not back after another %.0fs; the remaining fields are left for the user", wait)
            pending.task.cancel()
            return None
        except Exception as exc:  # noqa: BLE001 - the rules' plan stands
            log.warning("model field mapping failed; rules only: %s", exc)
            return None
        fresh = self._remember(out, pending.label_of, pending.cache)
        full = build_fill_plan(snap.nodes, profile.facts(), answers, fresh, resumes=resumes, default_resume_id=profile.default_resume_id, submit_names=submit_names, job_location=job_location)
        settled = {f.node for f in plan.fields if f.status in ("auto", "skip") and f.value is not None or f.status == "auto"}
        by_node = {f.node: i for i, f in enumerate(plan.fields)}
        extra: list[FieldPlan] = []
        for f in full.fields:
            if f.node in settled or f.status in ("needs_input", "skip"):
                continue
            i = by_node.get(f.node)
            if i is None:
                continue
            was = plan.fields[i]
            if was.status == "auto" or (was.status == "needs_review" and was.value):
                continue  # the rules already had an answer for it
            plan.fields[i] = f
            extra.append(f)
        if not extra:
            return None
        return FillPlan(fields=extra, submit_node=plan.submit_node, next_node=plan.next_node)

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
                # Bounded: on this hardware a 100-word answer can take a minute; past the grace the
                # candidate's stored stance is used as it is.
                out = await asyncio.wait_for(self._gateway.complete(role, ModelRequest(task="compose", system=system, prompt=prompt, json_schema={}, inputs={"label": f.label}), AnswerOutput, task_id=task_id, use_cache=False, max_output_tokens=400), timeout=self.grace_seconds())
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
