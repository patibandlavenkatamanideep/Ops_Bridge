"""Runtime configuration from environment variables (prefix OPSBRIDGE_)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import quote, urlparse

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]

DbRole = Literal["app", "worker", "mcp", "migrator"]
ModelMode = Literal["mock", "anthropic", "ollama", "rules_only"]


class ConfigurationError(RuntimeError):
    pass


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="OPSBRIDGE_",
        env_file=str(REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    env: Literal["development", "test", "production"] = "development"

    db_host: str = "localhost"
    db_port: int = 5433
    db_name: str = "opsbridge"
    db_app_password: SecretStr = SecretStr("")
    db_worker_password: SecretStr = SecretStr("")
    db_mcp_password: SecretStr = SecretStr("")
    db_migrator_password: SecretStr = SecretStr("")
    db_pool_size: int = 5
    db_statement_timeout_ms: int = 15_000

    oidc_issuer: str = "http://localhost:8081/realms/opsbridge"
    # Optional internal URL for discovery when the issuer host is not reachable from the
    # backend (e.g. inside Compose). Token validation always uses `oidc_issuer`.
    oidc_discovery_url: str | None = None
    oidc_client_id: str = "opsbridge-web"
    oidc_client_secret: SecretStr = SecretStr("")
    worker_client_id: str = "opsbridge-worker"
    worker_client_secret: SecretStr = SecretStr("")
    demo_user_password: SecretStr | None = None

    public_url: str = "http://localhost:5173"
    session_secret: SecretStr = SecretStr("")
    session_absolute_hours: int = 8
    session_idle_minutes: int = 30
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])
    # Per verified user (after the session is checked), and per client address for every
    # API request with or without a cookie: an unverified cookie never selects a budget.
    rate_limit_per_minute: int = 240
    rate_limit_per_address_per_minute: int = 1200
    rate_limit_window_seconds: int = 60
    # CIDRs of reverse proxies whose X-Forwarded-For is believed (empty: the direct peer).
    trusted_proxies: list[str] = Field(default_factory=list)
    # Count in PostgreSQL so every API instance shares one limit; in-process fallback when the
    # database is unavailable. Off only for single-process tooling.
    rate_limit_shared: bool = True
    # Membership invitations: how long a link stays valid, and how many may be open per tenant.
    invitation_ttl_hours: int = 168
    max_open_invitations: int = 50
    max_upload_bytes: int = 5 * 1024 * 1024
    max_request_bytes: int = 6 * 1024 * 1024

    artifact_dir: Path = REPO_ROOT / "var" / "artifacts"
    packs_dir: Path = REPO_ROOT / "packs"
    evals_dir: Path = REPO_ROOT / "evals"
    # S3-compatible artifact storage (staging). Unset bucket = local filesystem store.
    artifact_s3_bucket: str | None = None
    artifact_s3_prefix: str = ""
    artifact_s3_endpoint_url: str | None = None  # e.g. a local S3-compatible server
    artifact_s3_region: str | None = None
    artifact_s3_sse: str | None = None  # "AES256" or "aws:kms"
    artifact_s3_kms_key_id: str | None = None
    # Tier quotas (investigations per hour, model calls per day). Off only in the isolated
    # benchmark harness, which measures decisions, not throughput (as it lifts rate limits).
    quotas_enabled: bool = True
    # Optional local OCR for PDFs without a text layer (needs tesseract). OCR text and the
    # field values read from it are shown for review and never used by the matching rules.
    ocr_enabled: bool = False
    ocr_max_pages: int = 3
    ocr_page_timeout_seconds: float = 15.0
    ocr_language: str = "eng"
    metrics_token: SecretStr | None = None  # bearer token for /metrics when set
    worker_http_host: str = "127.0.0.1"
    worker_http_port: int = 9101  # worker /healthz, /readyz, /metrics (0 disables)
    retention_schedule_seconds: int = (
        3600  # how often the worker checks for due sweeps (0 disables)
    )
    delegation_key_path: Path = REPO_ROOT / "var" / "keys" / "delegation_ed25519.pem"
    # PEM from a secret store (staging/production); takes precedence and is never written.
    delegation_private_key: SecretStr | None = None

    simulator_url: str = "http://localhost:8090"
    simulator_fault_controls: bool = False
    sim_master_secret: SecretStr = SecretStr("")

    records_mcp_url: str = "http://localhost:8101/mcp"
    casework_mcp_url: str = "http://localhost:8102/mcp"
    tool_transport: Literal["inprocess", "mcp"] = "inprocess"

    model_mode: ModelMode = "mock"
    live_model_opt_in: bool = False
    live_spend_ceiling_usd: str = "0"
    anthropic_model: str | None = None
    # Operator-configured prices (USD per million tokens) used to enforce the spend ceiling.
    anthropic_price_input_per_mtok_usd: str | None = None
    anthropic_price_output_per_mtok_usd: str | None = None
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str | None = None

    github_sandbox_repo: str | None = None
    github_token: SecretStr | None = None
    github_live_writes_authorized: bool = False

    worker_lease_seconds: int = 30
    worker_poll_seconds: float = 1.0
    connector_timeout_seconds: float = 5.0

    @model_validator(mode="after")
    def _absolute_paths(self) -> Settings:
        # Relative paths in .env are relative to the repository root, not the process cwd.
        for name in ("artifact_dir", "packs_dir", "evals_dir", "delegation_key_path"):
            value: Path = getattr(self, name)
            if not value.is_absolute():
                object.__setattr__(self, name, (REPO_ROOT / value).resolve())
        return self

    @model_validator(mode="after")
    def _production_guards(self) -> Settings:
        if self.env != "production":
            return self
        problems: list[str] = []
        if urlparse(self.public_url).scheme != "https":
            problems.append("public_url must use https in production")
        if urlparse(self.oidc_issuer).scheme != "https":
            problems.append("oidc_issuer must use https in production")
        if "localhost" in self.oidc_issuer or "127.0.0.1" in self.oidc_issuer:
            problems.append("the local development identity provider is not allowed in production")
        if self.demo_user_password is not None and self.demo_user_password.get_secret_value():
            problems.append("demo user credentials must not be configured in production")
        if self.simulator_fault_controls:
            problems.append("simulator fault-injection controls must be disabled in production")
        if len(self.session_secret.get_secret_value()) < 32:
            problems.append("session_secret must be at least 32 characters in production")
        if problems:
            raise ConfigurationError("; ".join(problems))
        return self

    @property
    def secure_cookies(self) -> bool:
        return self.env == "production" or urlparse(self.public_url).scheme == "https"

    def database_url(self, role: DbRole, database: str | None = None) -> str:
        user = f"opsbridge_{role}"
        password = {
            "app": self.db_app_password,
            "worker": self.db_worker_password,
            "mcp": self.db_mcp_password,
            "migrator": self.db_migrator_password,
        }[role].get_secret_value()
        db = database or self.db_name
        return f"postgresql+psycopg://{user}:{quote(password, safe='')}@{self.db_host}:{self.db_port}/{db}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
