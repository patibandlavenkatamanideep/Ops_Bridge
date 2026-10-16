"""Privacy, retention and adversarial hardening (Phase 6 exit gate; OB-10, OB-14, OB-15, OB-16)."""

from __future__ import annotations

import json
import uuid
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine, select, text
from sqlalchemy.pool import QueuePool

from opsbridge.evidence.retrieval import search
from opsbridge.evidence.storage import ArtifactKeyError, LocalArtifactStore
from opsbridge.persistence.db import Database, DbContext, make_engine
from opsbridge.persistence.models import DocumentVersion, PolicyVersion

from .stack import get, open_case
from .test_api_cases import Actor, call
from .test_approvals_execution import approve, drain, operations, prepared, remote_tasks

pytestmark = pytest.mark.integration
T = "/api/v1/tenants/demo-furniture"
DOCS = Path(__file__).resolve().parents[3] / "fixtures" / "documents"


def upload(
    client: Any,
    actor: Actor,
    case_id: str,
    kind: str,
    name: str,
    data: bytes,
    ctype: str,
    tenant: str = "demo-furniture",
) -> httpx.Response:
    client.cookies.clear()
    client.cookies.set("opsbridge_session", actor.token)
    return client.post(
        f"/api/v1/tenants/{tenant}/cases/{case_id}/documents",
        data={"kind": kind},
        files={"file": (name, data, ctype)},
        headers={"X-CSRF-Token": actor.csrf},
    )


def investigated(
    st: dict[str, Any], invoice: str, tenant: str = "demo-furniture", who: str = "furn-investigator"
) -> str:
    case_id = open_case(st, who, tenant, invoice)
    r = call(
        st["client"],
        st["actors"][who],
        "POST",
        f"/api/v1/tenants/{tenant}/cases/{case_id}/investigations",
        json={"model_mode": "rules_only"},
    )
    assert r.status_code in (200, 202), r.text
    drain(st)
    return case_id


def tenant_id(engine: Engine, slug: str) -> uuid.UUID:
    with engine.begin() as conn:
        return conn.execute(
            text("SELECT id FROM tenants WHERE slug = :s"), {"s": slug}
        ).scalar_one()


# --- uploads ------------------------------------------------------------------------------------


def test_upload_parses_versions_and_links_supplier_message(stack) -> None:  # type: ignore[no-untyped-def]
    st = stack
    case_id = investigated(st, "INV-9001")
    actor = st["actors"]["furn-investigator"]
    body = b"Hello,\nWe delivered all 20 chairs for PO-1001 on Monday.\nRegards"
    r = upload(st["client"], actor, case_id, "supplier_message", "email.txt", body, "text/plain")
    assert r.status_code == 201, r.text
    first = r.json()
    assert first["parse_status"] == "parsed" and first["records"]["supplier_assertions"] == 1
    r2 = upload(
        st["client"],
        actor,
        case_id,
        "supplier_message",
        "email.txt",
        body + b"\nP.S. updated",
        "text/plain",
    )
    assert r2.json()["document_id"] == first["document_id"] and r2.json()["version_no"] == 2
    docs = get(st, "furn-investigator", f"{T}/cases/{case_id}/documents").json()["items"]
    assert any(d["document_id"] == first["document_id"] and len(d["versions"]) == 2 for d in docs)


@pytest.mark.parametrize(
    ("kind", "name", "data", "ctype", "status", "code"),
    [
        (
            "supplier_message",
            "page.txt",
            b"<html><script>alert(1)</script></html>",
            "text/plain",
            409,
            "upload_rejected",
        ),
        ("invoice", "inv.pdf", b'{"not": "a pdf"}', "application/pdf", 409, "upload_rejected"),
        ("attachment", "image.png", b"\x89PNG\r\n\x1a\n", "image/png", 409, "upload_rejected"),
        ("payment_instruction", "x.txt", b"hello", "text/plain", 409, "invalid_kind"),
    ],
)
def test_upload_rejects_disguised_or_unsupported_content(
    stack, kind: str, name: str, data: bytes, ctype: str, status: int, code: str
) -> None:  # type: ignore[no-untyped-def]
    st = stack
    case_id = investigated(st, "INV-9001")
    r = upload(st["client"], st["actors"]["furn-investigator"], case_id, kind, name, data, ctype)
    assert r.status_code == status and r.json()["error"]["code"] == code


