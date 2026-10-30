"""Stack-level benchmark: every trial goes through the product path.

Simulator HTTP on loopback (seeded only from the split's frozen corpus) → case creation and
investigation start through the services → worker workflow (ingestion into PostgreSQL,
matching, optional agent steps) → the stored investigation record. The database is
`opsbridge_eval`, truncated at the start of each benchmark run. Modes run on the same cases
and inputs; repeats re-run the investigation on the same case.
"""

from __future__ import annotations

import secrets
import time
import uuid
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from pydantic import SecretStr
from sqlalchemy import select, text

from opsbridge.agents.contracts import ModelAdapter
from opsbridge.api.deps import resolve_principal
from opsbridge.auth.permissions import Principal
from opsbridge.auth.sessions import SessionInfo
from opsbridge.canonical import digest
from opsbridge.config import Settings
from opsbridge.persistence.db import Database, DbContext, make_engine
from opsbridge.persistence.models import Base, ModelCall, ToolInvocation, User, WorkflowRun
from opsbridge.seed import seed_demo
from opsbridge.services.cases import CaseCreate, create_case
from opsbridge.services.investigations import latest_investigation, start_investigation
from opsbridge.simulator.app import create_simulator
from opsbridge.simulator.scenarios import Corpus, Scenario, seed_simulator
from opsbridge.workflow.investigation import WORKFLOW_VERSION, run_investigation
from opsbridge.workflow.jobs import ClaimedJob
from opsbridge.workflow.worker import Worker, build_investigation_deps

from .local_server import serve

EVAL_DB = "opsbridge_eval"
DISCREPANCY_TYPES = ("missing_receipt", "quantity_mismatch", "price_mismatch")
ESCALATE = "escalate:assign_case"
INVESTIGATOR = {"furniture": "furn-investigator", "it_hardware": "it-investigator"}
TENANT = {"furniture": "demo-furniture", "it_hardware": "demo-it"}


@dataclass
class TrialResult:
    case_id: str
    split: str
    pack: str
    family: str
    template: str
    mode: str
    repeat: int
    expected: dict[str, Any]
    outcome: str | None = None
    types: list[str] = field(default_factory=list)
    next_action: str | None = None
    finding_codes: list[str] = field(default_factory=list)
    explanation_origin: str | None = None
    response_mode: str | None = None
    run_status: str | None = None
    model_calls: int = 0
    model_calls_rejected: int = 0
    model_input_tokens: int = 0
    model_output_tokens: int = 0
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    citations: int = 0
    citations_valid: int = 0
    proposal_validation: dict[str, Any] | None = None
    latency_ms: int = 0
    error: str | None = None
    # Behaviour beyond the next action, for replay comparison 2 (reproduce.py): digests of the
    # findings (values and evidence) and of the citations, without the ids a run generates.
    findings_sha256: str | None = None
    citations_sha256: str | None = None

    @property
    def correct(self) -> dict[str, bool]:
        e = self.expected
        action = self.next_action == e["next_action"]
        outcome = self.outcome == e["outcome"]
        types = sorted(self.types) == sorted(e["types"])
        return {
            "next_action": action,
            "outcome": outcome,
            "types": types,
            "case": action and outcome and types,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "correct": self.correct}


def _truncate(engine: Any) -> None:
    tables = ", ".join(t.name for t in reversed(Base.metadata.sorted_tables))
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))


