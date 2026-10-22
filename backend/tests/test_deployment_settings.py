"""A deployed environment refuses development defaults (review finding F13).

`production` used to check HTTPS, the identity provider, demo credentials, fault controls and
the session secret, but would start with the local artifact store, quotas or the shared rate
limit switched off, no trusted proxy (every client counted under the load balancer), and
purchasing data from the simulator. `staging` (synthetic data, simulator allowed) did not exist,
so the staging plan had to claim to be production.
"""

from __future__ import annotations

from typing import Any

import pytest

from opsbridge.config import ConfigurationError, Settings

DEPLOYED: dict[str, Any] = {
    "public_url": "https://opsbridge.example.test",
    "oidc_issuer": "https://id.example.test/realms/opsbridge",
    "session_secret": "s" * 40,
    "artifact_s3_bucket": "opsbridge-artifacts",
    "artifact_s3_sse": "aws:kms",
    "trusted_proxies": ["10.0.0.0/16"],
    "demo_user_password": None,
    "simulator_fault_controls": False,
}


def settings(**changes: Any) -> Settings:
    # Independent of the developer's .env (which enables fault controls, for instance).
    return Settings(_env_file=None, **{**DEPLOYED, **changes})  # type: ignore[call-arg]


def test_staging_accepts_a_deployed_configuration_with_synthetic_sources() -> None:
    s = settings(env="staging")
    assert s.deployed and s.secure_cookies


@pytest.mark.parametrize("env", ["staging", "production"])
@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"artifact_s3_bucket": None}, "artifact store"),
        ({"artifact_s3_sse": None}, "server-side encryption"),
        ({"quotas_enabled": False}, "quotas"),
        ({"rate_limit_shared": False}, "rate limit"),
        ({"trusted_proxies": []}, "trusted_proxies"),
        ({"simulator_fault_controls": True}, "fault"),
        ({"public_url": "http://opsbridge.example.test"}, "https"),
    ],
)
def test_a_deployed_environment_refuses_development_defaults(
    env: str, change: dict[str, Any], message: str
) -> None:
    with pytest.raises(ConfigurationError, match=message):
        settings(env=env, **change)


def test_production_refuses_synthetic_sources_and_demo_users() -> None:
    with pytest.raises(ConfigurationError, match="simulator"):
        settings(env="production")
    with pytest.raises(ConfigurationError, match="demo user"):
        settings(env="production", demo_user_password="demo")
