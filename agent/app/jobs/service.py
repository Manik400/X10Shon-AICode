"""JobService: import, canonicalize, deduplicate, extract, filter, score, persist."""

from __future__ import annotations

import csv
import io
import logging
from typing import Any

from ..applications.identity import identity_key_for
from ..core.enums import ApplicationState
from ..core.util import now_iso
from ..models.schemas import JobExtraction
from ..observability.audit import AuditLogger
from ..persistence.unit import Repositories
from .adapters import AdapterRegistry
from .canonical import canonicalize_url, dedup_key, detect_requisition, fuzzy_key
from .extraction import JobExtractor
from .matching import JobFilters, JobMatcher, apply_filters

log = logging.getLogger("lfa.jobs")


class JobService:
    def __init__(self, repos: Repositories, extractor: JobExtractor, matcher: JobMatcher, adapters: AdapterRegistry, audit: AuditLogger):
        self._repos = repos
        self._extractor = extractor
        self._matcher = matcher
        self._adapters = adapters
        self._audit = audit

    # ------------------------------------------------------------------ settings
    async def filters(self) -> JobFilters:
        raw = await self._repos.settings.get("job_filters", {})
        return JobFilters.model_validate(raw or {})

    async def save_filters(self, filters: JobFilters) -> None:
        await self._repos.settings.set("job_filters", filters.model_dump())

    # ------------------------------------------------------------------ import
    async def import_url(self, url: str, *, source_kind: str = "url", source_id: str | None = None, extraction: JobExtraction | None = None, prefill: dict[str, Any] | None = None, page_fetcher=None) -> tuple[dict[str, Any], bool]:  # type: ignore[no-untyped-def]
        """Import one job. Returns (job, is_duplicate). Extraction may be supplied by the caller
        (e.g. from a page snapshot), fetched through an adapter's official API, or, when
        ``page_fetcher`` is given, extracted from a headless snapshot of the public page."""
        canonical = canonicalize_url(url)
        ats, req = detect_requisition(canonical)
        key = dedup_key(canonical, requisition_id=(prefill or {}).get("requisition_id") or req)
        existing = await self._repos.jobs.find_by_dedup_key(key)
        if existing:
            await self._audit.log("job.duplicate", job_id=existing["id"], url=canonical, reason="dedup_key")
            return existing, True

        data: dict[str, Any] = {"original_url": url, "canonical_url": canonical, "dedup_key": key, "source_kind": source_kind, "source_id": source_id, "requisition_id": req}
        if prefill:
            data.update({k: v for k, v in prefill.items() if v is not None})
        if extraction is None:
            adapter = self._adapters.for_url(canonical)
            try:
                extraction = await adapter.extract(canonical, None)
                if extraction:
                    data["extraction_method"] = "api"
            except Exception as exc:
                log.warning("adapter extraction failed for %s: %s", canonical, exc)
            if extraction is None and page_fetcher is not None:
                try:
                    snap = await page_fetcher(canonical)
                    if snap is not None:
                        extraction, method = await self._extractor.extract(snap)
                        data["extraction_method"] = method
                except Exception as exc:
                    log.warning("page fetch for %s failed: %s", canonical, exc)
        if extraction is not None:
            for k, v in extraction.model_dump().items():
                if k == "confidence" or v in (None, [], ""):
                    continue
                if k == "remote_type" and v == "unknown" and data.get("remote_type"):
                    continue
                data[k] = v
            data.setdefault("extraction_method", "snapshot")
        data.setdefault("apply_url", canonical)

        # Secondary fuzzy duplicate: same normalized company + title + location already imported.
        if data.get("company") and data.get("title"):
            data["fuzzy_key"] = fuzzy_key(data.get("company"), data.get("title"), data.get("location"))
            for cand in await self._repos.jobs.find_by_fuzzy_key(data["fuzzy_key"]):
                if not (cand.get("requisition_id") and data.get("requisition_id") and cand["requisition_id"] != data["requisition_id"]):
                    data["duplicate_of"] = cand["id"]
                    break

        job = await self._repos.jobs.insert(data)
        if data.get("duplicate_of"):
            app = await self._repos.applications.create(job["id"], identity_key_for(job, suffix="dup:" + job["id"]), ApplicationState.DUPLICATE, application_url=job.get("apply_url"), reason="fuzzy duplicate of " + data["duplicate_of"], actor="agent")
            await self._audit.log("job.duplicate", job_id=job["id"], application_id=app["id"], duplicate_of=data["duplicate_of"], reason="company+title+location")
            return job, True

        await self._audit.log("job.imported", job_id=job["id"], url=canonical, method=data.get("extraction_method"))
        await self.screen(job["id"])
        return await self._repos.jobs.get(job["id"]) or job, False

    async def import_from_snapshot(self, snapshot, *, source_kind: str = "manual", task_id: str | None = None) -> tuple[dict[str, Any], bool]:  # type: ignore[no-untyped-def]
        extraction, method = await self._extractor.extract(snapshot, task_id=task_id)
        job, dup = await self.import_url(snapshot.url, source_kind=source_kind, extraction=extraction, prefill={"extraction_method": method})
        return job, dup

    async def import_csv(self, content: str, *, source_id: str | None = None) -> dict[str, Any]:
        reader = csv.DictReader(io.StringIO(content))
        imported, duplicates, errors = 0, 0, []
        for i, row in enumerate(reader, start=2):
            url = (row.get("url") or row.get("URL") or row.get("link") or "").strip()
            if not url:
                errors.append(f"row {i}: missing url")
                continue
            prefill = {k: (row.get(k) or "").strip() or None for k in ("title", "company", "location")}
            try:
                _, dup = await self.import_url(url, source_kind="csv", source_id=source_id, prefill=prefill)
                duplicates += int(dup)
                imported += int(not dup)
            except Exception as exc:
                errors.append(f"row {i}: {str(exc)[:120]}")
        return {"imported": imported, "duplicates": duplicates, "errors": errors}

    async def run_source(self, source_id: str) -> dict[str, Any]:
        src = await self._repos.jobs.get_source(source_id)
        if not src:
            raise KeyError(source_id)
        adapter = self._adapters.by_name(src["kind"])
        if adapter is None or "discover" not in adapter.capabilities.supported_operations:
            raise ValueError(f"source kind '{src['kind']}' cannot discover jobs")
        try:
            listings = await adapter.discover(src.get("config_json") or {})
        except Exception as exc:
            await self._repos.jobs.update_source(source_id, last_run_at=now_iso(), last_error=str(exc)[:300])
            raise
        imported = duplicates = 0
        for item in listings:
            if not item.get("url"):
                continue
            extraction = None
            raw = item.get("raw")
            if raw and hasattr(adapter, "from_api_json"):
                try:
                    extraction = adapter.from_api_json(raw, item["url"], company_hint=item.get("company"))  # type: ignore[attr-defined]
                except Exception:
                    extraction = None
            _, dup = await self.import_url(item["url"], source_kind=src["kind"], source_id=source_id, extraction=extraction, prefill={k: item.get(k) for k in ("title", "company", "location", "requisition_id", "posted_at")})
            duplicates += int(dup)
            imported += int(not dup)
        await self._repos.jobs.update_source(source_id, last_run_at=now_iso(), last_error=None)
        return {"listed": len(listings), "imported": imported, "duplicates": duplicates}

    # ------------------------------------------------------------------ screening
    async def screen(self, job_id: str, *, use_model: bool = True) -> dict[str, Any]:
        job = await self._repos.jobs.get(job_id)
        if job is None:
            raise KeyError(job_id)
        filters = await self.filters()
        profile = await self._repos.profile.get() or {}
        fres = apply_filters(job, filters)
        score, explanation = await self._matcher.score(job, profile, filters, use_model=use_model)
        passed = fres.passed and score >= filters.min_match_score
        if fres.passed and score < filters.min_match_score:
            fres.reasons.append(f"match score {score} below minimum {filters.min_match_score}")
        await self._repos.jobs.update(job_id, {"match_score": score, "match_explanation": explanation, "filter_result": fres.to_dict()})

        app = await self._repos.applications.get_by_job(job_id)
        if app is None:
            app = await self._repos.applications.create(job_id, identity_key_for(job), ApplicationState.IMPORTED, application_url=job.get("apply_url"), reason="imported")
        if app["state"] in (ApplicationState.IMPORTED, ApplicationState.SCREENING, ApplicationState.ELIGIBLE, ApplicationState.SKIPPED):
            if app["state"] != ApplicationState.SCREENING:
                app = await self._repos.applications.transition(app["id"], ApplicationState.SCREENING, reason="screening")
            target = ApplicationState.ELIGIBLE if passed else ApplicationState.SKIPPED
            app = await self._repos.applications.transition(app["id"], target, reason=("passed filters" if passed else "; ".join(fres.reasons)[:400]))
        await self._audit.log("job.screened", job_id=job_id, application_id=app["id"], score=score, passed=passed, reasons=fres.reasons)
        return {"job": await self._repos.jobs.get(job_id), "application": app, "score": score, "passed": passed, "reasons": fres.reasons}

    async def rescreen_all(self) -> int:
        from ..persistence.repositories import Page

        n = 0
        offset = 0
        while True:
            rows, _ = await self._repos.jobs.list(Page(limit=200, offset=offset), include_duplicates=False)
            if not rows:
                break
            for r in rows:
                if r.get("application_state") in (None, "IMPORTED", "SCREENING", "ELIGIBLE", "SKIPPED"):
                    await self.screen(r["id"], use_model=False)
                    n += 1
            offset += 200
        return n