def test_upload_size_filename_tenant_and_role_defences(stack, app_db: Database) -> None:  # type: ignore[no-untyped-def]
    st = stack
    case_id = investigated(st, "INV-9001")
    big = b"x" * (st["settings"].max_upload_bytes + 1)
    r = upload(
        st["client"],
        st["actors"]["furn-investigator"],
        case_id,
        "attachment",
        "big.txt",
        big,
        "text/plain",
    )
    assert r.status_code == 413
    r = upload(
        st["client"],
        st["actors"]["furn-investigator"],
        case_id,
        "attachment",
        "../../etc/passwd.txt",
        b"synthetic note",
        "text/plain",
    )
    assert r.status_code == 201
    vid = r.json()["version_id"]
    detail = get(st, "furn-investigator", f"{T}/cases/{case_id}/documents/versions/{vid}").json()
    original = get(
        st, "furn-investigator", f"{T}/cases/{case_id}/documents/versions/{vid}/original"
    )
    assert 'filename="passwd.txt"' in original.headers["content-disposition"]
    assert detail["version"]["parse_status"] == "parsed"
    # Another tenant cannot upload into this case; a requester lacks evidence.upload.
    r = upload(
        st["client"],
        st["actors"]["it-investigator"],
        case_id,
        "attachment",
        "a.txt",
        b"x",
        "text/plain",
    )
    assert r.status_code == 404
    r = upload(
        st["client"],
        st["actors"]["it-investigator"],
        case_id,
        "attachment",
        "a.txt",
        b"x",
        "text/plain",
        tenant="demo-it",
    )
    assert r.status_code == 404
    r = upload(
        st["client"],
        st["actors"]["furn-requester"],
        case_id,
        "attachment",
        "a.txt",
        b"x",
        "text/plain",
    )
    assert r.status_code in (403, 404)


def test_chunked_body_without_length_is_refused_before_buffering(stack) -> None:  # type: ignore[no-untyped-def]
    st = stack
    case_id = investigated(st, "INV-9001")
    actor = st["actors"]["furn-investigator"]
    st["client"].cookies.clear()
    st["client"].cookies.set("opsbridge_session", actor.token)
    r = st["client"].post(
        f"{T}/cases/{case_id}/messages",
        content=iter([b'{"body": "', b"x" * 10, b'"}']),
        headers={"X-CSRF-Token": actor.csrf, "content-type": "application/json"},
    )
    assert r.status_code == 411 and r.json()["error"]["code"] == "length_required"


def test_original_downloads_are_never_cached_or_rendered_inline(stack) -> None:  # type: ignore[no-untyped-def]
    st = stack
    case_id = investigated(st, "INV-9001")
    r = upload(
        st["client"],
        st["actors"]["furn-investigator"],
        case_id,
        "attachment",
        "note.txt",
        b"<b>synthetic</b> note",
        "text/plain",
    )
    vid = r.json()["version_id"]
    url = f"{T}/cases/{case_id}/documents/versions/{vid}/original"
    dl = get(st, "furn-investigator", url)
    assert dl.status_code == 200
    assert dl.headers["cache-control"] == "no-store"
    assert dl.headers["x-content-type-options"] == "nosniff"
    assert dl.headers["content-disposition"].startswith("attachment;")
    assert (
        get(
            st,
            "it-investigator",
            f"/api/v1/tenants/demo-it/cases/{case_id}/documents/versions/{vid}/original",
        ).status_code
        == 404
    )


# --- conversation history -------------------------------------------------------------------------


