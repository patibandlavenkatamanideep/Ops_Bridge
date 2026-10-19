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

    @staticmethod
    def _missing(code: str) -> Exception:  # shaped like botocore's ClientError
        err = Exception(code)
        err.response = {"Error": {"Code": code}}  # type: ignore[attr-defined]
        return err

    def head_object(self, Bucket: str, Key: str) -> dict[str, Any]:
        if Key not in self.objects:
            raise self._missing("404")
        return {}

    def put_object(self, **kw: Any) -> dict[str, Any]:
        self.puts.append(kw)
        self.objects[kw["Key"]] = kw["Body"]
        return {}

    def get_object(self, Bucket: str, Key: str) -> dict[str, Any]:
        if Key not in self.objects:
            raise self._missing("NoSuchKey")
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


@pytest.mark.parametrize("backend", ["local", "s3"])
def test_restore_check_reads_through_either_store(backend: str, tmp_path: Any) -> None:
    from types import SimpleNamespace

    from opsbridge.evidence.storage import LocalArtifactStore
    from opsbridge.operations.restore import artifact_problems

    fake = FakeS3()
    store = (
        LocalArtifactStore(tmp_path) if backend == "local" else S3ArtifactStore("b", client=fake)
    )
    tenant = uuid.uuid4()
    good_key, good_sha = store.put(tenant, b"kept")
    gone_key, gone_sha = store.put(tenant, b"lost in the restore")
    store.delete(tenant, gone_key)
    arts = [
        SimpleNamespace(id="good", storage_key=good_key, sha256=good_sha),
        SimpleNamespace(id="gone", storage_key=gone_key, sha256=gone_sha),
        SimpleNamespace(id="other", storage_key=f"{uuid.uuid4()}/00/{'0' * 64}", sha256="sha256:x"),
    ]
    assert artifact_problems(store, tenant, arts) == ["missing gone", "key outside tenant other"]
    if backend == "local":
        (tmp_path / good_key).write_bytes(b"tampered")
    else:
        fake.objects[good_key] = b"tampered"
    assert artifact_problems(store, tenant, arts[:1]) == ["digest mismatch good"]
