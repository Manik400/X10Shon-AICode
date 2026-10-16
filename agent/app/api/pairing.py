from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ..security.auth import CLIENT_HEADER, trusted_dashboard_origins
from .deps import Auth, Ctx

router = APIRouter(prefix="/api/pairing", tags=["pairing"])


class ClaimBody(BaseModel):
    code: str = Field(min_length=8, max_length=8)
    extension_id: str | None = Field(default=None, max_length=64)


@router.post("/code")
async def issue_code(ctx: Ctx, principal: Auth) -> dict:
    if principal.kind != "dashboard":
        raise HTTPException(403, "only the dashboard can issue pairing codes")
    code = ctx.pairing.issue_code()
    await ctx.audit.log("pairing.code_issued")
    return code


@router.post("/claim")
async def claim(body: ClaimBody, request: Request, ctx: Ctx) -> dict:
    """Unauthenticated by design (the extension has no token yet). Protected by: loopback bind,
    8-char short-lived code, 5 failures/minute limit, origin must be chrome-extension or trusted."""
    origin = request.headers.get("origin", "")
    if origin and not origin.startswith("chrome-extension://") and origin not in trusted_dashboard_origins(ctx.settings):
        raise HTTPException(403, "untrusted origin")
    if request.headers.get(CLIENT_HEADER) is None:
        raise HTTPException(401, f"missing {CLIENT_HEADER} header")
    ext_id = body.extension_id or (origin.removeprefix("chrome-extension://") if origin.startswith("chrome-extension://") else None)
    if body.code.upper() == "AUTOPAIR":
        # Code-less pairing for the extension loaded unpacked from this repository's own
        # extension/dist on this machine (origin must match the path-derived id, and no active
        # pairing may exist for it yet).
        from ..security.extension_id import repo_extension_dist, unpacked_extension_id

        expected = unpacked_extension_id(repo_extension_dist())
        origin_id = origin.removeprefix("chrome-extension://") if origin.startswith("chrome-extension://") else None
        if origin_id is None or origin_id != expected:
            await ctx.audit.log("pairing.autopair_refused", level="warning", origin=origin or None)
            raise HTTPException(403, "automatic pairing is only available to the extension loaded from this project's extension/dist folder")
        # Only the genuine build can present this origin, so a re-claim (after a database reset
        # or a lost token) replaces any earlier pairing for the same id instead of being refused.
        for p in await ctx.repos.pairings.list():
            if p.get("extension_id") == origin_id and not p.get("revoked_at"):
                await ctx.repos.pairings.revoke(p["id"])
        token = await ctx.pairing.auto_claim(origin_id)
    else:
        token = await ctx.pairing.claim(body.code, ext_id)
    if token is None:
        await ctx.audit.log("pairing.claim_failed", level="warning")
        raise HTTPException(400, "invalid or expired pairing code")
    pairing = await ctx.repos.pairings.find_by_token(token)
    await ctx.audit.log("pairing.claimed", pairing_id=pairing["id"] if pairing else None)
    return {"token": token, "pairing_id": pairing["id"] if pairing else None}


@router.get("")
async def list_pairings(ctx: Ctx, _: Auth) -> dict:
    from ..security.extension_id import repo_extension_dist, unpacked_extension_id

    dist = repo_extension_dist()
    return {"items": await ctx.repos.pairings.list(), "pending_code": ctx.pairing.has_pending(), "unpacked": {"path": str(dist), "expected_extension_id": unpacked_extension_id(dist)}}


@router.delete("/{pairing_id}")
async def revoke(pairing_id: str, ctx: Ctx, principal: Auth) -> dict:
    if principal.kind != "dashboard":
        raise HTTPException(403, "only the dashboard can revoke pairings")
    ok = await ctx.repos.pairings.revoke(pairing_id)
    if ok:
        await ctx.audit.log("pairing.revoked", pairing_id=pairing_id)
    return {"ok": ok}
