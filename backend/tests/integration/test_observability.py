"""Telemetry contract (Phase 8; OB-19, OB-20): correlated traces with safe identifiers,
bounded metric labels, aggregate state without weakening RLS, worker health endpoints."""

from __future__ import annotations

import re

import httpx
import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from prometheus_client import REGISTRY
from sqlalchemy import text

from opsbridge.persistence.db import Database, make_engine
from opsbridge.telemetry import metrics
from opsbridge.workflow.worker import Worker, serve_health

from .servers import free_port
from .test_approvals_execution import approve, drain, operations, prepared

pytestmark = pytest.mark.integration
EXPORTER = InMemorySpanExporter()
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


@pytest.fixture(scope="module", autouse=True)
def tracing_provider() -> None:
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(EXPORTER))
    trace.set_tracer_provider(provider)  # once per process; module tracers are proxies


def sample(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def by_trace(spans: list[ReadableSpan], trace_id: int) -> list[ReadableSpan]:
    return [s for s in spans if s.context.trace_id == trace_id]


def test_one_trace_from_request_through_worker_steps_and_connector(stack) -> None:  # type: ignore[no-untyped-def]
    st = stack
    EXPORTER.clear()
    jobs_before = sample(
        "opsbridge_jobs_processed_total", kind="dispatch_operation", result="completed"
    )
    case_id, prop = prepared(st, "INV-9001")
    spans = list(EXPORTER.get_finished_spans())
    req = next(
        s for s in spans if s.name == "POST /api/v1/tenants/{tenant}/cases/{case_id}/investigations"
    )
    attrs = dict(req.attributes or {})
    assert attrs["http.response.status_code"] in (200, 202) and attrs["opsbridge.correlation_id"]
    assert attrs["opsbridge.tenant_id"] and UUID.fullmatch(str(attrs["opsbridge.tenant_id"]))
    job = next(s for s in spans if s.name == "job investigate")
    assert job.context.trace_id == req.context.trace_id  # trace continued via the job payload
    steps = [s for s in by_trace(spans, req.context.trace_id) if s.name.startswith("step ")]
    assert {s.name for s in steps} >= {
        "step collect_context",
        "step reconcile_records",
        "step explain_findings",
    }
    assert all(dict(s.attributes or {})["opsbridge.case_id"] == case_id for s in steps)
    assert all(s.parent and s.parent.span_id == job.context.span_id for s in steps)

    EXPORTER.clear()
    assert approve(st, case_id, prop).status_code == 200
    drain(st)
    [op] = operations(st, case_id)
    spans = list(EXPORTER.get_finished_spans())
    decision = next(s for s in spans if s.name.endswith("/proposals/{proposal_id}/decision"))
    connector = [
        s for s in by_trace(spans, decision.context.trace_id) if s.name.startswith("connector ")
    ]
    create = next(
        s for s in connector if dict(s.attributes or {})["opsbridge.connector.phase"] == "dispatch"
    )
    a = dict(create.attributes or {})
    assert a["opsbridge.operation_id"] == op["id"] and a["opsbridge.case_id"] == case_id
    assert (
        a["opsbridge.attempt.status"] == "succeeded"
        and a["opsbridge.remote_ref"] == op["remote_ref"]
    )
    assert UUID.fullmatch(str(a["opsbridge.attempt_id"]))
    assert (
        sample("opsbridge_jobs_processed_total", kind="dispatch_operation", result="completed")
        == jobs_before + 1
    )

    # Spans carry identifiers and statuses, never document or message content.
    everything = " ".join(
        str(v) for s in EXPORTER.get_finished_spans() for v in (s.attributes or {}).values()
    )
    for content in ("Task chair", "CH-100", "185.00", "receiving"):
        assert content not in everything


def test_metrics_have_bounded_labels_and_business_series(stack) -> None:  # type: ignore[no-untyped-def]
    st = stack
    waits = sample("opsbridge_approval_wait_seconds_count", decision="approve")
    confirmed = sample(
        "opsbridge_operations_finished_total", status="confirmed", connector="sim_tasks"
    )
    case_id, prop = prepared(st, "INV-9001")
    assert approve(st, case_id, prop).status_code == 200
    drain(st)
    assert sample("opsbridge_approval_wait_seconds_count", decision="approve") == waits + 1
    assert (
        sample("opsbridge_operations_finished_total", status="confirmed", connector="sim_tasks")
        == confirmed + 1
    )
    assert sample("opsbridge_connector_attempts_total", phase="dispatch", result="succeeded") >= 1
    assert (
        sample(
            "opsbridge_investigation_duration_seconds_count",
            mode="rules_only",
            outcome="discrepancies_found",
        )
        >= 1
    )
    body = st["client"].get("/metrics").text
    assert 'route="/api/v1/tenants/{tenant}/cases/{case_id}/investigations"' in body
    labels = " ".join(re.findall(r"\{([^}]*)\}", body))
    assert not UUID.search(labels), "a metric label carries an identifier"
    assert "demo-furniture" not in labels and "INV-9001" not in labels


def test_aggregate_state_reports_backlog_while_rls_still_hides_rows(stack, migrator_engine) -> None:  # type: ignore[no-untyped-def]
    st = stack
    case_id, prop = prepared(st, "INV-9001")  # one proposal awaiting approval
    worker_engine = make_engine("worker", database="opsbridge_test")
    try:
        with worker_engine.connect() as conn:
            assert (
                conn.execute(text("SELECT count(*) FROM proposals")).scalar_one() == 0
            )  # RLS intact
            snap = dict(
                conn.execute(text("SELECT metric, value FROM ops.metrics_snapshot()")).all()
            )
            with pytest.raises(Exception, match="permission denied"):
                conn.execute(text("SET ROLE opsbridge_ops_reader"))
        assert snap["approval_backlog"] == 1 and snap["approval_backlog_oldest_age_seconds"] >= 0
        metrics.watch_state(worker_engine)
        body = metrics.render().decode()
        assert 'opsbridge_state{metric="approval_backlog"} 1.0' in body
        assert "opsbridge_state_scrape_ok 1.0" in body
        assert 'opsbridge_queue_jobs{kind="investigate",status="succeeded"}' in body
    finally:
        metrics.PROCESS.state = None
        worker_engine.dispose()
    with migrator_engine.connect() as conn:
        login = conn.execute(
            text(
                "SELECT rolcanlogin, rolbypassrls FROM pg_roles WHERE rolname = 'opsbridge_ops_reader'"
            )
        ).one()
    assert tuple(login) == (False, False)


def test_worker_health_endpoints(stack, worker_db: Database) -> None:  # type: ignore[no-untyped-def]
    worker = Worker(db=worker_db, settings=stack["settings"])
    port = free_port()
    server = serve_health(worker, "127.0.0.1", port)
    base = f"http://127.0.0.1:{port}"
    try:
        assert httpx.get(f"{base}/healthz").status_code == 200
        ready = httpx.get(f"{base}/readyz")
        assert ready.status_code == 200 and ready.json()["checks"] == {
            "loop": "ok",
            "database": "ok",
        }
        assert "opsbridge_jobs_processed" in httpx.get(f"{base}/metrics").text
        worker.last_loop -= 10_000  # a wedged loop is not live
        assert httpx.get(f"{base}/healthz").status_code == 503
        assert httpx.get(f"{base}/readyz").json()["checks"]["loop"] == "stale"
    finally:
        server.shutdown()


def test_metrics_endpoint_can_require_a_token(stack) -> None:  # type: ignore[no-untyped-def]
    from fastapi.testclient import TestClient
    from pydantic import SecretStr

    from opsbridge.api.app import create_app

    settings = stack["settings"].model_copy(
        update={"metrics_token": SecretStr("synthetic-scrape-token")}
    )
    app = create_app(settings=settings, db=stack["client"].app.state.opsbridge.db)
    with TestClient(app) as client:
        assert client.get("/metrics").status_code == 401
        assert client.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 401
        ok = client.get("/metrics", headers={"Authorization": "Bearer synthetic-scrape-token"})
        assert ok.status_code == 200 and "opsbridge_http_requests_total" in ok.text


def test_incoming_trace_context_is_not_trusted(stack) -> None:  # type: ignore[no-untyped-def]
    EXPORTER.clear()
    forged = "4bf92f3577b34da6a3ce929d0e0e4736"
    stack["client"].get("/api/v1/me", headers={"traceparent": f"00-{forged}-00f067aa0ba902b7-01"})
    server = next(s for s in EXPORTER.get_finished_spans() if s.name == "GET /api/v1/me")
    assert format(server.context.trace_id, "032x") != forged and server.parent is None
