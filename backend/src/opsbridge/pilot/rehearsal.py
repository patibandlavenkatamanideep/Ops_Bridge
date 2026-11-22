"""The pilot's synthetic rehearsal export (docs/pilot/quickstart.md).

Renders the rehearsal cases (fixtures/pilot/rehearsal.yaml) into an export in the format a
customer delivers (`opsbridge-export/1`, connectors/exports.py): one file per record, each
listed in the manifest with its digest. The export is dated when it is written, so its data is
as current as a fresh customer export. It is synthetic and says so in its manifest; it never
writes over an existing export.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

from opsbridge.config import REPO_ROOT
from opsbridge.connectors.exports import EXPORT_SCHEMA
from opsbridge.simulator.scenarios import ORG_FOR_PACK, Corpus, Record, render

REHEARSAL = REPO_ROOT / "fixtures" / "pilot" / "rehearsal.yaml"
SOURCE_SYSTEM = "OpsBridge synthetic pilot rehearsal (not a customer export)"
_SUFFIX = {
    "application/json": ".json",
    "text/csv": ".csv",
    "application/pdf": ".pdf",
    "text/plain": ".txt",
}


class ExportExists(Exception):
    pass


def write_rehearsal_export(corpus: Corpus, out: Path, extracted_at: datetime) -> Path:
    """Write the corpus as an export under `out`/<organization>; returns that directory."""
    [org] = {ORG_FOR_PACK[s.pack] for s in corpus.scenarios}
    records = [r for s in corpus.scenarios for r in render(s)]
    return write_export(records, out / org, SOURCE_SYSTEM, extracted_at)


def write_export(
    records: list[Record], root: Path, source_system: str, extracted_at: datetime
) -> Path:
    """Write rendered records as a complete export in `root`, which must be empty or absent:
    one file per record, each listed in the manifest with its digest."""
    if root.exists() and any(root.iterdir()):
        raise ExportExists(f"{root} is not empty; an export is never written over")
    entries = []
    for collection, key, body, content_type, po in records:
        name = f"{collection}/{key}{_SUFFIX[content_type]}"
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(body)
        entries.append(
            {
                "collection": collection,
                "key": key,
                "po_number": po,
                "file": name,
                "content_type": content_type,
                "sha256": hashlib.sha256(body).hexdigest(),
            }
        )
    manifest = {
        "schema": EXPORT_SCHEMA,
        "source_system": source_system,
        "extracted_at": extracted_at.isoformat(),
        "complete": True,
        "records": entries,
        "withdrawn": [],
    }
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return root
