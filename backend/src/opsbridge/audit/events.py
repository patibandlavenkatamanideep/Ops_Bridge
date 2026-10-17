"""Append-only operational audit events with minimal, non-sensitive details."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from opsbridge.persistence.models import AuditEvent


def record(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    actor_kind: str,
    actor_id: str,
    action: str,
    resource_type: str,
    resource_id: str,
    outcome: str = "ok",
    on_behalf_of: uuid.UUID | None = None,
    correlation_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """Add an audit event to the current transaction (committed with the business change)."""
    session.add(
        AuditEvent(
            tenant_id=tenant_id,
            actor_kind=actor_kind,
            actor_id=actor_id,
            on_behalf_of=on_behalf_of,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            outcome=outcome,
            correlation_id=correlation_id,
            details=details or {},
        )
    )
