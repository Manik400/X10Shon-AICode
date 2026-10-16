"""Structured JSON logging with rotation and redaction."""

from __future__ import annotations

import json
import logging
import logging.handlers
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..security.redaction import redact_text


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": redact_text(record.getMessage()),
        }
        for key in ("task_id", "application_id", "job_id", "event"):
            val = getattr(record, key, None)
            if val is not None:
                payload[key] = val
        if record.exc_info:
            payload["exc"] = redact_text(self.formatException(record.exc_info))[-2000:]
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str, log_dir: Path | None, max_bytes: int, backup_count: int) -> None:
    root = logging.getLogger()
    root.setLevel(level.upper())
    for h in list(root.handlers):
        root.removeHandler(h)
    console = logging.StreamHandler()
    console.setFormatter(JsonFormatter())
    root.addHandler(console)
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(log_dir / "agent.log", maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8")
        fh.setFormatter(JsonFormatter())
        root.addHandler(fh)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    class _DropProactorReset(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            return "_call_connection_lost" not in record.getMessage()

    logging.getLogger("asyncio").addFilter(_DropProactorReset())
