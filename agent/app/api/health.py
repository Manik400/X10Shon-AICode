from __future__ import annotations

from fastapi import APIRouter

from .. import __version__
from .deps import Auth, Ctx

router = APIRouter(prefix="/api", tags=["health"])


@router.get("/health")
async def health(ctx: Ctx, _: Auth) -> dict:
    version = await ctx.db.schema_version()
    return {
        "ok": True,
        "version": __version__,
        "started_at": ctx.started_at,
        "db": {"path": str(ctx.db.path), "schema_version": version},
        "extension": ctx.hub.status(),
        "scheduler": ctx.scheduler.status(),
        "paused": ctx.policy.paused,
        "resources": ctx.resources.snapshot().to_dict(),
        "running_tasks": ctx.tasks.running_ids,
    }


@router.get("/health/models")
async def model_health(ctx: Ctx, _: Auth) -> dict:
    return await ctx.gateway.health()
