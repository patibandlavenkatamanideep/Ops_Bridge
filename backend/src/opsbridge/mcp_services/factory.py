"""Build MCP clients/connectors and servers from settings (no organization branches)."""

from __future__ import annotations

import uuid
from typing import Any

from opsbridge.config import Settings
from opsbridge.connectors.tasks import ConnectorCapabilities, SimTaskConnector

from .auth import DelegationSigner, DelegationVerifier, load_or_create_key
from .client import ClientCredentialsTokens, McpCaseworkConnector, McpToolClient

RECORDS_AUDIENCE = "opsbridge-records-mcp"
CASEWORK_AUDIENCE = "opsbridge-casework-mcp"


def signer(settings: Settings) -> DelegationSigner:
    return DelegationSigner(load_or_create_key(settings.delegation_key_path))


def verifier(settings: Settings, audience: str) -> DelegationVerifier:
    return DelegationVerifier(
        load_or_create_key(settings.delegation_key_path).public_key(), audience
    )


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
    settings: Settings, tenant_id: uuid.UUID, operation: dict[str, Any]
) -> McpCaseworkConnector:
    # Capabilities are those of the connector the casework service uses (sim_tasks).
    caps = SimTaskConnector(settings.simulator_url, "unused", "unused").capabilities
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
