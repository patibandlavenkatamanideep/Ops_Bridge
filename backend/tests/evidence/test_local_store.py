"""Local artifact reads verify content against the digest in the key (review finding F16).

The S3 store already did; the local store returned whatever bytes were on disk, so altered
evidence was served as if it were the original.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from opsbridge.evidence.storage import ArtifactIntegrityError, LocalArtifactStore


def test_altered_bytes_are_refused_and_a_rewrite_repairs_them(tmp_path: Path) -> None:
    store, tenant = LocalArtifactStore(tmp_path), uuid.uuid4()
    key, digest = store.put(tenant, b"Invoice INV-9001: 20 EA")
    assert store.get(tenant, key) == b"Invoice INV-9001: 20 EA"
    (tmp_path / key).write_bytes(b"Invoice INV-9001: 2 EA")  # bit rot or tampering
    with pytest.raises(ArtifactIntegrityError, match="does not match its digest"):
        store.get(tenant, key)
    assert store.put(tenant, b"Invoice INV-9001: 20 EA") == (key, digest)  # same content again
    assert store.get(tenant, key) == b"Invoice INV-9001: 20 EA"
