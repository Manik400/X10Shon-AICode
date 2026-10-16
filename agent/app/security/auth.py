"""Request authentication and origin validation for the local API.

Two trust classes exist:

* **dashboard** – same-origin pages served by this process (or the Vite dev origin in
  development). Identified by the ``Origin``/``Referer`` host and the mandatory ``X-LFA-Client``
  header. Because browsers only send that custom header after a successful CORS preflight, and
  our CORS allow-list contains only our own origins, arbitrary websites cannot reach the API.
* **extension** – requests carrying a bearer token issued by pairing. The ``Origin`` must be the
  paired ``chrome-extension://<id>`` origin when present.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, status

from ..config import Settings

CLIENT_HEADER = "x-lfa-client"


@dataclass(slots=True)
class Principal:
    kind: Literal["dashboard", "extension"]
    pairing_id: str | None = None
    extension_id: str | None = None


def _origin_of(request: Request) -> str | None:
    origin = request.headers.get("origin")
    if origin:
        return origin.rstrip("/")
    referer = request.headers.get("referer")
    if referer:
        parts = urlsplit(referer)
        if parts.scheme and parts.netloc:
            return f"{parts.scheme}://{parts.netloc}"
    return None


def trusted_dashboard_origins(settings: Settings) -> set[str]:
    base = {settings.origin, f"http://localhost:{settings.port}"}
    base.update(o.rstrip("/") for o in settings.dashboard_dev_origins)
    return base


def extension_origin(ext_id: str) -> str:
    return f"chrome-extension://{ext_id}"


async def authenticate(request: Request) -> Principal:
    app = request.app
    settings: Settings = app.state.settings
    origin = _origin_of(request)

    # Extension path: bearer token.
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
        pairing = await app.state.repos.pairings.find_by_token(token)
        if pairing is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or revoked extension token")
        if origin and origin.startswith("chrome-extension://"):
            ext_id = origin.removeprefix("chrome-extension://")
            if pairing.get("extension_id") and pairing["extension_id"] != ext_id:
                raise HTTPException(status.HTTP_403_FORBIDDEN, "extension origin does not match the pairing")
        elif origin and origin not in trusted_dashboard_origins(settings):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "untrusted origin")
        await app.state.repos.pairings.touch(pairing["id"])
        return Principal(kind="extension", pairing_id=pairing["id"], extension_id=pairing.get("extension_id"))

    # Dashboard path: same-origin + custom header.
    if request.headers.get(CLIENT_HEADER) is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"missing {CLIENT_HEADER} header")
    if origin is not None and origin not in trusted_dashboard_origins(settings):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "untrusted origin")
    host = request.headers.get("host", "")
    if host.split(":")[0] not in {settings.host, "localhost", "127.0.0.1", *settings.trusted_hosts}:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "unexpected host header")
    return Principal(kind="dashboard")


async def require_extension(request: Request) -> Principal:
    p = await authenticate(request)
    if p.kind != "extension":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "extension token required")
    return p
