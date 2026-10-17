"""Benchmark results (read-only): runs, run detail, sealed receipts."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Response

from opsbridge.services import evaluations as svc

from ..deps import CurrentPrincipal, State
from ..errors import not_found

router = APIRouter(prefix="/api/v1/tenants/{tenant}/evaluations", tags=["evaluations"])


@router.get("")
def list_runs(principal: CurrentPrincipal, state: State) -> dict[str, Any]:
    return {"items": svc.list_runs(state.settings.evals_dir, principal)}


@router.get("/{benchmark}/{run_id}")
def run_detail(
    benchmark: str, run_id: str, principal: CurrentPrincipal, state: State
) -> dict[str, Any]:
    try:
        return svc.run_detail(state.settings.evals_dir, principal, benchmark, run_id)
    except svc.EvalNotFound as exc:
        raise not_found("evaluation run") from exc


@router.get("/{benchmark}/{run_id}/receipt")
def receipt(benchmark: str, run_id: str, principal: CurrentPrincipal, state: State) -> Response:
    try:
        records = svc.receipt_records(state.settings.evals_dir, principal, benchmark, run_id)
    except svc.EvalNotFound as exc:
        raise not_found("evaluation run") from exc
    return Response(
        content=json.dumps(records, indent=1),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{run_id}-evalseal-records.json"'},
    )
