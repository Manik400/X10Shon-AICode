from __future__ import annotations

from typing import Any

from ...core.util import new_id, now_iso, sha256_hex
from .base import Repository, row_to_dict


class PairingRepository(Repository):
    """Stores only the SHA-256 of extension tokens."""

    async def create(self, token: str, label: str, extension_id: str | None) -> dict[str, Any]:
        pid = new_id()
        async with self.db.transaction():
            await self.db.execute(
                "INSERT INTO pairings(id, label, token_hash, extension_id, created_at) VALUES (?, ?, ?, ?, ?)",
                (pid, label, sha256_hex(token), extension_id, now_iso()),
            )
        return {"id": pid, "label": label, "extension_id": extension_id}

    async def find_by_token(self, token: str) -> dict[str, Any] | None:
        row = await self.db.fetch_one(
            "SELECT * FROM pairings WHERE token_hash = ? AND revoked_at IS NULL", (sha256_hex(token),)
        )
        return row_to_dict(row)

    async def touch(self, pairing_id: str) -> None:
        await self.db.execute("UPDATE pairings SET last_seen_at = ? WHERE id = ?", (now_iso(), pairing_id))

    async def list(self) -> list[dict[str, Any]]:
        rows = await self.db.fetch_all(
            "SELECT id, label, extension_id, created_at, last_seen_at, revoked_at FROM pairings ORDER BY created_at DESC"
        )
        return [dict(r) for r in rows]

    async def revoke(self, pairing_id: str) -> bool:
        async with self.db.transaction():
            n = await self.db.execute(
                "UPDATE pairings SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL", (now_iso(), pairing_id)
            )
        return n > 0

    async def active_extension_ids(self) -> list[str]:
        rows = await self.db.fetch_all(
            "SELECT DISTINCT extension_id FROM pairings WHERE revoked_at IS NULL AND extension_id IS NOT NULL"
        )
        return [r["extension_id"] for r in rows]
