"""Protected artifact storage behind an interface.

The local implementation stores bytes under <root>/<tenant_id>/<sha256[:2]>/<sha256>.
Keys are derived from content digests and tenant IDs only — never from user-supplied
names — so path traversal and cross-tenant reads by key are impossible. Callers must
still authorize access through the artifact manifest (RLS-protected) before reading.
"""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from pathlib import Path
from typing import Protocol

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
