"""Build MCP clients/connectors and servers from settings (no organization branches)."""

from __future__ import annotations

import uuid
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from opsbridge.config import Settings
from opsbridge.connectors.tasks import ConnectorCapabilities, SimTaskConnector

from .auth import DelegationError, DelegationSigner, DelegationVerifier, load_or_create_key
from .client import ClientCredentialsTokens, McpCaseworkConnector, McpToolClient

RECORDS_AUDIENCE = "opsbridge-records-mcp"
CASEWORK_AUDIENCE = "opsbridge-casework-mcp"


def delegation_key(settings: Settings) -> Ed25519PrivateKey:
    """The delegation signing key: from the injected secret if set; otherwise the key file,
    created only outside production (a generated key in production would differ between
    services that are meant to share it)."""
    pem = (
        settings.delegation_private_key.get_secret_value()
        if settings.delegation_private_key
        else ""
    )
    if pem:
        key = serialization.load_pem_private_key(pem.encode(), password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise DelegationError("the configured delegation key is not Ed25519")
        return key
    if settings.deployed and not settings.delegation_key_path.exists():
        raise DelegationError(
            "no delegation key: provide OPSBRIDGE_DELEGATION_PRIVATE_KEY from the secret store"
        )
    return load_or_create_key(settings.delegation_key_path)


def signer(settings: Settings) -> DelegationSigner:
    return DelegationSigner(delegation_key(settings))


def verifier(settings: Settings, audience: str) -> DelegationVerifier:
    return DelegationVerifier(delegation_key(settings).public_key(), audience)


def worker_tokens(settings: Settings) -> ClientCredentialsTokens:
    return ClientCredentialsTokens(
        # Backchannel URL when the public issuer is not reachable from here (containers).
        f"{(settings.oidc_discovery_url or settings.oidc_issuer).rstrip('/')}/protocol/openid-connect/token",
        settings.worker_client_id,
        settings.worker_client_secret.get_secret_value(),
    )


def records_client(settings: Settings) -> McpToolClient:
    return McpToolClient(
        settings.records_mcp_url, worker_tokens(settings), settings.connector_timeout_seconds * 2
    )


def casework_connector(
    settings: Settings, tenant_id: uuid.UUID, operation: dict[str, Any], org: str
) -> McpCaseworkConnector:
    # Capabilities are those of the connector the casework service uses (sim_tasks for this
    # organization), discovered from the tracker so the worker and the service agree.
    caps = SimTaskConnector(
        settings.simulator_url, settings.sim_master_secret.get_secret_value(), org
    ).capabilities
    caps = ConnectorCapabilities(
        caps.idempotency_keys,
        caps.lookup,
        caps.conditional_writes,
        caps.definite_failure_statuses,
        caps.retryable_definite_statuses,
        notes="via Casework MCP; " + caps.notes,
    )
    return McpCaseworkConnector(
        client=McpToolClient(
            settings.casework_mcp_url, worker_tokens(settings), settings.connector_timeout_seconds
        ),
        signer=signer(settings),
        tenant_id=tenant_id,
        operation_id=operation["operation_id"],
        approver_id=operation["approved_by"],
        case_id=operation["case_id"],
        capabilities=caps,
    )
