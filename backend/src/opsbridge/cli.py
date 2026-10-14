"""OpsBridge command-line entry points (used by the Makefile and containers)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


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
    uvicorn.run(app, host=args.host, port=args.port, proxy_headers=False, log_level="info")
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

    args = parser.parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    sys.exit(main())
