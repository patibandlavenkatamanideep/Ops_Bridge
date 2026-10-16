"""S3 artifact store: parameters, key isolation and read integrity (no network)."""

from __future__ import annotations

import hashlib
import io
import uuid
from typing import Any

import pytest

from opsbridge.evidence.storage import ArtifactIntegrityError, ArtifactKeyError, S3ArtifactStore


class FakeS3:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.puts: list[dict[str, Any]] = []

    def head_object(self, Bucket: str, Key: str) -> dict[str, Any]:
        if Key not in self.objects:
            err = Exception("missing")
            err.response = {"Error": {"Code": "404"}}  # type: ignore[attr-defined]
            raise err
        return {}

    def put_object(self, **kw: Any) -> dict[str, Any]:
        self.puts.append(kw)
        self.objects[kw["Key"]] = kw["Body"]
        return {}

    def get_object(self, Bucket: str, Key: str) -> dict[str, Any]:
        return {"Body": io.BytesIO(self.objects[Key])}

    def delete_object(self, Bucket: str, Key: str) -> dict[str, Any]:
        self.objects.pop(Key, None)
        return {}


def test_encryption_parameters_and_prefix_are_sent() -> None:
    fake = FakeS3()
    store = S3ArtifactStore(
        "bucket", prefix="opsbridge/", sse="aws:kms", kms_key_id="alias/opsbridge", client=fake
    )
    tenant = uuid.uuid4()
    key, sha = store.put(tenant, b"synthetic")
    assert sha == "sha256:" + hashlib.sha256(b"synthetic").hexdigest()
    [put] = fake.puts
    assert put["Key"] == f"opsbridge/{key}" and put["ServerSideEncryption"] == "aws:kms"
    assert put["SSEKMSKeyId"] == "alias/opsbridge"
    store.put(tenant, b"synthetic")
    assert len(fake.puts) == 1  # content-addressed: identical bytes are not uploaded twice


def test_keys_are_tenant_scoped_and_reads_are_verified() -> None:
    fake = FakeS3()
    store = S3ArtifactStore("bucket", client=fake)
    a, b = uuid.uuid4(), uuid.uuid4()
    key, _ = store.put(a, b"tenant a bytes")
    for bad in (key, f"{a}/../{key}", "../../etc/passwd"):
        with pytest.raises(ArtifactKeyError):
            store.get(b, bad)
    fake.objects[key] = b"replaced"  # someone overwrote the object
    with pytest.raises(ArtifactIntegrityError):
        store.get(a, key)
