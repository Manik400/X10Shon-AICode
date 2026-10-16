from __future__ import annotations

from typing import Any

import aiosqlite

from ...core.util import loads
from ..db import Database


def row_to_dict(row: aiosqlite.Row | None, json_fields: tuple[str, ...] = ()) -> dict[str, Any] | None:
    if row is None:
        return None
    d = dict(row)
    for f in json_fields:
        if f in d:
            d[f] = loads(d[f])
    return d


class Repository:
    def __init__(self, db: Database):
        self.db = db


class Page:
    """Simple pagination helper used by list endpoints."""

    def __init__(self, limit: int = 50, offset: int = 0):
        self.limit = max(1, min(int(limit), 500))
        self.offset = max(0, int(offset))
