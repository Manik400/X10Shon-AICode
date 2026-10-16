"""Short-lived pairing codes and extension token issuance.

Flow: the dashboard (same origin, already trusted) asks for a pairing code → the user types the
code into the extension options page → the extension POSTs ``/api/pairing/claim`` with the code
and its ``chrome-extension://`` origin → the agent returns a bearer token (shown once, stored only
hashed) and remembers the extension id for origin validation.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from ..core.util import pairing_code, random_token
from ..persistence.repositories import PairingRepository


@dataclass(slots=True)
class PendingCode:
    code: str
    expires_at: float
    label: str


class PairingService:
    def __init__(self, repo: PairingRepository, ttl_seconds: int = 300):
        self._repo = repo
        self._ttl = ttl_seconds
        self._pending: dict[str, PendingCode] = {}
        self._claim_failures: list[float] = []

    def issue_code(self, label: str = "Chrome extension") -> dict[str, object]:
        self._expire()
        # Only one outstanding code at a time keeps the attack surface tiny.
        self._pending.clear()
        code = pairing_code()
        self._pending[code] = PendingCode(code=code, expires_at=time.time() + self._ttl, label=label)
        return {"code": code, "expires_in": self._ttl}

    def has_pending(self) -> bool:
        self._expire()
        return bool(self._pending)

    async def claim(self, code: str, extension_id: str | None) -> str | None:
        """Return a fresh token on success, None otherwise. Rate-limited to 5 failures/minute."""
        self._expire()
        now = time.time()
        self._claim_failures = [t for t in self._claim_failures if now - t < 60]
        if len(self._claim_failures) >= 5:
            return None
        pending = self._pending.pop(code.strip().upper(), None)
        if pending is None:
            self._claim_failures.append(now)
            return None
        token = random_token(32)
        await self._repo.create(token, pending.label, extension_id)
        return token

    async def auto_claim(self, extension_id: str) -> str:
        """Issue a token without a code; the API only calls this for the path-verified
        extension id of this repository's own build."""
        token = random_token(32)
        await self._repo.create(token, "Chrome extension (auto-paired from extension/dist)", extension_id)
        return token

    def _expire(self) -> None:
        now = time.time()
        for k in [k for k, v in self._pending.items() if v.expires_at < now]:
            self._pending.pop(k, None)
