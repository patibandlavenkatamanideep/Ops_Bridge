"""S3 artifact store against a real S3-compatible server (SeaweedFS from `make infra-up`).
Not AWS S3: server-side encryption is not exercised here (the local server does not support
it without its own key setup); only parameter passing is unit-tested."""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

import boto3
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from opsbridge.api.app import create_app
from opsbridge.evidence.storage import ArtifactIntegrityError, ArtifactKeyError, S3ArtifactStore
from opsbridge.persistence.db import Database
from opsbridge.workflow.retention import sweep

from .test_api_cases import call
from .test_approvals_execution import drain
from .test_hardening import investigated, tenant_id, upload

pytestmark = pytest.mark.integration
ENDPOINT = os.environ.get("OPSBRIDGE_TEST_S3_ENDPOINT", "http://127.0.0.1:8333")


@pytest.fixture
def bucket(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "local-test")  # the local server is anonymous
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "local-test")
    name = f"opsbridge-test-{uuid.uuid4().hex[:12]}"
    _client().create_bucket(Bucket=name)
    return name


def _client() -> Any:
    return boto3.client("s3", endpoint_url=ENDPOINT, region_name="us-east-1")


def test_store_round_trip_isolation_and_tamper_detection(bucket: str) -> None:
    store = S3ArtifactStore(bucket, prefix="artifacts", endpoint_url=ENDPOINT, region="us-east-1")
    a, b = uuid.uuid4(), uuid.uuid4()
    key, sha = store.put(a, b"synthetic invoice bytes")
    assert store.exists(a, key) and store.get(a, key) == b"synthetic invoice bytes"
    assert store.put(a, b"synthetic invoice bytes") == (key, sha)  # idempotent
    with pytest.raises(ArtifactKeyError):
        store.get(b, key)
    _client().put_object(Bucket=bucket, Key=f"artifacts/{key}", Body=b"tampered")
    with pytest.raises(ArtifactIntegrityError):
        store.get(a, key)
    assert store.delete(a, key) is True and store.exists(a, key) is False
    with pytest.raises(FileNotFoundError):
        store.get(a, key)


def test_uploads_downloads_and_retention_use_the_bucket(
    stack: dict[str, Any], bucket: str, worker_db: Database, migrator_engine: Engine
) -> None:
    st = stack
    s3_settings = st["settings"].model_copy(
        update={
            "artifact_s3_bucket": bucket,
            "artifact_s3_endpoint_url": ENDPOINT,
            "artifact_s3_region": "us-east-1",
        }
    )
    case_id = investigated(st, "INV-9001")
    with TestClient(
        create_app(settings=s3_settings, db=st["client"].app.state.opsbridge.db),
        base_url="http://localhost:5173",
    ) as client:
        actor = st["actors"]["furn-investigator"]
        r = upload(
            client, actor, case_id, "attachment", "note.txt", b"stored in the bucket", "text/plain"
        )
        assert r.status_code == 201, r.text
        vid = r.json()["version_id"]
        client.cookies.set("opsbridge_session", actor.token)
        dl = client.get(
            f"/api/v1/tenants/demo-furniture/cases/{case_id}/documents/versions/{vid}/original"
        )
        assert dl.status_code == 200 and dl.content == b"stored in the bucket"
    tid = tenant_id(migrator_engine, "demo-furniture")
    objects = [o["Key"] for o in _client().list_objects_v2(Bucket=bucket).get("Contents", [])]
    assert any(k.startswith(f"{tid}/") for k in objects)
    assert not any(
        Path(st["settings"].artifact_dir).rglob("*" + objects[0].rsplit("/", 1)[1])
    )  # not on local disk
    adm = st["actors"]["furn-admin"]
    assert (
        call(
            st["client"],
            adm,
            "PUT",
            "/api/v1/tenants/demo-furniture/settings",
            json={"retention": {"raw_documents_days": 0}},
        ).status_code
        == 200
    )
    report = sweep(
        worker_db, S3ArtifactStore(bucket, endpoint_url=ENDPOINT, region="us-east-1"), tid
    )
    assert report.artifacts_deleted >= 1
    assert not _client().list_objects_v2(Bucket=bucket).get("Contents")  # deleted from the bucket
    drain(st)
