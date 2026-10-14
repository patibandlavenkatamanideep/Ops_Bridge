"""Organization pack loading: policies (and, from Phase 2, source field mappings).

Packs are validated data files. Core code never branches on a pack or tenant name.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from opsbridge.canonical import digest
from opsbridge.domain.policy import ReconciliationPolicy
from opsbridge.mapping import PackSpec


@dataclass(frozen=True)
class LoadedPolicy:
    policy: ReconciliationPolicy
    document: dict[str, Any]
    sha256: str


def load_policy_file(path: Path) -> LoadedPolicy:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    policy = ReconciliationPolicy.model_validate(raw)
    document = policy.model_dump(mode="json")
    return LoadedPolicy(policy=policy, document=document, sha256=digest(document))


class PackRegistry:
    """Loads and caches validated pack specs from the packs directory."""

    def __init__(self, packs_dir: Path) -> None:
        self.packs_dir = packs_dir
        self._cache: dict[str, PackSpec] = {}

    def get(self, pack_id: str) -> PackSpec:
        if pack_id not in self._cache:
            if not pack_id.replace("_", "").isalnum():
                raise ValueError("invalid pack id")
            raw = yaml.safe_load(
                (self.packs_dir / pack_id / "pack.yaml").read_text(encoding="utf-8")
            )
            self._cache[pack_id] = PackSpec.model_validate(raw)
        return self._cache[pack_id]

    def digest(self, pack_id: str) -> str:
        return digest(self.get(pack_id).model_dump(mode="json"))
