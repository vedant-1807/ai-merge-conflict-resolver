"""Typed application settings loaded from the environment (12-factor).

Every knob lives here so the API, worker, CLI and eval harness share one source of truth.
Nested groups use the ``__`` delimiter, e.g. ``LLM__MAX_JOB_BUDGET_USD=2.5``.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    DEV = "dev"
    TEST = "test"
    PROD = "prod"


class GitHubSettings(BaseModel):
    app_id: int | None = None
    private_key: SecretStr | None = None
    private_key_path: Path | None = None
    webhook_secret: SecretStr = SecretStr("")
    api_url: str = "https://api.github.com"
    bot_branch_prefix: str = "mergeagent/"
    # Trigger phrase recognised in PR comments.
    command: str = "/resolve"

    def load_private_key(self) -> str:
        if self.private_key is not None:
            return self.private_key.get_secret_value().replace("\\n", "\n")
        if self.private_key_path is not None:
            return self.private_key_path.read_text()
        raise RuntimeError("GitHub App private key is not configured")


class LLMSettings(BaseModel):
    provider: str = "anthropic"  # anthropic | fake
    anthropic_api_key: SecretStr | None = None
    # Tiered models used by the router. Cheapest first.
    model_small: str = "claude-haiku-5-5"
    model_medium: str = "claude-sonnet-5-5"
    model_large: str = "claude-opus-5-5"
    # When set, the router is bypassed and every call uses this model (cost baseline).
    fixed_model: str | None = None
    max_job_budget_usd: float = 2.00
    max_output_tokens: int = 16_000
    request_timeout_s: float = 300.0
    max_retries: int = 3


class EmbeddingSettings(BaseModel):
    provider: str = "hashing"  # voyage | hashing
    voyage_api_key: SecretStr | None = None
    model: str = "voyage-code-3"
    dimensions: int = 1024


class SandboxSettings(BaseModel):
    backend: str = "docker"  # docker | local (local is for tests only: no isolation)
    image: str = "ghcr.io/mergeagent/sandbox-python:latest"
    runtime: str | None = None  # e.g. "runsc" for gVisor
    memory: str = "2g"
    cpus: float = 2.0
    pids_limit: int = 512
    setup_network: str = "mergeagent-sandbox-egress"
    setup_timeout_s: int = 900
    command_timeout_s: int = 900
    user: str = "10001:10001"


class AgentSettings(BaseModel):
    max_attempts: int = 3
    max_tool_steps: int = 24
    max_hunks_per_job: int = 60
    max_file_bytes: int = 400_000


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        env_ignore_empty=True,
        extra="ignore",
    )

    env: Environment = Environment.DEV
    log_level: str = "INFO"
    log_json: bool = True

    database_url: str = "postgresql+asyncpg://mergeagent:mergeagent@localhost:5432/mergeagent"
    redis_url: str = "redis://localhost:6379/0"
    api_key: SecretStr = SecretStr("dev-api-key")
    workspace_root: Path = Path("/tmp/mergeagent/workspaces")  # noqa: S108

    github: GitHubSettings = Field(default_factory=GitHubSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    embeddings: EmbeddingSettings = Field(default_factory=EmbeddingSettings)
    sandbox: SandboxSettings = Field(default_factory=SandboxSettings)
    agent: AgentSettings = Field(default_factory=AgentSettings)


@lru_cache
def get_settings() -> Settings:
    return Settings()