class BenchEnvironment:
    """One isolated benchmark environment (simulator + eval database + worker).

    Every trial runs the investigation workflow `workflow_version`: a frozen plan's own (see
    manifest.workflow_version_of), so recordings replay with the behaviour that made them."""

    def __init__(
        self,
        settings: Settings,
        corpus: Corpus,
        workdir: Path,
        model_adapter: ModelAdapter | None,
        *,
        workflow_version: str = WORKFLOW_VERSION,
    ) -> None:
        self.base_settings = settings
        self.corpus = corpus
        self.workdir = workdir
        self.model_adapter = model_adapter
        self.workflow_version = workflow_version
        self.cases: dict[str, uuid.UUID] = {}
        self._stack = ExitStack()

    def __enter__(self) -> BenchEnvironment:
        self.workdir.mkdir(parents=True, exist_ok=True)
        master = secrets.token_hex(16)
        sim = create_simulator(
            self.workdir / "simulator.sqlite3", master, fault_controls=False, production=False
        )
        seed_simulator(sim.state.store, self.corpus)
        base = self._stack.enter_context(serve(sim))
        self.settings = self.base_settings.model_copy(
            update={
                "simulator_url": base,
                "sim_master_secret": SecretStr(master),
                "artifact_dir": self.workdir / "artifacts",
                "tool_transport": "inprocess",
                "rate_limit_per_minute": 1_000_000,
                "quotas_enabled": False,  # 240 runs per split would exceed tier quotas
            }
        )
        migrator = make_engine("migrator", self.settings, database=EVAL_DB)
        try:
            _truncate(migrator)
            mdb = Database(migrator)
            seed_demo(mdb, self.settings)
            with mdb.session() as s:
                users = {
                    u.username: (u.id, u.display_name)
                    for u in s.execute(
                        select(User).where(User.username.in_(INVESTIGATOR.values()))
                    ).scalars()
                }
        finally:
            migrator.dispose()
        self.app_db = Database(make_engine("app", self.settings, database=EVAL_DB))
        self.worker_db = Database(make_engine("worker", self.settings, database=EVAL_DB))
        self._stack.callback(self.app_db.engine.dispose)
        self._stack.callback(self.worker_db.engine.dispose)
        deps = build_investigation_deps(self.settings, self.worker_db)
        if self.model_adapter is not None:
            deps.adapter_overrides = {"mock": self.model_adapter}
        if hasattr(self.model_adapter, "call_tool"):  # a cassette also records/replays tools
            deps.tool_cassette = self.model_adapter
        self.worker = Worker(db=self.worker_db, settings=self.settings)

        def investigate(job: ClaimedJob) -> None:
            run_investigation(deps, job.tenant_id, uuid.UUID(job.payload["run_id"]))

        self.worker.register("investigate", investigate)
        self.principals = {
            pack: self._principal(user, *users[user], TENANT[pack])
            for pack, user in INVESTIGATOR.items()
        }
        return self

    def __exit__(self, *exc: object) -> None:
        self._stack.close()

    def _principal(
        self, username: str, user_id: uuid.UUID, display_name: str, tenant: str
    ) -> Principal:
        # Same membership resolution as an authenticated request (server-side roles and tier).
        info = SimpleNamespace(user_id=user_id, username=username, display_name=display_name)
        p = resolve_principal(self.app_db, cast(SessionInfo, info), tenant)
        if p is None:
            raise RuntimeError(f"{username} is not a member of {tenant}")
        return p

    def _case_for(self, s: Scenario, p: Principal) -> uuid.UUID:
        if s.id not in self.cases:
            case, _ = create_case(
                self.app_db,
                p,
                CaseCreate(
                    title=s.case_title,
                    # Neutral text: the requester does not know the answer.
                    description=f"Invoice {s.invoice.number} is blocked in accounts payable.",
                    invoice_number=s.invoice.number,
                    po_number=None,
                ),
                idempotency_key=None,
            )
            self.cases[s.id] = case.id
        return self.cases[s.id]

    def run_trial(self, s: Scenario, mode: str, repeat: int) -> TrialResult:
        t = TrialResult(
            s.id, s.split, s.pack, s.family, s.template, mode, repeat, s.expected.model_dump()
        )
        adapter = self.model_adapter
        if adapter is not None and hasattr(adapter, "start"):
            adapter.start(s.id, mode, repeat)
        failures_before = len(getattr(adapter, "failures", []))
        p = self.principals[s.pack]
        started = time.monotonic()
        try:
            case_id = self._case_for(s, p)
            run, _ = start_investigation(
                self.app_db,
                p,
                case_id,
                mode,
                correlation_id=None,
                enforce_quota=self.settings.quotas_enabled,
                workflow_version=self.workflow_version,
            )
            while self.worker.run_once():
                if len(getattr(adapter, "failures", [])) > failures_before:
                    break  # replay failed: stop; never retry into a different path
        except Exception as exc:  # recorded as a failed trial; the run continues
            t.error = f"{type(exc).__name__}: {exc}"[:500]
            t.latency_ms = int((time.monotonic() - started) * 1000)
            return t
        t.latency_ms = int((time.monotonic() - started) * 1000)
        replay_failures = getattr(adapter, "failures", [])[failures_before:]
        if replay_failures:
            t.error = "ReplayFailure: " + replay_failures[0]
        with self.worker_db.session(DbContext(tenant_id=p.tenant_id)) as sess:
            wr = sess.get(WorkflowRun, run.id)
            assert wr is not None
            t.run_status, t.response_mode = wr.status, wr.response_mode
            if wr.status != "completed" and not t.error:
                t.error = f"run {wr.status}: {wr.error or 'not completed'}"[:500]
            calls = list(
                sess.execute(select(ModelCall).where(ModelCall.run_id == run.id)).scalars()
            )
            t.model_calls = len(calls)
            t.model_calls_rejected = sum(1 for c in calls if c.outcome != "ok")
            t.model_input_tokens = sum(c.input_tokens or 0 for c in calls)
            t.model_output_tokens = sum(c.output_tokens or 0 for c in calls)
            t.tool_calls = [
                {
                    "seq": ti.seq,
                    "tool": ti.tool_name,
                    "version": ti.tool_version,
                    "input_sha256": ti.input_digest,
                    "response_sha256": ti.response_digest,
                    "outcome": ti.outcome,
                    "origin": ti.origin,  # live, or recorded when replay served it
                }
                for ti in sess.execute(
                    select(ToolInvocation)
                    .where(ToolInvocation.run_id == run.id)
                    .order_by(ToolInvocation.seq)
                ).scalars()
            ]
        if t.run_status != "completed":
            return t
        view = latest_investigation(self.app_db, p, case_id)
        if view is None or view.version.run_id != run.id:
            t.error = t.error or "investigation record for this run not found"
            return t
        iv = view.version
        rec = iv.recommendation or {}
        t.outcome = iv.outcome
        t.types = sorted(
            {f["finding_type"] for f in iv.findings if f["finding_type"] in DISCREPANCY_TYPES}
        )
        t.next_action = (
            f"{rec['decision']}:{rec['action_type']}" if rec.get("action_type") else "none"
        )
        t.finding_codes = [f["reason_code"] for f in iv.findings]
        t.explanation_origin = iv.explanation_origin
        t.citations = len(view.citations)
        t.citations_valid = sum(1 for c in view.citations if c.get("status") == "valid")
        t.findings_sha256 = behaviour_digest(iv.findings)
        t.citations_sha256 = behaviour_digest(view.citations)
        draft = rec.get("proposal_draft") or {}
        t.proposal_validation = draft.get("validation") if draft else None
        return t


_RUN_IDS = frozenset({"document_id", "version_id"})


def behaviour_digest(items: list[dict[str, Any]]) -> str:
    """Digest of findings or citations as behaviour: the document and version ids a run
    generates are left out; values, statuses, locators and the cited content's own SHA-256
    stay, so a different value or a different source changes the digest."""

    def strip(value: Any) -> Any:
        if isinstance(value, dict):
            return {k: strip(v) for k, v in value.items() if k not in _RUN_IDS}
        if isinstance(value, list):
            return [strip(v) for v in value]
        return value

    return digest([strip(i) for i in items])


@contextmanager
def environment(
    settings: Settings,
    corpus: Corpus,
    workdir: Path,
    model_adapter: ModelAdapter | None,
    *,
    workflow_version: str = WORKFLOW_VERSION,
) -> Iterator[BenchEnvironment]:
    with BenchEnvironment(
        settings, corpus, workdir, model_adapter, workflow_version=workflow_version
    ) as env:
        yield env
