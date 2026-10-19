"""LIVE GitHub sandbox check. Never runs by default (marker `live` is always skipped
unless OPSBRIDGE_RUN_LIVE=github and live writes are explicitly authorized)."""

from __future__ import annotations

import os
import uuid

import pytest

from opsbridge.config import get_settings
from opsbridge.connectors.github import github_connector_from_settings
from opsbridge.connectors.tasks import TaskRequest

pytestmark = pytest.mark.live


def test_create_and_read_back_one_sandbox_issue() -> None:
    s = get_settings()
    if os.environ.get("OPSBRIDGE_RUN_LIVE") != "github" or not s.github_live_writes_authorized:
        pytest.skip("live GitHub run not authorized")
    conn = github_connector_from_settings(s)
    marker = f"opsbridge-op:live-{uuid.uuid4().hex[:12]}"
    task = conn.create(
        TaskRequest(
            "OpsBridge sandbox connectivity check (synthetic)",
            f"Synthetic test issue.\\n\\nOpsBridge operation marker: {marker}",
            None,
            marker,
        ),
        None,
        10.0,
    )
    back = conn.get(task.remote_ref, 10.0)
    assert back is not None and back.marker == marker
