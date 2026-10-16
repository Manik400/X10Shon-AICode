from __future__ import annotations

from typing import Any

from ...core.util import dumps, loads, now_iso
from .base import Repository


class SettingsRepository(Repository):
    async def get(self, key: str, default: Any = None) -> Any:
        row = await self.db.fetch_one("SELECT value_json FROM settings WHERE key = ?", (key,))
        return loads(row["value_json"], default) if row else default

    async def set(self, key: str, value: Any) -> None:
        async with self.db.transaction():
            await self.db.execute(
                "INSERT INTO settings(key, value_json, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, updated_at = excluded.updated_at",
                (key, dumps(value), now_iso()),
            )

    async def all(self) -> dict[str, Any]:
        rows = await self.db.fetch_all("SELECT key, value_json FROM settings")
        return {r["key"]: loads(r["value_json"]) for r in rows}

    async def get_counter(self, day: str, counter: str) -> int:
        v = await self.db.fetch_value("SELECT value FROM daily_counters WHERE day = ? AND counter = ?", (day, counter))
        return int(v or 0)

    async def increment_counter(self, day: str, counter: str, by: int = 1) -> int:
        async with self.db.transaction():
            await self.db.execute(
                "INSERT INTO daily_counters(day, counter, value) VALUES (?, ?, ?) "
                "ON CONFLICT(day, counter) DO UPDATE SET value = value + excluded.value",
                (day, counter, by),
            )
            return await self.get_counter(day, counter)
