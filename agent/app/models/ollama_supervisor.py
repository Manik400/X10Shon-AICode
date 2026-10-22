"""Keeps a private Ollama server running for the agent.

The agent talks to its own ``ollama serve`` on loopback (default 127.0.0.1:11435) instead of the
Ollama desktop app's server, whose model folder and network exposure are user settings that other
tools change. The supervisor starts it when it is not answering (after boot, sleep, a crash or the
user quitting it) with the GPU settings and model folder the agent needs. It never touches the
desktop app or its settings.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

import httpx

log = logging.getLogger("lfa.ollama")


def _default_exe() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe"


def _default_models_dir() -> Path:
    return Path.home() / ".ollama" / "models"


class OllamaSupervisor:
    def __init__(self, base_url: str, *, exe: str | None = None, models_dir: str | None = None, gpu: bool = True) -> None:
        self._base_url = base_url.rstrip("/")
        self._exe = Path(exe) if exe else _default_exe()
        self._models_dir = Path(models_dir) if models_dir else _default_models_dir()
        self._gpu = gpu
        self._proc: subprocess.Popen[bytes] | None = None
        self._lock = asyncio.Lock()

    @property
    def manages_local_server(self) -> bool:
        host = urlsplit(self._base_url).hostname or ""
        return host in ("127.0.0.1", "localhost") and self._exe.exists()

    async def _alive(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=3.0) as c:
                r = await c.get(self._base_url + "/api/version")
                return r.status_code == 200
        except Exception:
            return False

    def _env(self) -> dict[str, str]:
        port = urlsplit(self._base_url).port or 11434
        env = dict(os.environ)
        env.update(
            {
                "OLLAMA_HOST": f"127.0.0.1:{port}",  # loopback only, never the network
                "OLLAMA_MODELS": str(self._models_dir),
                "OLLAMA_KEEP_ALIVE": "-1",
                "OLLAMA_NUM_PARALLEL": "1",
                "OLLAMA_MAX_LOADED_MODELS": "1",
            }
        )
        if self._gpu:
            env.update({"OLLAMA_VULKAN": "1", "OLLAMA_IGPU_ENABLE": "1"})
        return env

    async def ensure_running(self) -> bool:
        """True when the server answers (starting it first if needed)."""
        if await self._alive():
            return True
        if not self.manages_local_server:
            return False
        async with self._lock:
            if await self._alive():
                return True
            if self._proc is not None and self._proc.poll() is None:
                self._proc.terminate()
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            log_path = Path(os.environ.get("LFA_DATA_DIR") or self._models_dir.parent) / "logs" / "ollama-agent.log"
            try:
                log_path.parent.mkdir(parents=True, exist_ok=True)
                out = open(log_path, "ab")  # noqa: SIM115 - handed to the child process
                self._proc = subprocess.Popen([str(self._exe), "serve"], env=self._env(), stdout=out, stderr=out, stdin=subprocess.DEVNULL, creationflags=flags)
            except Exception as exc:
                log.warning("could not start ollama: %s", exc)
                return False
            log.info("started private ollama server (pid %s) on %s", self._proc.pid, self._base_url)
            for _ in range(40):
                await asyncio.sleep(0.5)
                if await self._alive():
                    return True
            log.warning("ollama did not answer within 20 s")
            return False
