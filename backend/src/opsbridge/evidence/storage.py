"""Protected artifact storage behind an interface.

The local implementation stores bytes under <root>/<tenant_id>/<sha256[:2]>/<sha256>.
Keys are derived from content digests and tenant IDs only — never from user-supplied
names — so path traversal and cross-tenant reads by key are impossible. Callers must
still authorize access through the artifact manifest (RLS-protected) before reading.
"""

from __future__ import annotations

import functools
import hashlib
import os
import re
import uuid
from pathlib import Path
from typing import Any, Protocol

_KEY_RE = re.compile(r"^[0-9a-f-]{36}/[0-9a-f]{2}/[0-9a-f]{64}$")


class ArtifactStore(Protocol):
    def put(self, tenant_id: uuid.UUID, data: bytes) -> tuple[str, str]: ...
    def get(self, tenant_id: uuid.UUID, key: str) -> bytes: ...
    def delete(self, tenant_id: uuid.UUID, key: str) -> bool: ...
    def exists(self, tenant_id: uuid.UUID, key: str) -> bool: ...


class ArtifactKeyError(ValueError):
    pass


class LocalArtifactStore:
    """Filesystem store. Not encrypted by this code; see docs/privacy-and-retention.md."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def _path(self, tenant_id: uuid.UUID, key: str) -> Path:
        if not _KEY_RE.match(key) or not key.startswith(str(tenant_id) + "/"):
            raise ArtifactKeyError("artifact key does not belong to this tenant")
        path = (self.root / key).resolve()
        if self.root not in path.parents:
            raise ArtifactKeyError("artifact key escapes the store root")
        return path

    def put(self, tenant_id: uuid.UUID, data: bytes) -> tuple[str, str]:
        """Store bytes; return (storage_key, 'sha256:<hex>'). Idempotent per content."""
        hexdigest = hashlib.sha256(data).hexdigest()
        key = f"{tenant_id}/{hexdigest[:2]}/{hexdigest}"
        path = self._path(tenant_id, key)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            tmp = path.with_suffix(f".tmp-{uuid.uuid4().hex}")
            with open(tmp, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        return key, f"sha256:{hexdigest}"

    def get(self, tenant_id: uuid.UUID, key: str) -> bytes:
        return self._path(tenant_id, key).read_bytes()

    def exists(self, tenant_id: uuid.UUID, key: str) -> bool:
        return self._path(tenant_id, key).exists()

    def delete(self, tenant_id: uuid.UUID, key: str) -> bool:
        path = self._path(tenant_id, key)
        if path.exists():
            path.unlink()
            return True
        return False


class ArtifactIntegrityError(RuntimeError):
    """Stored bytes no longer match the digest in their key."""


@functools.lru_cache(maxsize=4)
def _s3_client(endpoint_url: str | None, region: str | None) -> Any:
    """One shared client per endpoint: boto3 clients are thread-safe but slow to create,
    and a store is built per request. Role credentials are refreshed by botocore."""
    import boto3  # optional at import time: local development never needs it

    return boto3.client("s3", endpoint_url=endpoint_url, region_name=region)


class S3ArtifactStore:
    """S3-compatible object store (staging). Same tenant-scoped, content-addressed keys as
    the local store; every read is re-hashed against the digest in its key, so a replaced
    or corrupted object is refused rather than served. Encryption at rest is whatever is
    requested here (`sse`) or enforced by the bucket — this code does not encrypt bytes.
    Credentials come from the standard AWS chain (environment or role), never settings files.
    """

    def __init__(
        self,
        bucket: str,
        *,
        prefix: str = "",
        sse: str | None = None,
        kms_key_id: str | None = None,
        client: Any = None,
        endpoint_url: str | None = None,
        region: str | None = None,
    ) -> None:
        self.client = client if client is not None else _s3_client(endpoint_url, region)
        self.bucket = bucket
        self.prefix = prefix.strip("/") + "/" if prefix.strip("/") else ""
        self._put_extra: dict[str, str] = {}
        if sse:
            self._put_extra["ServerSideEncryption"] = sse
            if sse == "aws:kms" and kms_key_id:
                self._put_extra["SSEKMSKeyId"] = kms_key_id

    def _object(self, tenant_id: uuid.UUID, key: str) -> str:
        if not _KEY_RE.match(key) or not key.startswith(str(tenant_id) + "/"):
            raise ArtifactKeyError("artifact key does not belong to this tenant")
        return self.prefix + key

    def _missing(self, exc: Exception) -> bool:
        code = str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))
        return code in ("404", "NoSuchKey", "NotFound")

    def put(self, tenant_id: uuid.UUID, data: bytes) -> tuple[str, str]:
        hexdigest = hashlib.sha256(data).hexdigest()
        key = f"{tenant_id}/{hexdigest[:2]}/{hexdigest}"
        if not self.exists(tenant_id, key):  # content-addressed: identical bytes already stored
            self.client.put_object(
                Bucket=self.bucket,
                Key=self._object(tenant_id, key),
                Body=data,
                ContentType="application/octet-stream",
                **self._put_extra,
            )
        return key, f"sha256:{hexdigest}"

    def get(self, tenant_id: uuid.UUID, key: str) -> bytes:
        try:
            obj = self.client.get_object(Bucket=self.bucket, Key=self._object(tenant_id, key))
        except Exception as exc:
            if self._missing(exc):
                raise FileNotFoundError(key) from exc
            raise
        data: bytes = obj["Body"].read()
        if hashlib.sha256(data).hexdigest() != key.rsplit("/", 1)[1]:
            raise ArtifactIntegrityError(f"stored object {key} does not match its digest")
        return data

    def exists(self, tenant_id: uuid.UUID, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=self._object(tenant_id, key))
            return True
        except Exception as exc:
            if self._missing(exc):
                return False
            raise

    def delete(self, tenant_id: uuid.UUID, key: str) -> bool:
        existed = self.exists(tenant_id, key)
        if existed:
            self.client.delete_object(Bucket=self.bucket, Key=self._object(tenant_id, key))
        return existed


def artifact_store(settings: Any) -> ArtifactStore:
    """The configured store: S3-compatible when a bucket is set, else the local filesystem."""
    if getattr(settings, "artifact_s3_bucket", None):
        return S3ArtifactStore(
            settings.artifact_s3_bucket,
            prefix=settings.artifact_s3_prefix,
            sse=settings.artifact_s3_sse or None,
            kms_key_id=settings.artifact_s3_kms_key_id,
            endpoint_url=settings.artifact_s3_endpoint_url,
            region=settings.artifact_s3_region,
        )
    return LocalArtifactStore(settings.artifact_dir)
