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
