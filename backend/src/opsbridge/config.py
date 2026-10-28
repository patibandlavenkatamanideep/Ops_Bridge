"""Runtime configuration from environment variables (prefix OPSBRIDGE_)."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import quote, urlparse

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]

DbRole = Literal["app", "worker", "mcp", "migrator"]
ModelMode = Literal["mock", "anthropic", "ollama", "rules_only"]


class ConfigurationError(RuntimeError):
    pass


class ConnectionCredential(BaseModel):
    """A secret-store reference the deployment registered for one tenant and destination.
    Tenant administrators choose among these; they never name arbitrary secrets."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    tenant: str  # the tenant's slug
    connector: Literal["github_issues"]
    repositories: list[str]  # owner/name, compared case-insensitively


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="OPSBRIDGE_",
        env_file=str(REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # staging: deployed, with synthetic data (the purchasing simulator is allowed);
    # production: deployed, real data. Both refuse development defaults (_deployment_guards).
    env: Literal["development", "test", "staging", "production"] = "development"

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
    # Only the services that SIGN delegations (API, worker) need it.
    delegation_private_key: SecretStr | None = None
    # Public keys the MCP services verify delegations with (PEM; several while keys rotate),
    # given directly or as a file of PEM blocks. A deployed verifier needs one of these; in
    # development the public key may be derived from the local key file.
    delegation_public_keys: list[str] = Field(default_factory=list)
    delegation_public_key_path: Path | None = None

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
    github_token: SecretStr | None = None  # used by GitHub connections without a credential ref
    # Credential references a tenant may choose, each registered to a tenant and destination
    # (JSON in OPSBRIDGE_CONNECTION_CREDENTIALS); the secret is OPSBRIDGE_CONNECTION_SECRET_<REF>.
    connection_credentials: dict[str, ConnectionCredential] = Field(default_factory=dict)
    github_live_writes_authorized: bool = False
    # Repositories a tenant's GitHub task connection may name (the sandbox repo is included).
    github_repository_allowlist: list[str] = Field(default_factory=list)
    github_api_url: str = "https://api.github.com"

    worker_lease_seconds: int = 30
    # A step that runs longer stops renewing its job's lease: another worker may take the
    # run over, and the stuck worker's late writes are refused (fencing).
    investigation_step_limit_seconds: int = 300
    worker_poll_seconds: float = 1.0
    connector_timeout_seconds: float = 5.0
    # An investigation's purchasing data may be acted on (proposals, approvals, dispatch,
    # resolution) for this long after it was read; then the investigation must be re-run.
    source_max_age_hours: float = Field(default=24.0, gt=0)
    # Reading the purchasing source for one investigation stops after this long (every page
    # and record together); what was read is kept and nothing is concluded from it.
    source_collection_seconds: float = Field(default=120.0, gt=0)

    @model_validator(mode="after")
    def _absolute_paths(self) -> Settings:
        # Relative paths in .env are relative to the repository root, not the process cwd.
        for name in (
            "artifact_dir",
            "packs_dir",
            "evals_dir",
            "delegation_key_path",
            "delegation_public_key_path",
        ):
            value: Path | None = getattr(self, name)
            if value is not None and not value.is_absolute():
                object.__setattr__(self, name, (REPO_ROOT / value).resolve())
        return self

    @model_validator(mode="after")
    def _amounts_are_finite(self) -> Settings:
        """The spend ceiling and prices reserve real money: refuse what cannot be compared."""
        for name in (
            "live_spend_ceiling_usd",
            "anthropic_price_input_per_mtok_usd",
            "anthropic_price_output_per_mtok_usd",
        ):
            raw = getattr(self, name)
            if raw is None:
                continue
            try:
                value = Decimal(raw)
            except InvalidOperation as exc:
                raise ValueError(f"{name} is not a decimal amount: {raw!r}") from exc
            if not value.is_finite() or value < 0:
                raise ValueError(f"{name} must be a finite, non-negative amount, got {raw!r}")
        return self

    @model_validator(mode="after")
    def _deployment_guards(self) -> Settings:
        """A deployed environment refuses to start with development defaults. These are
        settings checks only: they do not show that the bucket encrypts, the proxy is the load
        balancer or the secrets are rotated (docs/deployment.md)."""
        if not self.deployed:
            return self
        env = self.env
        problems: list[str] = []
        if urlparse(self.public_url).scheme != "https":
            problems.append(f"public_url must use https in {env}")
        if urlparse(self.oidc_issuer).scheme != "https":
            problems.append(f"oidc_issuer must use https in {env}")
        if "localhost" in self.oidc_issuer or "127.0.0.1" in self.oidc_issuer:
            problems.append(f"the local development identity provider is not allowed in {env}")
        if self.simulator_fault_controls:
            problems.append(f"simulator fault-injection controls must be disabled in {env}")
        if len(self.session_secret.get_secret_value()) < 32:
            problems.append(f"session_secret must be at least 32 characters in {env}")
        if not self.artifact_s3_bucket:
            problems.append(f"the local artifact store is not allowed in {env}; set a bucket")
        elif self.artifact_s3_sse not in ("AES256", "aws:kms"):
            problems.append(f"artifact storage must request server-side encryption in {env}")
        if not self.quotas_enabled:
            problems.append(f"quotas must be enabled in {env}")
        if not self.rate_limit_shared:
            problems.append(f"the rate limit must be shared across instances in {env}")
        if not self.trusted_proxies:
            problems.append(
                f"trusted_proxies must name the load balancer in {env}, or every client is "
                "rate-limited under its address"
            )
        if env == "production":
            if self.demo_user_password is not None and self.demo_user_password.get_secret_value():
                problems.append("demo user credentials must not be configured in production")
            problems.append(
                "purchasing data would come from the simulator: no production purchasing-source "
                "adapter is implemented yet (use env=staging with synthetic data)"
            )
        if problems:
            raise ConfigurationError("; ".join(problems))
        return self

    @property
    def deployed(self) -> bool:
        return self.env in ("staging", "production")

    @property
    def secure_cookies(self) -> bool:
        return self.deployed or urlparse(self.public_url).scheme == "https"

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
