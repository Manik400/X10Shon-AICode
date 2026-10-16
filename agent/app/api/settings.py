from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..scheduler import SchedulerConfig
from .deps import Auth, Ctx

router = APIRouter(prefix="/api/settings", tags=["settings"])


class EvalBody(BaseModel):
    roles: dict[str, dict[str, Any]] | None = Field(default=None, description="Optional role overrides to evaluate")


@router.get("")
async def get_settings(ctx: Ctx, _: Auth) -> dict:
    s = ctx.settings
    return {
        "scheduler": ctx.scheduler.config.model_dump(),
        "model_roles": {r: c.model_dump() for r, c in s.model_roles.items()},
        "runtime": {
            "host": s.host,
            "port": s.port,
            "data_dir": str(s.data_dir),
            "max_actions_per_task": s.max_actions_per_task,
            "action_timeout_seconds": s.action_timeout_seconds,
            "task_deadline_seconds": s.task_deadline_seconds,
            "snapshot_max_nodes": s.snapshot_max_nodes,
            "max_concurrent_pages": s.max_concurrent_pages,
            "model_max_concurrency": s.model_max_concurrency,
            "model_context_chars": s.model_context_chars,
            "model_max_response_bytes": s.model_max_response_bytes,
            "queue_size": s.queue_size,
            "ollama_base_url": s.ollama_base_url,
            "openai_compatible_base_url": s.openai_compatible_base_url,
        },
        "allowed_domains": ctx.policy.allowed_domains,
        "adapters": ctx.adapters.describe(),
    }


@router.put("")
async def put_settings(body: SchedulerConfig, ctx: Ctx, _: Auth) -> dict:
    for role, cfg in body.model_roles.items():
        if cfg.get("provider") not in ("ollama", "openai_compatible", "heuristic"):
            raise HTTPException(400, f"invalid provider for role {role}")
    cfg = await ctx.scheduler.save(body)
    if body.model_roles:
        ctx.gateway.update_roles(body.model_roles)
    ctx.settings.max_concurrent_tasks = cfg.max_concurrent_tasks
    await ctx.audit.log("settings.updated")
    return {"scheduler": cfg.model_dump(), "model_roles": {r: c.model_dump() for r, c in ctx.settings.model_roles.items()}}


@router.get("/models")
async def models(ctx: Ctx, _: Auth) -> dict:
    health = await ctx.gateway.health()
    return health


@router.post("/models/evaluate")
async def evaluate(body: EvalBody, ctx: Ctx, _: Auth) -> dict:
    from ..models.evaluation import run_evaluation

    return await run_evaluation(ctx.gateway, roles_override=body.roles)