def test_conversation_correction_and_controlled_deletion(stack) -> None:  # type: ignore[no-untyped-def]
    st = stack
    case_id = investigated(st, "INV-9001")
    inv, adm = st["actors"]["furn-investigator"], st["actors"]["furn-admin"]
    m1 = call(
        st["client"],
        inv,
        "POST",
        f"{T}/cases/{case_id}/messages",
        json={"body": "Called receiving: 18 counted"},
    )
    assert m1.status_code == 201
    m2 = call(
        st["client"],
        inv,
        "POST",
        f"{T}/cases/{case_id}/messages",
        json={"body": "Correction: 18 counted, 2 on back order", "supersedes_id": m1.json()["id"]},
    )
    assert m2.json()["supersedes_id"] == m1.json()["id"]
    r = call(
        st["client"],
        st["actors"]["furn-approver"],
        "POST",
        f"{T}/cases/{case_id}/messages",
        json={"body": "x", "supersedes_id": m1.json()["id"]},
    )
    assert r.status_code in (
        403,
        409,
    )  # approvers lack conversation.write; non-authors cannot correct
    d = call(
        st["client"],
        adm,
        "POST",
        f"{T}/cases/{case_id}/messages/{m1.json()['id']}/delete",
        json={"reason": "Contained a phone number"},
    )
    assert d.status_code == 200
    gone = d.json()
    assert gone["body"] is None and gone["deletion_reason"] == "Contained a phone number"
    assert (
        gone["body_sha256"] == m1.json()["body_sha256"]
    )  # integrity reference kept (not anonymization)
    items = get(st, "furn-investigator", f"{T}/cases/{case_id}/messages").json()["items"]
    assert [i["body"] for i in items] == [None, "Correction: 18 counted, 2 on back order"]
    r = get(st, "it-investigator", f"/api/v1/tenants/demo-it/cases/{case_id}/messages")
    assert r.status_code == 404


# --- tenant settings: tiers are features, roles are authority ------------------------------------


def test_tenant_settings_respect_roles_and_tiers(stack) -> None:  # type: ignore[no-untyped-def]
    st = stack
    adm = st["actors"]["furn-admin"]
    view = get(st, "furn-admin", f"{T}/settings").json()
    assert (
        view["tenant"]["tier"] == "standard"
        and "anthropic" not in view["entitlements"]["model_modes"]
    )
    assert any(m["username"] == "furn-approver" for m in view["members"])
    for body in (
        {"model_mode": "anthropic"},
        {"task_connector": "github_issues"},
        {"retention": {"raw_documents_days": -1}},
        {"retention": {"everything_days": 3}},
    ):
        r = call(st["client"], adm, "PUT", f"{T}/settings", json=body)
        assert r.status_code == 409, body
    assert (
        call(
            st["client"], adm, "PUT", f"{T}/settings", json={"model_mode": "rules_only"}
        ).status_code
        == 200
    )
    assert get(st, "furn-investigator", f"{T}/settings").status_code == 403
    assert get(st, "furn-auditor", f"{T}/settings").status_code == 200
    r = call(
        st["client"],
        st["actors"]["furn-auditor"],
        "PUT",
        f"{T}/settings",
        json={"model_mode": "mock"},
    )
    assert r.status_code == 403


# --- retention ------------------------------------------------------------------------------------


def test_retention_deletes_protected_content_and_says_so(stack) -> None:  # type: ignore[no-untyped-def]
    st = stack
    case_id = investigated(st, "INV-9001")
    docs = get(st, "furn-investigator", f"{T}/cases/{case_id}/documents").json()["items"]
    vid = docs[0]["versions"][0]["version_id"]
    assert (
        get(
            st, "furn-investigator", f"{T}/cases/{case_id}/documents/versions/{vid}/original"
        ).status_code
        == 200
    )
    adm = st["actors"]["furn-admin"]
    assert (
        call(
            st["client"],
            adm,
            "PUT",
            f"{T}/settings",
            json={"retention": {"raw_documents_days": 0, "model_artifacts_days": 0}},
        ).status_code
        == 200
    )
    r = call(st["client"], adm, "POST", f"{T}/retention/run")
    assert r.status_code == 202
    drain(st)
    r = get(st, "furn-investigator", f"{T}/cases/{case_id}/documents/versions/{vid}/original")
    assert r.status_code == 404 and "deleted by retention policy" in r.json()["error"]["message"]
    detail = get(st, "furn-investigator", f"{T}/cases/{case_id}/documents/versions/{vid}").json()
    assert detail["chunks"] == []  # extracted text deleted with the original
    assert detail["version"]["raw_sha256"].startswith("sha256:")  # the integrity reference remains
    inv = get(st, "furn-investigator", f"{T}/cases/{case_id}/investigation").json()
    assert inv["outcome"] == "discrepancies_found"  # findings remain; excerpts no longer available


