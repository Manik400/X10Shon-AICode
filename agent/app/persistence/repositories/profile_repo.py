from __future__ import annotations

from typing import Any

from ...core.util import dumps, loads, new_id, now_iso
from .base import Repository, row_to_dict


class ProfileRepository(Repository):
    async def get(self) -> dict[str, Any] | None:
        row = await self.db.fetch_one("SELECT data_json, updated_at FROM candidate_profile WHERE id = 1")
        if row is None:
            return None
        data = loads(row["data_json"], {})
        data["updated_at"] = row["updated_at"]
        return data

    async def save(self, data: dict[str, Any]) -> None:
        data = dict(data)
        data.pop("updated_at", None)
        async with self.db.transaction():
            await self.db.execute(
                "INSERT INTO candidate_profile(id, data_json, updated_at) VALUES (1, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET data_json = excluded.data_json, updated_at = excluded.updated_at",
                (dumps(data), now_iso()),
            )

    # ------------------------------------------------------------------ approved answers
    async def list_answers(self, category: str | None = None) -> list[dict[str, Any]]:
        if category:
            rows = await self.db.fetch_all("SELECT * FROM approved_answers WHERE category = ? ORDER BY updated_at DESC", (category,))
        else:
            rows = await self.db.fetch_all("SELECT * FROM approved_answers ORDER BY category, updated_at DESC")
        return [dict(r) for r in rows]

    async def upsert_answer(self, answer: dict[str, Any]) -> dict[str, Any]:
        aid = answer.get("id") or new_id()
        ts = now_iso()
        async with self.db.transaction():
            await self.db.execute(
                "INSERT INTO approved_answers(id, category, question, answer, provenance, confidence, legally_significant, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET category=excluded.category, question=excluded.question, answer=excluded.answer, "
                "provenance=excluded.provenance, confidence=excluded.confidence, legally_significant=excluded.legally_significant, updated_at=excluded.updated_at",
                (
                    aid,
                    answer["category"],
                    answer["question"],
                    answer["answer"],
                    answer.get("provenance", "approved_answer"),
                    float(answer.get("confidence", 1.0)),
                    int(bool(answer.get("legally_significant", False))),
                    ts,
                    ts,
                ),
            )
        row = await self.db.fetch_one("SELECT * FROM approved_answers WHERE id = ?", (aid,))
        return dict(row) if row else {"id": aid}

    async def delete_answer(self, answer_id: str) -> bool:
        async with self.db.transaction():
            return (await self.db.execute("DELETE FROM approved_answers WHERE id = ?", (answer_id,))) > 0

    # ------------------------------------------------------------------ resumes
    async def list_resumes(self) -> list[dict[str, Any]]:
        rows = await self.db.fetch_all("SELECT * FROM resumes ORDER BY created_at DESC")
        return [row_to_dict(r, ("targeting_json",)) or {} for r in rows]

    async def get_resume(self, resume_id: str) -> dict[str, Any] | None:
        return row_to_dict(await self.db.fetch_one("SELECT * FROM resumes WHERE id = ?", (resume_id,)), ("targeting_json",))

    async def add_resume(self, resume: dict[str, Any]) -> dict[str, Any]:
        rid = resume.get("id") or new_id()
        ts = now_iso()
        async with self.db.transaction():
            await self.db.execute(
                "INSERT INTO resumes(id, label, file_path, file_name, size_bytes, sha256, mime_type, targeting_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    rid,
                    resume["label"],
                    resume["file_path"],
                    resume["file_name"],
                    int(resume["size_bytes"]),
                    resume["sha256"],
                    resume["mime_type"],
                    dumps(resume.get("targeting", {})),
                    ts,
                    ts,
                ),
            )
        return await self.get_resume(rid) or {"id": rid}

    async def update_resume(self, resume_id: str, *, label: str | None = None, targeting: dict[str, Any] | None = None) -> dict[str, Any] | None:
        async with self.db.transaction():
            if label is not None:
                await self.db.execute("UPDATE resumes SET label = ?, updated_at = ? WHERE id = ?", (label, now_iso(), resume_id))
            if targeting is not None:
                await self.db.execute("UPDATE resumes SET targeting_json = ?, updated_at = ? WHERE id = ?", (dumps(targeting), now_iso(), resume_id))
        return await self.get_resume(resume_id)

    async def delete_resume(self, resume_id: str) -> bool:
        async with self.db.transaction():
            return (await self.db.execute("DELETE FROM resumes WHERE id = ?", (resume_id,))) > 0
