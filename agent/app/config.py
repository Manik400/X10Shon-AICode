"""Typed, validated settings for the local agent service.

All values can be overridden with environment variables prefixed ``LFA_`` or an ``.env`` file in
the working directory. Nothing here is a secret; secrets never live in settings.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_data_dir() -> Path:
    base = os.environ.get("LFA_DATA_DIR")
    if base:
        return Path(base)
    home = Path(os.environ.get("LOCALAPPDATA") or Path.home())
    return home / ".lfa"


class ModelRoleConfig(BaseSettings):
    """Model selection per role. ``provider`` is one of ollama|openai_compatible|heuristic."""

    model_config = SettingsConfigDict(extra="ignore")

    provider: Literal["ollama", "openai_compatible", "heuristic"] = "heuristic"
    model: str = ""
    temperature: float = 0.0
    max_output_tokens: int = 1024


_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"  # agent/.env, independent of the working directory


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LFA_", env_file=str(_ENV_FILE), env_nested_delimiter="__", extra="ignore")

    # --- network -----------------------------------------------------------------------------
    host: str = "127.0.0.1"
    port: int = 8765
    dashboard_dev_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173", "http://127.0.0.1:5173"])

    # --- storage -----------------------------------------------------------------------------
    data_dir: Path = Field(default_factory=_default_data_dir)
    db_filename: str = "lfa.db"
    dashboard_dist: Path | None = None  # auto-detected relative to repo when None

    # --- security ----------------------------------------------------------------------------
    pairing_code_ttl_seconds: int = 86_400  # codes are single-use; tokens never expire
    max_request_bytes: int = 2 * 1024 * 1024
    allowed_domains: list[str] = Field(default_factory=list)  # empty => only domains added via settings/jobs
    trusted_hosts: list[str] = Field(default_factory=list)  # extra Host header values accepted (tests use "testserver")
    require_submit_approval: Literal["always", "never", "per_site"] = "always"

    # --- model runtime -----------------------------------------------------------------------
    ollama_base_url: str = "http://127.0.0.1:11434"
    # Start (and restart) a private loopback `ollama serve` for ollama_base_url when it is down.
    ollama_autostart: bool = False
    ollama_exe: str | None = None  # default: %LOCALAPPDATA%\Programs\Ollama\ollama.exe
    ollama_models_dir: str | None = None  # default: ~/.ollama/models
    openai_compatible_base_url: str = "http://127.0.0.1:8080/v1"
    model_request_timeout_seconds: float = 150.0  # CPU inference of a 3B model on ~2k tokens takes 30-90 s
    model_max_concurrency: int = 1
    model_max_response_bytes: int = 64 * 1024
    model_context_chars: int = 8_000  # bound on page context chars sent to a model
    model_cache_ttl_seconds: int = 600
    model_roles: dict[str, ModelRoleConfig] = Field(
        default_factory=lambda: {
            "intent": ModelRoleConfig(),
            "planner": ModelRoleConfig(),
            "extractor": ModelRoleConfig(),
            "matcher": ModelRoleConfig(),
            "field_mapper": ModelRoleConfig(),
            "classifier": ModelRoleConfig(),
        }
    )

    # --- agent loop bounds -------------------------------------------------------------------
    max_actions_per_task: int = 60
    action_timeout_seconds: float = 20.0
    task_deadline_seconds: float = 900.0
    max_recovery_attempts: int = 3
    snapshot_max_nodes: int = 600

    # --- browser -----------------------------------------------------------------------------
    playwright_headless: bool = True
    max_concurrent_pages: int = 2
    navigation_timeout_ms: int = 30_000

    # --- scheduler / resources ---------------------------------------------------------------
    max_concurrent_tasks: int = 1
    per_domain_concurrency: int = 1
    per_domain_delay_seconds: float = 5.0
    daily_application_limit: int = 20
    queue_size: int = 200
    max_ram_mb: int = 8192
    cpu_threads: int = 0  # 0 => runtime default
    gpu_enabled: bool = False
    pause_on_resource_pressure: bool = True
    memory_pressure_percent: int = 92  # system RAM % above which tasks wait (if under min_available_mb)
    memory_min_available_mb: int = 1536

    # --- logging -----------------------------------------------------------------------------
    log_level: str = "INFO"
    log_max_bytes: int = 5 * 1024 * 1024
    log_backup_count: int = 3

    @field_validator("host")
    @classmethod
    def _no_wildcard_bind(cls, v: str) -> str:
        if v in {"0.0.0.0", "::", ""}:
            raise ValueError("Binding to all interfaces is not allowed by default; set LFA_HOST explicitly to a specific address")
        return v

    @property
    def db_path(self) -> Path:
        return self.data_dir / self.db_filename

    @property
    def origin(self) -> str:
        return f"http://{self.host}:{self.port}"


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def set_settings(settings: Settings) -> None:
    """Used by tests to inject an isolated configuration."""
    global _settings
    _settings = settings
