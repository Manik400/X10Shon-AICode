"""Entry point usable from any working directory: ``python agent/run.py`` (or ``pythonw`` to run
without a console window, which is how the logon task starts it)."""

from __future__ import annotations

import os
import socket
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# pythonw has no console: give stdout/stderr somewhere to go so nothing that prints can crash.
if sys.stdout is None or sys.stderr is None:
    log_dir = Path(os.environ.get("LFA_DATA_DIR") or (HERE.parent / "data")) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stream = open(log_dir / "console.log", "a", encoding="utf-8", buffering=1)  # noqa: SIM115
    sys.stdout = sys.stdout or stream
    sys.stderr = sys.stderr or stream


def _already_running(host: str = "127.0.0.1", port: int = 8765) -> bool:
    """The watchdog trigger starts this every few minutes; exit quietly if the agent is up."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


if __name__ == "__main__":
    if _already_running():
        sys.exit(0)
    from app.main import main

    main()