# --- stale inputs between approval and dispatch ---------------------------------------------------


def test_policy_published_before_dispatch_cancels_the_operation(
    stack, migrator_engine: Engine
) -> None:  # type: ignore[no-untyped-def]
    st = stack
    case_id, prop = prepared(st, "INV-9001")
    assert approve(st, case_id, prop).status_code == 200
    tid = tenant_id(migrator_engine, "demo-furniture")
    mdb = Database(make_engine("migrator", database="opsbridge_test"))
    with mdb.session(DbContext(tenant_id=tid)) as s:
        v2 = s.execute(select(PolicyVersion).where(PolicyVersion.version == 2)).scalar_one()
        doc = {**v2.document, "version": 3, "effective_from": "2026-06-01"}
        s.add(
            PolicyVersion(
                tenant_id=tid,
                policy_id=v2.policy_id,
                version=3,
                effective_from=date(2026, 6, 1),
                document=doc,
                sha256="sha256:" + "3" * 64,
            )
        )
    mdb.engine.dispose()
    drain(st)
    [op] = operations(st, case_id)
    assert op["status"] == "cancelled_before_dispatch" and remote_tasks(st, op) == []


def test_changed_source_document_creates_a_new_version_and_new_evidence_state(stack) -> None:  # type: ignore[no-untyped-def]
    st = stack
    case_id = investigated(st, "INV-9003")
    before = get(st, "furn-investigator", f"{T}/cases/{case_id}/investigation").json()
    po = {
        "po_number": "PO-1003",
        "order_date": "2026-09-01",
        "supplier": {"id": "SUP-ACME", "name": "Acme Office Furniture Inc"},
        "currency": "USD",
        "status": "OPEN",
        "lines": [
            {
                "line_no": 1,
                "sku": "CB-310",
                "description": "Filing cabinet",
                "qty_ordered": "10",
                "uom": "EA",
                "unit_price": "310.00",
                "discount": "0.00",
            }
        ],
    }
    httpx.put(
        f"{st['base']}/_control/records/demo-furniture/purchase_orders/PO-1003?po_number=PO-1003",
        content=json.dumps(po).encode(),
        headers={"content-type": "application/json"},
    ).raise_for_status()
    r = call(
        st["client"],
        st["actors"]["furn-investigator"],
        "POST",
        f"{T}/cases/{case_id}/investigations",
        json={"model_mode": "rules_only"},
    )
    assert r.status_code in (200, 202)
    drain(st)
    after = get(st, "furn-investigator", f"{T}/cases/{case_id}/investigation").json()
    assert after["source_manifest_digest"] != before["source_manifest_digest"]
    reasons = {f["reason_code"] for f in after["findings"]}
    assert "invoiced_exceeds_ordered" in reasons  # 12 invoiced vs 10 now ordered
    po_cite = next(c for c in after["citations"] if c["field"] == "po_line.quantity")
    assert po_cite["version_no"] == 2 and po_cite["value"] == "10"


# --- isolation of retrieval, artifacts and pooled worker connections -------------------------------


