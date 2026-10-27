"""OpsBridge command-line entry points (used by the Makefile and containers)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

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
        f"memberships created={report.memberships_created} kept as they are="
        f"{report.memberships_kept}; policies created={report.policies_created}"
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


def _eval_canary(args: argparse.Namespace) -> int:
    from opsbridge.config import REPO_ROOT
    from opsbridge.evaluation import canary

    path = REPO_ROOT / "evals" / "baselines" / "canary-rules.json"
    current = canary.run(REPO_ROOT)
    if args.update_baseline:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(current, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        print(f"baseline written: {len(current['cases'])} cases → {path.relative_to(REPO_ROOT)}")
        return 0
    if not path.exists():
        print(
            f"No pinned baseline at {path}; create it with --update-baseline in a reviewed commit."
        )
        return 2
    diffs = canary.compare(canary.load(path), current)
    agree = sum(1 for c in current["cases"].values() if c["matches_label"])
    print(
        f"canary: {len(current['cases'])} cases, {agree} match their labels, {len(diffs)} change(s) vs baseline"
    )
    for d in diffs:
        print(f"  CHANGED {d}")
    return 1 if diffs else 0


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
    from typing import cast

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
                mode_results[mode] = metrics.summarize_mode(
                    trials, scheme=mf.scheme_of(frozen["scoring"]) or "", repeats=repeats
                )
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
    if kind == "replay":
        from opsbridge.evaluation.reproduce import differences, recorded_results

        results_dir = Path(f"evals/results/{args.benchmark}")
        diffs = differences(recorded_results(results_dir, source), mode_results)
        summary["reproduces"] = {
            "recorded_run": source.parent.name,
            "ok": not diffs,
            "differences": diffs[:100],
        }
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
    if not ok:
        return 4
    if kind == "replay":
        diffs = summary["reproduces"]["differences"]
        if diffs:
            print(f"  REPLAY DIFFERS from {summary['reproduces']['recorded_run']} ({len(diffs)}):")
            for d in diffs[:20]:
                print(f"    - {d}")
            return 5
        print(f"  replay reproduces {summary['reproduces']['recorded_run']} case by case")
    return 0


def _eval_replay_check(args: argparse.Namespace) -> int:
    """Replay each split's latest recorded run under the egress guard and compare verdicts
    case by case. No receipts are sealed and nothing under evals/ is written, so it can run
    in CI and in `make verify` without the signing key."""
    import os
    from datetime import UTC, datetime
    from typing import cast

    from opsbridge.agents.contracts import ModelAdapter
    from opsbridge.config import REPO_ROOT, get_settings
    from opsbridge.evaluation import manifest as mf
    from opsbridge.evaluation import metrics
    from opsbridge.evaluation.bench import environment
    from opsbridge.evaluation.cassette import ReplayAdapter, ReplayFailure
    from opsbridge.evaluation.egress import EgressBlocked, loopback_only
    from opsbridge.evaluation.reproduce import differences, recorded_results
    from opsbridge.simulator.scenarios import load_corpus

    os.chdir(REPO_ROOT)
    frozen = mf.load(Path("evals/manifests") / f"{args.benchmark}.json")
    problems = mf.verify(frozen, REPO_ROOT)
    if problems:
        print("Refusing: inputs differ from the frozen benchmark:")
        for problem in problems:
            print(f"  - {problem}")
        return 2
    failed = False
    for split_name in args.split or ["heldout", "security"]:
        recorded = sorted(
            Path(f"evals/runs/{args.benchmark}").glob(f"{split_name}-record-*/model-cassette.json")
        )
        if not recorded:
            print(f"{split_name}: no recorded run to replay")
            failed = True
            continue
        cassette = recorded[-1]
        expected = recorded_results(Path(f"evals/results/{args.benchmark}"), cassette)
        corpus = load_corpus(Path(frozen["splits"][split_name]["path"]))
        repeats = int(frozen["repeats"])
        adapter = ReplayAdapter.load(cassette)
        check_id = f"check-{split_name}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
        replayed: dict[str, Any] = {}
        try:
            with (
                loopback_only(),
                environment(
                    get_settings(),
                    corpus,
                    REPO_ROOT / "var" / "eval" / check_id,
                    cast(ModelAdapter, adapter),
                ) as env,
            ):
                for mode in [m for m in frozen["modes"] if m in expected["modes"]]:
                    trials = []
                    for scenario in corpus.scenarios:
                        for repeat in range(repeats):
                            t = env.run_trial(scenario, mode, repeat).to_dict()
                            if (t["error"] or "").startswith("ReplayFailure"):
                                raise ReplayFailure(t["error"])  # fail closed, as recording does
                            trials.append(t)
                    replayed[mode] = metrics.summarize_mode(
                        trials, scheme=mf.scheme_of(frozen["scoring"]) or "", repeats=repeats
                    )
        except (ReplayFailure, EgressBlocked) as exc:
            print(f"{split_name}: FAILED CLOSED ({type(exc).__name__}): {exc}")
            failed = True
            continue
        diffs = differences(expected, replayed)
        counts = ", ".join(
            f"{m} {r['next_action']['correct']}/{r['next_action']['n']}"
            for m, r in replayed.items()
        )
        if diffs:
            failed = True
            print(
                f"{split_name}: replay DIFFERS from {cassette.parent.name} ({len(diffs)}): {counts}"
            )
            for d in diffs[:20]:
                print(f"  - {d}")
        else:
            print(f"{split_name}: reproduces {cassette.parent.name} case by case ({counts})")
    return 1 if failed else 0


def _eval_backfill_tools(args: argparse.Namespace) -> int:
    import os

    from opsbridge.config import REPO_ROOT
    from opsbridge.evaluation.cassette import backfill_tool_cassette

    os.chdir(REPO_ROOT)
    run = Path(args.run)
    out = backfill_tool_cassette(run, Path("var/eval") / run.name / "artifacts")
    entries = json.loads(out.read_text(encoding="utf-8"))["entries"]
    print(f"{out}: {len(entries)} tool call(s), each matching the run's recorded digests")
    return 0


def _eval_rescore(args: argparse.Namespace) -> int:
    """Disagreement in a recorded run's trials under another scoring scheme. A derived
    reading for comparison only: it prints, writes nothing, and changes no recorded result."""
    import os

    from opsbridge.config import REPO_ROOT
    from opsbridge.evaluation import manifest as mf
    from opsbridge.evaluation import metrics

    os.chdir(REPO_ROOT)
    run = Path(args.run)
    frozen = mf.load(Path("evals/manifests") / f"{run.parent.name}.json")
    print(
        f"{run.name}: recorded under {mf.scheme_of(frozen['scoring'])}, read under {args.scheme} "
        "(derived; not the run's result)"
    )
    for path in sorted(run.glob("trials-*.jsonl")):
        trials = [
            json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line
        ]
        s = metrics.summarize_mode(trials, scheme=args.scheme, repeats=int(frozen["repeats"]))
        mode = path.stem.removeprefix("trials-")
        if args.scheme == metrics.SCORING_2:
            i = s["instability"]
            axes = ", ".join(
                f"{axis} {i[axis]['cases_with_disagreement']}/{s['n_cases']}"
                for axis in ("action", "correctness", "outcome_types")
            )
            done = "complete" if s["complete"] else f"incomplete: {s['incomplete_cases']}"
            print(
                f"  {mode}: cases with disagreement: {axes}; errored repeats "
                f"{i['errors']['trials_with_error']}; next action {s['next_action']['correct']}/"
                f"{s['n_cases']}; {done}"
            )
        else:
            print(f"  {mode}: cases with any flip {s['instability']['cases_with_any_flip']}")
    return 0


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

    settings = get_settings()
    if settings.delegation_private_key and settings.delegation_private_key.get_secret_value():
        print("delegation key provided from the secret store; nothing to create")
        return 0
    path = settings.delegation_key_path
    existed = path.exists()
    load_or_create_key(path)
    print(f"delegation key {'present' if existed else 'created'} at {path}")
    return 0


def _verify_restore(args: argparse.Namespace) -> int:
    from opsbridge.config import get_settings
    from opsbridge.evidence.storage import ArtifactStore, LocalArtifactStore, S3ArtifactStore
    from opsbridge.operations.restore import verify_restored

    if args.database in ("opsbridge", "opsbridge_test", "opsbridge_eval"):
        print("Refusing: verify-restore runs against an isolated restored database only.")
        return 2
    settings = get_settings()
    store: ArtifactStore
    if args.artifact_bucket:
        # the restored bucket; endpoint, region and prefix come from the S3 settings
        store = S3ArtifactStore(
            args.artifact_bucket,
            prefix=settings.artifact_s3_prefix,
            endpoint_url=settings.artifact_s3_endpoint_url,
            region=settings.artifact_s3_region,
        )
    else:
        artifact_dir = Path(args.artifact_dir)
        if not artifact_dir.is_absolute():
            # `uv --directory backend` changes the working directory; a relative path would
            # silently point elsewhere and report every artifact as missing.
            print("Refusing: --artifact-dir must be an absolute path.")
            return 2
        store = LocalArtifactStore(artifact_dir)
    report = verify_restored(settings, args.database, store)
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

        def connector_for(tenant_id: uuid.UUID, destination: dict[str, Any]) -> TaskConnector:
            """A connector for exactly the approved destination. The worker sends GitHub
            issues itself, with the tenant's connection; this service executes simulator
            tasks only, in the tenant's own organization (it never falls back to
            deployment-wide settings)."""
            with db.session(DbContext(tenant_id=tenant_id)) as s:
                tenant = s.get(Tenant, tenant_id)
                assert tenant is not None
                org = tenant.slug
            if destination.get("connector") != "sim_tasks":
                raise ValueError(
                    f"this service does not execute {destination.get('connector')!r} destinations"
                )
            if destination.get("organization") != org:
                raise ValueError("the approved organization is not this tenant's")
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

    p = sub.add_parser("eval-canary", help="offline regression check against the pinned baseline")
    p.add_argument("--update-baseline", action="store_true")
    p.set_defaults(func=_eval_canary)

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

    p = sub.add_parser(
        "eval-replay-check",
        help="replay recorded runs offline and compare verdicts; seals and writes nothing tracked",
    )
    p.add_argument("--benchmark", default="opsbridge-bench-v1")
    p.add_argument("--split", action="append", choices=["dev", "validation", "heldout", "security"])
    p.set_defaults(func=_eval_replay_check)

    p = sub.add_parser(
        "eval-backfill-tools",
        help="write a pre-tool-cassette run's tool cassette from its kept, digest-checked artifacts",
    )
    p.add_argument("--run", required=True, help="evals/runs/<benchmark>/<split>-record-<time>")
    p.set_defaults(func=_eval_backfill_tools)

    p = sub.add_parser(
        "eval-rescore",
        help="print a recorded run's disagreement under another scoring scheme (writes nothing)",
    )
    p.add_argument("--run", required=True, help="evals/runs/<benchmark>/<split>-<kind>-<time>")
    p.add_argument("--scheme", default="opsbridge-scoring/2")
    p.set_defaults(func=_eval_rescore)

    p = sub.add_parser("eval-verify", help="verify benchmark receipts, signatures and artifacts")
    p.set_defaults(func=_eval_verify)

    p = sub.add_parser("probe", help="health probe for containers (http://127.0.0.1… or tcp://…)")
    p.add_argument("url")
    p.set_defaults(func=_probe)

    p = sub.add_parser("init-keys", help="create the delegation signing key if absent")
    p.set_defaults(func=_init_keys)

    p = sub.add_parser("verify-restore", help="application-level checks on a restored database")
    p.add_argument("--database", required=True)
    where = p.add_mutually_exclusive_group(required=True)
    where.add_argument("--artifact-dir", help="restored local artifact directory (absolute)")
    where.add_argument("--artifact-bucket", help="restored S3 bucket (endpoint from settings)")
    p.set_defaults(func=_verify_restore)

    args = parser.parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    sys.exit(main())
