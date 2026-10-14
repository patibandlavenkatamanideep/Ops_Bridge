"""Shared stack fixtures: real simulator HTTP server, API app, worker, seeded tenants."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, select

from opsbridge.api.app import create_app
from opsbridge.config import REPO_ROOT, Settings, get_settings
from opsbridge.persistence.db import Database, make_engine
from opsbridge.persistence.models import User
from opsbridge.seed import seed_demo
from opsbridge.simulator.app import create_simulator
from opsbridge.simulator.scenarios import load_corpus, seed_simulator
from opsbridge.workflow.investigation import run_investigation
from opsbridge.workflow.worker import Worker, build_investigation_deps

from .servers import serve
from .test_api_cases import call, login

MASTER = "test-simulator-master-secret"


@pytest.fixture(scope="module")
def simulator(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[str, Any]]:
    db_path = tmp_path_factory.mktemp("sim") / "sim.sqlite3"
    app = create_simulator(
        db_path, MASTER, fault_controls=True, production=False, slow_response_seconds=3
    )
    seed_simulator(app.state.store, load_corpus(REPO_ROOT / "fixtures" / "scenarios" / "dev.yaml"))
    with serve(app) as base:
        yield base, app.state.store


@pytest.fixture
def stack(
    simulator, tmp_path: Path, app_db: Database, worker_db: Database, migrator_engine: Engine
):  # type: ignore[no-untyped-def]
    base, store = simulator
    store.clear_faults()
    settings: Settings = get_settings().model_copy(
        update={
            "simulator_url": base,
            "sim_master_secret": SecretStr(MASTER),
            "artifact_dir": tmp_path / "artifacts",
            "rate_limit_per_minute": 10_000,
            "connector_timeout_seconds": 2.0,
        }
    )
    mdb = Database(make_engine("migrator", database="opsbridge_test"))
    seed_demo(mdb, settings)
    with mdb.session() as s:
        users = {u.username: u.id for u in s.execute(select(User)).scalars()}
    mdb.engine.dispose()
    actors = {name: login(app_db, settings, uid) for name, uid in users.items()}
    worker = Worker(db=worker_db, settings=settings)
    deps = build_investigation_deps(settings, worker_db)
    worker.register(
        "investigate",
        lambda job: run_investigation(deps, job.tenant_id, uuid.UUID(job.payload["run_id"])),
    )
    with TestClient(
        create_app(settings=settings, db=app_db), base_url="http://localhost:5173"
    ) as client:
        yield {
            "client": client,
            "worker": worker,
            "actors": actors,
            "settings": settings,
            "deps": deps,
            "store": store,
            "base": base,
        }


def open_case(st: dict[str, Any], who: str, tenant: str, invoice: str) -> str:
    r = call(
        st["client"],
        st["actors"][who],
        "POST",
        f"/api/v1/tenants/{tenant}/cases",
        json={"title": f"Invoice {invoice} blocked", "invoice_number": invoice},
    )
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


def investigate(
    st: dict[str, Any], who: str, tenant: str, case_id: str, *, drain: bool = True
) -> dict[str, Any]:
    r = call(
        st["client"],
        st["actors"][who],
        "POST",
        f"/api/v1/tenants/{tenant}/cases/{case_id}/investigations",
    )
    assert r.status_code in (200, 202), r.text
    run = r.json()
    if drain:
        while st["worker"].run_once():
            pass
    return dict(run)


def get(st: dict[str, Any], who: str, url: str) -> httpx.Response:
    return call(st["client"], st["actors"][who], "GET", url)