def test_retrieval_authorizes_before_ranking_across_tenants(
    stack, worker_db: Database, migrator_engine: Engine
) -> None:  # type: ignore[no-untyped-def]
    st = stack
    investigated(st, "INV-9001")
    investigated(st, "INV-7002", tenant="demo-it", who="it-investigator")
    furn, it = tenant_id(migrator_engine, "demo-furniture"), tenant_id(migrator_engine, "demo-it")
    with worker_db.session(DbContext(tenant_id=furn)) as s:
        furn_versions = list(s.execute(select(DocumentVersion.id)).scalars())
    with worker_db.session(DbContext(tenant_id=it)) as s:
        it_versions = list(s.execute(select(DocumentVersion.id)).scalars())
    # "Lumen" (the IT supplier) matches the IT tenant's chunks under its own context ...
    with worker_db.session(DbContext(tenant_id=it)) as s:
        assert search(s, it_versions, "Lumen", limit=20)
    # ... but tenant A asking for versions of BOTH tenants never sees (or ranks) them.
    with worker_db.session(DbContext(tenant_id=furn)) as s:
        assert search(s, furn_versions + it_versions, "Lumen", limit=20) == []
        own = search(s, furn_versions + it_versions, "Acme", limit=20)
    assert own and {h.version_id for h in own} <= {str(v) for v in furn_versions}


def test_artifact_keys_cannot_cross_tenants_or_escape_the_store(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path)
    a, b = uuid.uuid4(), uuid.uuid4()
    key, _ = store.put(a, b"secret synthetic bytes")
    with pytest.raises(ArtifactKeyError):
        store.get(b, key)
    for bad in ("../../etc/passwd", f"{a}/../../x", f"{a}/aa/{'0' * 64}/../x"):
        with pytest.raises(ArtifactKeyError):
            store.get(a, bad)


def test_worker_connection_reuse_does_not_carry_tenant_context(
    stack, migrator_engine: Engine
) -> None:  # type: ignore[no-untyped-def]
    st = stack
    investigated(st, "INV-9001")
    investigated(st, "INV-7002", tenant="demo-it", who="it-investigator")
    furn, it = tenant_id(migrator_engine, "demo-furniture"), tenant_id(migrator_engine, "demo-it")
    engine = make_engine("worker", database="opsbridge_test")
    engine.pool = QueuePool(engine.pool._creator, pool_size=1, max_overflow=0)  # type: ignore[attr-defined]
    db = Database(engine)
    seen = []
    for tid in (furn, it, None, furn):
        with db.session(DbContext(tenant_id=tid)) as s:
            pid = s.execute(text("SELECT pg_backend_pid()")).scalar_one()
            tenants = set(s.execute(text("SELECT DISTINCT tenant_id FROM cases")).scalars())
            seen.append((pid, tenants))
    assert len({pid for pid, _ in seen}) == 1
    assert [t for _, t in seen] == [{furn}, {it}, set(), {furn}]
    engine.dispose()


def test_retention_uses_the_database_clock_not_a_drifting_app_clock(
    stack, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    """Found when the Podman VM clock drifted hours behind the host: cut-offs mixed the
    application clock with database-written timestamps. Skew the application clock two days
    ahead; one-day retention must still delete nothing that is actually new."""
    import datetime as real

    from opsbridge.workflow import retention

    st = stack
    case_id = investigated(st, "INV-9001")
    vid = get(st, "furn-investigator", f"{T}/cases/{case_id}/documents").json()["items"][0][
        "versions"
    ][0]["version_id"]
    adm = st["actors"]["furn-admin"]
    assert (
        call(
            st["client"], adm, "PUT", f"{T}/settings", json={"retention": {"raw_documents_days": 1}}
        ).status_code
        == 200
    )

    class SkewedDatetime(real.datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[no-untyped-def,override]
            return real.datetime.now(tz) + real.timedelta(days=2)

    monkeypatch.setattr(retention, "datetime", SkewedDatetime, raising=False)
    assert call(st["client"], adm, "POST", f"{T}/retention/run").status_code == 202
    drain(st)
    assert (
        get(
            st, "furn-investigator", f"{T}/cases/{case_id}/documents/versions/{vid}/original"
        ).status_code
        == 200
    )
