"""FastAPI application factory and entry point."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .api import applications, control, diagnostics, extension_ws, health, jobs, pairing, profile, settings as settings_api, tasks
from .config import Settings, get_settings
from .observability.logging import configure_logging
from .security.auth import CLIENT_HEADER, extension_origin, trusted_dashboard_origins
from .state import AppContext, build_context

log = logging.getLogger("lfa")

REPO_ROOT = Path(__file__).resolve().parents[2]


def _dashboard_dist(settings: Settings) -> Path | None:
    cand = settings.dashboard_dist or (REPO_ROOT / "dashboard" / "dist")
    return cand if (cand / "index.html").exists() else None


def create_app(settings: Settings | None = None, *, db_path: Path | str | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.data_dir / "logs", settings.log_max_bytes, settings.log_backup_count)

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
        ctx = await build_context(settings, db_path=db_path)
        app.state.ctx = ctx
        app.state.settings = settings
        app.state.repos = ctx.repos
        # Dynamic CORS allow-list: dashboard origins + every paired extension origin.
        ext_ids = await ctx.repos.pairings.active_extension_ids()
        app.state.allowed_origins = trusted_dashboard_origins(settings) | {extension_origin(e) for e in ext_ids}
        ctx.scheduler.start()
        try:
            await ctx.tasks.rearm_watchers()
        except Exception:  # noqa: BLE001 - never block startup on this
            log.exception("could not re-arm verification watchers")
        await ctx.audit.log("service.started", host=settings.host, port=settings.port)
        log.info("LFA agent listening on %s", settings.origin)
        try:
            yield
        finally:
            await ctx.scheduler.stop()
            await ctx.tasks.shutdown()
            await ctx.playwright_driver.shutdown()
            await ctx.gateway.close()
            await ctx.audit.log("service.stopped")
            await ctx.db.close()

    app = FastAPI(title="Local-First Agent", version="0.1.0", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    # CORS: only our own origins; the custom header is required so simple requests never pass.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[],
        allow_origin_regex=r"^(http://(127\.0\.0\.1|localhost)(:\d+)?|chrome-extension://[a-p]{32})$",
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", CLIENT_HEADER],
        max_age=600,
    )

    @app.middleware("http")
    async def limits_and_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
        cl = request.headers.get("content-length")
        if cl and int(cl) > settings.max_request_bytes:
            return JSONResponse({"detail": "request too large"}, status_code=413)
        origin = request.headers.get("origin")
        if origin and origin.startswith("chrome-extension://"):
            allowed = getattr(request.app.state, "allowed_origins", set())
            ctx: AppContext | None = getattr(request.app.state, "ctx", None)
            if origin not in allowed and ctx is not None:
                # A freshly paired extension: refresh the allow-list once.
                ids = await ctx.repos.pairings.active_extension_ids()
                allowed = trusted_dashboard_origins(settings) | {extension_origin(e) for e in ids}
                request.app.state.allowed_origins = allowed
            if origin not in allowed and not request.url.path.startswith("/api/pairing/claim"):
                return JSONResponse({"detail": "origin not paired"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(ValueError)
    async def value_error(_: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)[:300]}, status_code=400)

    for r in (health.router, pairing.router, extension_ws.router, jobs.router, applications.router, profile.router, tasks.router, control.router, settings_api.router, diagnostics.router):
        app.include_router(r)

    # Local test pages (forms, auth walls, job postings) for end-to-end checks through the real
    # extension: http://127.0.0.1:<port>/fixtures/forms/simple_application.html etc.
    fixtures = REPO_ROOT / "tests" / "fixtures"
    if fixtures.is_dir():
        app.mount("/fixtures", StaticFiles(directory=fixtures), name="fixtures")

    dist = _dashboard_dist(settings)
    if dist is not None:
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        @app.api_route("/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
        async def spa(path: str) -> Any:
            if path.startswith(("api/", "ws/")):
                raise HTTPException(404)
            candidate = (dist / path).resolve()
            if path and candidate.is_file() and str(candidate).startswith(str(dist.resolve())):
                return FileResponse(candidate)
            resp = FileResponse(dist / "index.html")
            resp.headers["Content-Security-Policy"] = "default-src 'self'; connect-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'"
            return resp

    else:

        @app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
        async def no_dashboard() -> dict[str, str]:
            return {"message": "Dashboard not built. Run `npm run build` in dashboard/ or `npm run dev` for development.", "api": "/api/health"}

    return app


def main() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, log_level=settings.log_level.lower(), ws_max_size=2 * 1024 * 1024)


if __name__ == "__main__":
    main()
