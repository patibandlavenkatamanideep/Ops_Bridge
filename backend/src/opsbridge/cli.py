"""OpsBridge command-line entry points (used by the Makefile and containers)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from opsbridge.telemetry.logging import uvicorn_log_config


def _export_openapi(args: argparse.Namespace) -> int:
    from opsbridge.api.app import create_app

    spec = create_app().openapi()
    Path(args.out).write_text(json.dumps(spec, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {args.out} ({len(spec['paths'])} paths)")
    return 0


def _seed_demo(_args: argparse.Namespace) -> int:
    from opsbridge.config import get_settings
    from opsbridge.persistence.db import Database, make_engine
    from opsbridge.seed import seed_demo

    db = Database(make_engine("migrator"))
    report = seed_demo(db, get_settings())
    db.engine.dispose()
    print(
        f"tenants created={report.tenants_created} existing={report.tenants_existing}; "
        f"memberships={report.memberships_upserted}; policies created={report.policies_created}"
    )
    return 0


def _api(args: argparse.Namespace) -> int:
    import uvicorn

    from opsbridge.api.app import create_app
    from opsbridge.config import REPO_ROOT

    static = Path(args.static) if args.static else REPO_ROOT / "frontend" / "dist"
    app = create_app(static_dir=static if args.serve_frontend else None)
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        proxy_headers=False,
        log_level="info",
        log_config=uvicorn_log_config("info"),
    )
    return 0


def _worker(_args: argparse.Namespace) -> int:
    from opsbridge.workflow.worker import main as worker_main

    worker_main()
    return 0


def _simulator(args: argparse.Namespace) -> int:
    import uvicorn

    from opsbridge.simulator.app import app_from_env

    uvicorn.run(
        app_from_env(),
        host=args.host,
        port=args.port,
        log_level="warning",
        log_config=uvicorn_log_config("warning"),
    )
    return 0


def _simulator_seed(args: argparse.Namespace) -> int:
    import os

    from opsbridge.config import REPO_ROOT
    from opsbridge.simulator.scenarios import load_corpus, seed_simulator
    from opsbridge.simulator.store import SimStore

    db = Path(os.environ.get("OPSBRIDGE_SIM_DB", str(REPO_ROOT / "var" / "simulator.sqlite3")))
    store = SimStore(db)
    total = 0
    for path in args.corpus or sorted((REPO_ROOT / "fixtures" / "scenarios").glob("*.yaml")):
        total += seed_simulator(store, load_corpus(Path(path)))
    print(f"simulator records written: {total} ({db})")
    return 0


def _eval_rules(args: argparse.Namespace) -> int:
    import platform
    from collections import Counter
    from datetime import UTC, datetime

    from opsbridge import __version__
    from opsbridge.config import REPO_ROOT
    from opsbridge.evaluation.rules_baseline import PolicyBook, evaluate
    from opsbridge.simulator.scenarios import load_corpus

    corpus_path = Path(args.corpus)
    corpus = load_corpus(corpus_path)
    book = PolicyBook(REPO_ROOT / "packs")
    verdicts = [evaluate(s, book) for s in corpus.scenarios]
    n = len(verdicts)
    summary = {
        "cases": n,
        "case_correct": sum(v.case_correct for v in verdicts),
        "outcome_correct": sum(v.outcome_correct for v in verdicts),
        "types_correct": sum(v.types_correct for v in verdicts),
        "action_correct": sum(v.action_correct for v in verdicts),
        "by_split": dict(Counter(v.split for v in verdicts)),
    }
    result = {
        "schema_version": 1,
        "mode": "rules_only",
        "evidence_level": "engineering verification on synthetic data (no model, no live services)",
        "corpus_id": corpus.corpus_id,
        "corpus_file": str(corpus_path.relative_to(REPO_ROOT))
        if corpus_path.is_relative_to(REPO_ROOT)
        else str(corpus_path),
        "unit": "case (one invoice); labels hand-written; single deterministic run per case",
        "generated_at": datetime.now(UTC).isoformat(),
        "code_version": __version__,
        "python": platform.python_version(),
        "summary": summary,
        "verdicts": [v.to_dict() for v in verdicts],
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(
        f"rules-only baseline: {summary['case_correct']}/{n} case-correct "
        f"(outcome {summary['outcome_correct']}, types {summary['types_correct']}, action {summary['action_correct']})"
    )
    for v in verdicts:
        if not v.case_correct:
            print(f"  MISS {v.case_id}: got {v.outcome}/{v.types}/{v.next_action}")
    print(f"wrote {out}")
    return 0


def _mcp(args: argparse.Namespace, which: str) -> int:
    import uuid

    import uvicorn

    from opsbridge.config import get_settings
    from opsbridge.connectors.tasks import SimTaskConnector, TaskConnector
    from opsbridge.mcp_services import factory
    from opsbridge.mcp_services.auth import JwksTokenVerifier
    from opsbridge.mcp_services.casework import build_casework_server
    from opsbridge.mcp_services.records import build_records_server
    from opsbridge.persistence.db import Database, DbContext, make_engine
    from opsbridge.persistence.models import Tenant

    settings = get_settings()
    db = Database(make_engine("mcp"))
    if which == "records":
        aud, url = factory.RECORDS_AUDIENCE, settings.records_mcp_url
        token_verifier = JwksTokenVerifier(
            settings.oidc_issuer, aud, frozenset({"opsbridge-worker"})
        )
        server = build_records_server(
            db, token_verifier, factory.verifier(settings, aud), url, settings.oidc_issuer
        )
    else:
        aud, url = factory.CASEWORK_AUDIENCE, settings.casework_mcp_url
        token_verifier = JwksTokenVerifier(
            settings.oidc_issuer, aud, frozenset({"opsbridge-worker"})
        )

        def connector_for(tenant_id: uuid.UUID, name: str) -> TaskConnector:
            with db.session(DbContext(tenant_id=tenant_id)) as s:
                tenant = s.get(Tenant, tenant_id)
                assert tenant is not None
                org = tenant.slug
            if name == "github_issues":
                from opsbridge.connectors.github import github_connector_from_settings

                return github_connector_from_settings(settings)
            return SimTaskConnector(
                settings.simulator_url, settings.sim_master_secret.get_secret_value(), org
            )

        server = build_casework_server(
            db,
            token_verifier,
            factory.verifier(settings, aud),
            url,
            settings.oidc_issuer,
            connector_for,
            timeout_seconds=settings.connector_timeout_seconds,
        )
    app = server.streamable_http_app(stateless_http=True, json_response=True)
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level="warning",
        log_config=uvicorn_log_config("warning"),
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="opsbridge")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("export-openapi", help="write the OpenAPI document")
    p.add_argument("--out", required=True)
    p.set_defaults(func=_export_openapi)

    p = sub.add_parser("seed-demo", help="idempotently seed the designated synthetic demo tenants")
    p.set_defaults(func=_seed_demo)

    p = sub.add_parser("api", help="run the HTTP API")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--serve-frontend", action="store_true", help="also serve the built UI")
    p.add_argument("--static", default=None)
    p.set_defaults(func=_api)

    p = sub.add_parser("worker", help="run the background worker")
    p.set_defaults(func=_worker)

    p = sub.add_parser("simulator", help="run the purchasing-system simulator")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8090)
    p.set_defaults(func=_simulator)

    p = sub.add_parser("simulator-seed", help="load scenario corpora into the simulator store")
    p.add_argument("corpus", nargs="*")
    p.set_defaults(func=_simulator_seed)

    for which, port in (("records", 8101), ("casework", 8102)):
        p = sub.add_parser(f"mcp-{which}", help=f"run the {which} MCP service (streamable HTTP)")
        p.add_argument("--host", default="127.0.0.1")
        p.add_argument("--port", type=int, default=port)
        p.set_defaults(func=lambda a, w=which: _mcp(a, w))

    p = sub.add_parser("eval-rules", help="run the rules-only baseline on a scenario corpus")
    p.add_argument("--corpus", default="../fixtures/scenarios/dev.yaml")
    p.add_argument("--out", default="../evals/results/rules-baseline-dev.json")
    p.set_defaults(func=_eval_rules)

    args = parser.parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    sys.exit(main())
