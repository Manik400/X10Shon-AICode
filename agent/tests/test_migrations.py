from __future__ import annotations

from pathlib import Path

import pytest

from app.persistence.db import Database, list_migrations


@pytest.mark.asyncio
async def test_migrations_apply_and_are_idempotent(tmp_path: Path) -> None:
    db = Database(tmp_path / "t.db")
    await db.connect()
    applied = await db.migrate()
    assert applied == [v for v, _ in list_migrations()]
    assert await db.schema_version() == max(applied)
    again = await db.migrate()
    assert again == []
    tables = {r["name"] for r in await db.fetch_all("SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ("jobs", "applications", "application_transitions", "tasks", "execution_attempts", "approvals", "audit_events", "model_metrics", "pairings", "candidate_profile", "approved_answers", "resumes", "scheduler_config"):
        assert t in tables
    await db.close()


@pytest.mark.asyncio
async def test_transaction_rolls_back_on_error(tmp_path: Path) -> None:
    db = Database(tmp_path / "t.db")
    await db.connect()
    await db.migrate()
    with pytest.raises(RuntimeError):
        async with db.transaction():
            await db.execute("INSERT INTO settings(key, value_json, updated_at) VALUES ('a', '1', 'now')")
            raise RuntimeError("boom")
    assert await db.fetch_value("SELECT COUNT(*) FROM settings") == 0
    await db.close()


@pytest.mark.asyncio
async def test_database_file_persists_between_connections(tmp_path: Path) -> None:
    path = tmp_path / "p.db"
    db = Database(path)
    await db.connect()
    await db.migrate()
    await db.execute("INSERT INTO settings(key, value_json, updated_at) VALUES ('k', '\"v\"', 'now')")
    await db.close()
    db2 = Database(path)
    await db2.connect()
    assert await db2.fetch_value("SELECT value_json FROM settings WHERE key='k'") == '"v"'
    await db2.close()
