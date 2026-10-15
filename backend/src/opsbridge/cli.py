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
    from opsbridge.telemetry.tracing import configure_tracing

    configure_tracing("opsbridge-api")
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


def _eval_freeze(args: argparse.Namespace) -> int:
    from opsbridge.config import REPO_ROOT
    from opsbridge.evaluation import manifest

    out = (
        Path(args.out) if args.out else REPO_ROOT / "evals" / "manifests" / f"{args.benchmark}.json"
    )
    if out.exists():
        print(f"{out} exists; a frozen benchmark is never re-frozen. Use a new benchmark id.")
        return 2
    body = manifest.freeze(
        REPO_ROOT, args.benchmark, modes=args.modes.split(","), repeats=args.repeats
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    sizes = ", ".join(f"{k}={v['n_cases']}" for k, v in body["splits"].items())
    print(f"froze {args.benchmark} ({sizes}) → {out}")
    return 0


def _eval_bench(args: argparse.Namespace) -> int:
    import os
    from datetime import UTC, datetime
    from typing import Any, cast

    from opsbridge.agents.adapters import MockModelAdapter
    from opsbridge.agents.contracts import ModelAdapter
    from opsbridge.agents.prompts import REGISTRY as PROMPTS
    from opsbridge.config import REPO_ROOT, get_settings
    from opsbridge.evaluation import manifest as mf
    from opsbridge.evaluation import metrics, receipt
    from opsbridge.evaluation.bench import environment
    from opsbridge.evaluation.cassette import RecordingAdapter, ReplayAdapter, ReplayFailure
    from opsbridge.evaluation.egress import EgressBlocked, loopback_only
    from opsbridge.simulator.scenarios import load_corpus

    os.chdir(REPO_ROOT)  # receipts and results use repository-relative paths only
    mpath = Path("evals/manifests") / f"{args.benchmark}.json"
    frozen = mf.load(mpath)
    problems = mf.verify(frozen, REPO_ROOT)
    if problems:
        print("Refusing to run: inputs differ from the frozen benchmark:")
        for problem in problems:
            print(f"  - {problem}")
        return 2
    split = frozen["splits"][args.split]
    corpus = load_corpus(Path(split["path"]))
    modes = args.modes.split(",") if args.modes else list(frozen["modes"])
    repeats = int(frozen["repeats"])  # fixed schedule from the frozen plan
    runs_root = Path(f"evals/runs/{args.benchmark}")
    recorded = sorted(runs_root.glob(f"{args.split}-record-*/model-cassette.json"))
    adapter: RecordingAdapter | ReplayAdapter
    if args.replay:
        if not recorded:
            print(f"No recorded run for split {args.split}; record it first.")
            return 2
        source = Path(args.from_run) / "model-cassette.json" if args.from_run else recorded[-1]
        adapter = ReplayAdapter.load(source)
        kind = "replay"
    else:
        if args.split == "heldout" and recorded:
            print("The held-out split was already recorded; the final comparison runs once.")
            print("Reproduce it offline with --replay (make eval-offline).")
            return 2
        adapter = RecordingAdapter(inner=MockModelAdapter())
        kind = "record"
    run_id = f"{args.split}-{kind}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    run_dir = runs_root / run_id
    # Record runs write their own cassette; replay runs bind the cassette they replayed.
    cassette = run_dir / "model-cassette.json" if kind == "record" else source
    run_dir.mkdir(parents=True, exist_ok=True)
    prompts = run_dir / "prompts.json"
    prompts.write_text(
        json.dumps(
            [
                {
                    "template_id": t.template_id,
                    "digest": t.digest,
                    "system": t.system,
                    "instructions": t.instructions,
                }
                for t in PROMPTS.values()
            ],
            indent=1,
        )
        + "\n",
        encoding="utf-8",
    )
    ledger = Path("evals/ledger/opsbridge-bench.jsonl")
    ledger.parent.mkdir(parents=True, exist_ok=True)
    key = REPO_ROOT / "var" / "evalseal" / "opsbridge-eval.key"
    public_key = receipt.ensure_key(key, Path("evals/keys/opsbridge-eval.pub"))
    bound = {
        "opsbridge_manifest": str(mpath),
        "opsbridge_acceptance_policy": "evals/acceptance-policy.json",
        "opsbridge_prompts": str(prompts),
        **{f"opsbridge_policy_{Path(p).stem}": p for p in frozen["files"] if "/policies/" in p},
    }
    summary: dict[str, Any] = {
        "schema": "opsbridge-eval-results/1",
        "benchmark_id": args.benchmark,
        "manifest_sha256": frozen["manifest_sha256"],
        "split": args.split,
        "run_id": run_id,
        "kind": kind,
        "response_origin": "mocked (deterministic mock model); no real model evaluated",
        "network": "Python-level egress limited to loopback during the run",
        "repeats": repeats,
        "code_version": mf.code_version(REPO_ROOT),
        "modes": {},
        "receipts": {},
    }
    mode_results: dict[str, dict[str, Any]] = {}
    receipts: dict[str, dict[str, Any]] = {}

    def progress(_t: dict[str, Any]) -> None:
        print(".", end="", flush=True)

    try:
        with (
            loopback_only(),
            environment(
                get_settings(),
                corpus,
                REPO_ROOT / "var" / "eval" / run_id,
                cast(ModelAdapter, adapter),
            ) as env,
        ):
            for mode in modes:
                print(f"{args.split} / {mode} / {repeats} repeats ", end="", flush=True)
                trials, record = receipt.run_mode(
                    env,
                    corpus.scenarios,
                    mode=mode,
                    repeats=repeats,
                    split=args.split,
                    corpus_rel=split["path"],
                    corpus_sha=split["sha256"],
                    run_dir=run_dir,
                    ledger=ledger,
                    key_path=key,
                    bound=bound,
                    adapter=adapter,
                    cassette_rel=str(cassette),
                    benchmark_id=args.benchmark,
                    on_trial=progress,
                )
                print()
                mode_results[mode] = metrics.summarize_mode(trials)
                receipts[mode] = {
                    "record_hash": record.hash,
                    "ledger": str(ledger),
                    "run_dir": str(run_dir),
                }
    except (ReplayFailure, EgressBlocked) as exc:
        print(f"\nFAILED CLOSED ({type(exc).__name__}): {exc}")
        return 3
    summary["modes"], summary["receipts"] = mode_results, receipts
    if "rules_only" in mode_results and "mock" in mode_results:
        summary["paired_rules_vs_mock"] = metrics.paired(
            mode_results["rules_only"], mode_results["mock"]
        )
    policy = json.loads(Path("evals/acceptance-policy.json").read_text(encoding="utf-8"))
    summary["acceptance"] = metrics.acceptance(summary, policy, args.split)
    summary["verification"] = receipt.verify(ledger, public_key)
    out = Path(f"evals/results/{args.benchmark}/{run_id}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    for mode, r in mode_results.items():
        na = r["next_action"]
        print(
            f"  {mode:<10} next action {na['correct']}/{na['n']} "
            f"(Wilson95 {na['wilson95']}), case {r['case']['correct']}/{r['case']['n']}, "
            f"safe misses {r['safe_misses']}, unsafe misses {r['unsafe_misses']}, "
            f"errors {r['trial_errors']}"
        )
    ok = bool(summary["verification"]["ok"])
    print(f"  receipts {'verified' if ok else 'FAILED VERIFICATION'}; results → {out}")
    return 0 if ok else 4


def _eval_verify(_args: argparse.Namespace) -> int:
    import os

    from opsbridge.config import REPO_ROOT
    from opsbridge.evaluation import receipt

    os.chdir(REPO_ROOT)
    ledger = Path("evals/ledger/opsbridge-bench.jsonl")
    report = receipt.verify(ledger, Path("evals/keys/opsbridge-eval.pub").read_text().strip())
    print(f"chain: {report['chain']['message']}")
    print(f"signatures: {report['signatures']['message']}")
    for r in report["artifacts"]:
        print(f"record {r['index']}: {'ok' if r['ok'] else 'FAILED'}")
        if not r["ok"]:
            print(r["output"])
    return 0 if report["ok"] else 1


def _probe(args: argparse.Namespace) -> int:
    """Container health probe: `http://…` must answer 200; `tcp://host:port` must accept.
    A subcommand (not `python -c`) because some engines re-split health-check arguments."""
    import socket
    import urllib.request
    from urllib.parse import urlparse

    target = urlparse(args.url)
    try:
        if target.scheme == "tcp":
            socket.create_connection((target.hostname, target.port), timeout=3).close()
            return 0
        if target.hostname not in ("127.0.0.1", "localhost") or target.scheme != "http":
            return 2  # probes are for the local process only
        with urllib.request.urlopen(args.url, timeout=3) as r:  # noqa: S310 - loopback http only
            return 0 if r.status == 200 else 1
    except OSError:
        return 1


def _init_keys(_args: argparse.Namespace) -> int:
    """Create the delegation signing key once, before services that share it start."""
    from opsbridge.config import get_settings
    from opsbridge.mcp_services.auth import load_or_create_key

    path = get_settings().delegation_key_path
    existed = path.exists()
    load_or_create_key(path)
    print(f"delegation key {'present' if existed else 'created'} at {path}")
    return 0


def _verify_restore(args: argparse.Namespace) -> int:
    from opsbridge.config import get_settings
    from opsbridge.operations.restore import verify_restored

    if args.database in ("opsbridge", "opsbridge_test", "opsbridge_eval"):
        print("Refusing: verify-restore runs against an isolated restored database only.")
        return 2
    artifact_dir = Path(args.artifact_dir)
    if not artifact_dir.is_absolute():
        # `uv --directory backend` changes the working directory; a relative path would
        # silently point elsewhere and report every artifact as missing.
        print("Refusing: --artifact-dir must be an absolute path.")
        return 2
    report = verify_restored(get_settings(), args.database, artifact_dir)
    print(json.dumps(report, indent=1, default=str))
    return 0 if report["ok"] else 1


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
    from opsbridge.telemetry.tracing import configure_tracing

    configure_tracing(f"opsbridge-mcp-{which}")
    settings = get_settings()
    # Tokens must carry the public issuer; keys may be fetched over the backchannel.
    backchannel = (settings.oidc_discovery_url or settings.oidc_issuer).rstrip("/")
    jwks_url = f"{backchannel}/protocol/openid-connect/certs"
    db = Database(make_engine("mcp"))
    if which == "records":
        aud, url = factory.RECORDS_AUDIENCE, settings.records_mcp_url
        token_verifier = JwksTokenVerifier(
            settings.oidc_issuer, aud, frozenset({"opsbridge-worker"}), jwks_url=jwks_url
        )
        server = build_records_server(
            db, token_verifier, factory.verifier(settings, aud), url, settings.oidc_issuer
        )
    else:
        aud, url = factory.CASEWORK_AUDIENCE, settings.casework_mcp_url
        token_verifier = JwksTokenVerifier(
            settings.oidc_issuer, aud, frozenset({"opsbridge-worker"}), jwks_url=jwks_url
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
    # DNS-rebinding protection stays on; the allowed Host values are this service's configured
    # address (a container name under Compose) and its loopback forms.
    from urllib.parse import urlparse

    from mcp.server.transport_security import TransportSecuritySettings

    parsed = urlparse(url)
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=sorted(
            {parsed.netloc, f"localhost:{parsed.port}", f"127.0.0.1:{parsed.port}"}
        ),
    )
    app = server.streamable_http_app(
        stateless_http=True, json_response=True, transport_security=security
    )
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

    p = sub.add_parser("eval-freeze", help="freeze benchmark inputs by digest (once per id)")
    p.add_argument("--benchmark", default="opsbridge-bench-v1")
    p.add_argument("--modes", default="rules_only,mock")
    p.add_argument("--repeats", type=int, default=3)
    p.add_argument("--out", default=None)
    p.set_defaults(func=_eval_freeze)

    p = sub.add_parser("eval-bench", help="run a frozen benchmark split through the stack")
    p.add_argument("--benchmark", default="opsbridge-bench-v1")
    p.add_argument("--split", required=True, choices=["dev", "validation", "heldout", "security"])
    p.add_argument("--modes", default=None, help="comma-separated; default: the frozen modes")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--record", action="store_true", help="record model calls (default)")
    mode.add_argument("--replay", action="store_true", help="strict offline replay")
    p.add_argument(
        "--from-run", default=None, help="record run directory to replay (default: latest)"
    )
    p.set_defaults(func=_eval_bench)

    p = sub.add_parser("eval-verify", help="verify benchmark receipts, signatures and artifacts")
    p.set_defaults(func=_eval_verify)

    p = sub.add_parser("probe", help="health probe for containers (http://127.0.0.1… or tcp://…)")
    p.add_argument("url")
    p.set_defaults(func=_probe)

    p = sub.add_parser("init-keys", help="create the delegation signing key if absent")
    p.set_defaults(func=_init_keys)

    p = sub.add_parser("verify-restore", help="application-level checks on a restored database")
    p.add_argument("--database", required=True)
    p.add_argument("--artifact-dir", required=True)
    p.set_defaults(func=_verify_restore)

    args = parser.parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    sys.exit(main())
