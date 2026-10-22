"""HeuristicProvider: deterministic, offline answers for every model task.

It exists so the whole platform runs and is testable without a model runtime, and as a
conservative fallback. It never invents data: when it cannot map a field it returns a null
mapping with low confidence, which the workflow turns into a review/pause.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from .base import ModelProvider, ModelRequest, ModelResponse


class HeuristicProvider(ModelProvider):
    name = "heuristic"

    async def generate(self, request: ModelRequest, model: str) -> ModelResponse:
        t0 = time.perf_counter()
        handler = getattr(self, f"_task_{request.task}", None)
        if handler is None:
            out: dict[str, Any] = {}
        else:
            out = handler(request.inputs)
        return ModelResponse(text=json.dumps(out), provider=self.name, model=model or "rules", latency_ms=int((time.perf_counter() - t0) * 1000))

    # ------------------------------------------------------------------ navigator (offline fallback)
    def _task_choose_click(self, inputs: dict[str, Any]) -> dict[str, Any]:
        items = [str(x).lower() for x in inputs.get("items") or []]
        for i, name in enumerate(items):
            if re.match(r"^\s*(apply|easy apply|i'?m interested|continue to (the )?application)", name):
                return {"index": i, "reason": "apply-style label"}
        return {"index": None, "reason": "no apply control"}

    def _task_choose_items(self, inputs: dict[str, Any]) -> dict[str, Any]:
        items = [str(x).lower() for x in inputs.get("items") or []]
        hits = [i for i, t in enumerate(items) if re.search(r"/jobs?/|/careers?/|/positions?/|/vacanc|/opening|gh_jid=|lever\.co|ashbyhq|greenhouse|workday|smartrecruiters", t)]
        return {"indices": hits}

    # ------------------------------------------------------------------ assistant (offline fallback)
    def _task_ask(self, inputs: dict[str, Any]) -> dict[str, Any]:
        """Extractive answer: the page sentences sharing the most words with the question."""
        question = (inputs.get("question") or "").lower()
        text = inputs.get("text") or ""
        words = {w for w in re.findall(r"[a-z0-9]{4,}", question)}
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text) if len(s.strip()) > 20]
        if not sentences:
            return {"answer": "The page has no readable text."}
        ranked = sorted(sentences, key=lambda s: -len(words & set(re.findall(r"[a-z0-9]{4,}", s.lower()))))
        best = [s for s in ranked[:4] if words & set(re.findall(r"[a-z0-9]{4,}", s.lower()))] or sentences[:3]
        return {"answer": " ".join(best)[:1500]}

    # ------------------------------------------------------------------ intent
    def _task_intent(self, inputs: dict[str, Any]) -> dict[str, Any]:
        text = (inputs.get("instruction") or "").lower()
        url = inputs.get("url")
        if re.search(r"\b(apply|application|fill|submit)\b", text):
            return {"kind": "apply", "confidence": 0.8, "url": url, "notes": "keyword: apply/fill"}
        if re.search(r"\b(extract|import|score|save (this )?job|add (this )?job)\b", text):
            return {"kind": "extract_job", "confidence": 0.8, "url": url, "notes": "keyword: extract/import"}
        if re.search(r"\b(go to|open|navigate|visit)\b", text) or re.search(r"https?://", text):
            m = re.search(r"https?://\S+", text)
            return {"kind": "navigate", "confidence": 0.7, "url": m.group(0) if m else url, "notes": "keyword: navigate"}
        if re.search(r"\b(read|summari[sz]e|what (is|does)|tell me)\b", text):
            return {"kind": "read", "confidence": 0.7, "url": url, "notes": "keyword: read"}
        return {"kind": "general", "confidence": 0.4, "url": url, "notes": "no keyword match"}

    # ------------------------------------------------------------------ planner
    def _task_plan(self, inputs: dict[str, Any]) -> dict[str, Any]:
        """The heuristic planner only handles obvious cases; otherwise it asks the user."""
        kind = inputs.get("page_kind", "other")
        goal = inputs.get("goal", "")
        if kind in ("login", "challenge"):
            return {"page_state": kind, "actions": [{"type": "ask_user", "args": {"question": "Please log in / complete the verification in the browser, then resume."}, "risk": "low", "verification": "none", "timeout_ms": 1000}], "confidence": 0.9}
        if kind == "confirmation":
            return {"page_state": kind, "actions": [{"type": "done", "args": {"summary": "confirmation page reached"}, "risk": "low", "verification": "none", "timeout_ms": 1000}], "confidence": 0.9}
        if goal == "read" or goal == "extract_job":
            return {"page_state": kind, "actions": [{"type": "extract", "args": {}, "risk": "low", "verification": "none", "timeout_ms": 5000}, {"type": "done", "args": {"summary": "extracted"}, "risk": "low", "verification": "none", "timeout_ms": 1000}], "confidence": 0.8}
        # Look for an apply button on job description pages.
        if kind == "job_description" and goal == "apply":
            for n in inputs.get("nodes", []):
                if n.get("role") in ("button", "link") and re.search(r"\bapply\b", n.get("name", ""), re.I):
                    return {"page_state": kind, "actions": [{"type": "click", "target": {"strategy": "node", "value": str(n["i"])}, "args": {}, "risk": "low", "verification": "url_change", "timeout_ms": 15000, "expected_outcome": "application form opens"}], "confidence": 0.7}
        return {"page_state": kind, "actions": [{"type": "ask_user", "args": {"question": "I cannot determine the next step on this page without a model. Please guide me or complete it manually."}, "risk": "low", "verification": "none", "timeout_ms": 1000}], "confidence": 0.3}

    # ------------------------------------------------------------------ extraction
    def _task_extract_job(self, inputs: dict[str, Any]) -> dict[str, Any]:
        from ..jobs.extraction import heuristic_extract_from_text

        return heuristic_extract_from_text(inputs.get("title", ""), inputs.get("text", ""), inputs.get("url", ""))

    # ------------------------------------------------------------------ field mapping
    def _task_map_fields(self, inputs: dict[str, Any]) -> dict[str, Any]:
        from ..applications.field_mapping import heuristic_map_fields

        return {"mappings": heuristic_map_fields(inputs.get("fields", []), inputs.get("fact_keys", []), inputs.get("answers", []))}

    # ------------------------------------------------------------------ matching
    def _task_match_job(self, inputs: dict[str, Any]) -> dict[str, Any]:
        skills = [s.lower() for s in inputs.get("skills", [])]
        text = (inputs.get("description", "") + " " + " ".join(inputs.get("requirements", []))).lower()
        matched = [s for s in skills if s and re.search(r"(?<![a-z0-9])" + re.escape(s) + r"(?![a-z0-9])", text)]
        req_terms = {t for r in inputs.get("requirements", []) for t in re.findall(r"[a-zA-Z][a-zA-Z+#.]{2,}", r.lower())}
        missing = sorted(t for t in req_terms if t not in skills and t in text and len(t) > 3)[:10]
        score = (len(matched) / len(skills)) if skills else 0.0
        return {"semantic_score": round(min(1.0, score), 3), "matched_skills": matched[:30], "missing_skills": missing, "rationale": f"{len(matched)} of {len(skills)} profile skills appear in the description (keyword overlap, no model)."}

    # ------------------------------------------------------------------ recovery
    def _task_classify_recovery(self, inputs: dict[str, Any]) -> dict[str, Any]:
        msg = (inputs.get("message") or "").lower()
        if "password" in msg or "log in" in msg or "sign in" in msg:
            return {"category": "AUTH_REQUIRED", "retryable": False, "suggestion": "Log in manually and resume."}
        if "captcha" in msg or "verification" in msg:
            return {"category": "HUMAN_VERIFICATION_REQUIRED", "retryable": False, "suggestion": "Complete the challenge manually."}
        if "not found" in msg or "stale" in msg:
            return {"category": "ELEMENT_NOT_FOUND", "retryable": True, "suggestion": "Re-snapshot and retry with a semantic locator."}
        if "ambiguous" in msg or "equally good" in msg:
            return {"category": "AMBIGUOUS_TARGET", "retryable": False, "suggestion": "Add section context."}
        if "timeout" in msg or "navigation" in msg:
            return {"category": "NAVIGATION_FAILED", "retryable": True, "suggestion": "Retry after a short wait."}
        return {"category": "UNKNOWN", "retryable": False, "suggestion": "Inspect the audit log."}
