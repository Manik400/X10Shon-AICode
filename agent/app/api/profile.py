from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, UploadFile
from pydantic import BaseModel, Field

from ..applications.profile import CandidateProfile
from ..core.util import new_id, sha256_hex
from .deps import Auth, Ctx

router = APIRouter(prefix="/api/profile", tags=["profile"])

_ALLOWED_RESUME_TYPES = {"application/pdf": ".pdf", "application/msword": ".doc", "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx", "text/plain": ".txt"}
_MAX_RESUME_BYTES = 5 * 1024 * 1024


class AnswerBody(BaseModel):
    id: str | None = None
    category: str = Field(min_length=1, max_length=64)
    question: str = Field(min_length=1, max_length=500)
    answer: str = Field(min_length=1, max_length=4000)
    provenance: str = Field(default="approved_answer", pattern="^(verified_fact|approved_answer|inferred)$")
    confidence: float = Field(default=1.0, ge=0, le=1)
    legally_significant: bool = False


class ResumeUpdate(BaseModel):
    label: str | None = Field(default=None, max_length=120)
    targeting: dict[str, Any] | None = None
    make_default: bool = False


@router.get("")
async def get_profile(ctx: Ctx, _: Auth) -> dict:
    raw = await ctx.repos.profile.get() or {}
    profile = CandidateProfile.model_validate({k: v for k, v in raw.items() if k != "updated_at"})
    return {"profile": profile.model_dump(), "updated_at": raw.get("updated_at"), "completeness": profile.completeness()}


@router.put("")
async def put_profile(body: CandidateProfile, ctx: Ctx, _: Auth) -> dict:
    await ctx.repos.profile.save(body.model_dump())
    await ctx.audit.log("profile.updated")
    return {"profile": body.model_dump(), "completeness": body.completeness()}


@router.get("/answers")
async def list_answers(ctx: Ctx, _: Auth, category: str | None = None) -> dict:
    return {"items": await ctx.repos.profile.list_answers(category)}


@router.post("/answers")
async def upsert_answer(body: AnswerBody, ctx: Ctx, _: Auth) -> dict:
    saved = await ctx.repos.profile.upsert_answer(body.model_dump())
    await ctx.audit.log("profile.answer_saved", category=body.category)
    return saved


@router.delete("/answers/{answer_id}")
async def delete_answer(answer_id: str, ctx: Ctx, _: Auth) -> dict:
    return {"ok": await ctx.repos.profile.delete_answer(answer_id)}


@router.get("/resumes")
async def list_resumes(ctx: Ctx, _: Auth) -> dict:
    return {"items": await ctx.repos.profile.list_resumes()}


@router.post("/resumes")
async def upload_resume(ctx: Ctx, _: Auth, file: UploadFile, label: str | None = None) -> dict:
    data = await file.read()
    if not data:
        raise HTTPException(400, "empty file")
    if len(data) > _MAX_RESUME_BYTES:
        raise HTTPException(413, "resume exceeds 5 MB")
    mime = file.content_type or "application/octet-stream"
    ext = _ALLOWED_RESUME_TYPES.get(mime) or Path(file.filename or "").suffix.lower()
    if ext not in _ALLOWED_RESUME_TYPES.values():
        raise HTTPException(415, "unsupported resume type (pdf, doc, docx, txt)")
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", file.filename or f"resume{ext}")[:120]
    rid = new_id()
    dest_dir = ctx.settings.data_dir / "resumes"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{rid}_{safe_name}"
    dest.write_bytes(data)
    resume = await ctx.repos.profile.add_resume({"id": rid, "label": label or safe_name, "file_path": str(dest), "file_name": safe_name, "size_bytes": len(data), "sha256": sha256_hex(data), "mime_type": mime, "targeting": {}})
    profile = await ctx.repos.profile.get() or {}
    if not profile.get("default_resume_id"):
        profile.pop("updated_at", None)
        profile["default_resume_id"] = rid
        await ctx.repos.profile.save(profile)
    await ctx.audit.log("profile.resume_added", resume_id=rid, size=len(data))
    return resume


@router.put("/resumes/{resume_id}")
async def update_resume(resume_id: str, body: ResumeUpdate, ctx: Ctx, _: Auth) -> dict:
    updated = await ctx.repos.profile.update_resume(resume_id, label=body.label, targeting=body.targeting)
    if updated is None:
        raise HTTPException(404, "resume not found")
    if body.make_default:
        profile = await ctx.repos.profile.get() or {}
        profile.pop("updated_at", None)
        profile["default_resume_id"] = resume_id
        await ctx.repos.profile.save(profile)
    return updated


@router.delete("/resumes/{resume_id}")
async def delete_resume(resume_id: str, ctx: Ctx, _: Auth) -> dict:
    resume = await ctx.repos.profile.get_resume(resume_id)
    if resume is None:
        raise HTTPException(404, "resume not found")
    ok = await ctx.repos.profile.delete_resume(resume_id)
    try:
        Path(resume["file_path"]).unlink(missing_ok=True)
    except OSError:
        pass
    return {"ok": ok}
