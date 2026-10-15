"""Model-call cassettes: record once, replay strictly.

A cassette entry is keyed by (case, mode, repeat, seq) — seq is the call's position within
that trial — and carries the canonical digest of the request that produced it. Replay
returns the recorded response only when the key exists *and* the request digest matches;
otherwise it raises `ReplayFailure`, which is deliberately not a `ModelError`: the
workflow must not treat it as an ordinary model failure and fall back to a template, and
nothing ever substitutes a live call. Responses keep their original origin label
(`mocked` when recorded from the mock adapter, `recorded` when captured from a live model).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from opsbridge.agents.contracts import Capabilities, ModelAdapter, ModelRequest, ModelResponse
from opsbridge.canonical import digest

SCHEMA = "opsbridge-model-cassette/1"


class ReplayFailure(RuntimeError):
    """No recorded response for this call, or the request diverged from the recording."""


def request_digest(req: ModelRequest) -> str:
    return digest(
        {
            "template_id": req.template_id,
            "template_digest": req.template_digest,
            "system": req.system,
            "user": req.user,
            "output_schema": req.output_schema,
            "max_output_tokens": req.max_output_tokens,
            "params": req.params,
        }
    )


@dataclass
class Trial:
    case_id: str
    mode: str
    repeat: int
    seq: int = 0


@dataclass
class _Base:
    trial: Trial | None = None
    failures: list[str] = field(default_factory=list)

    def start(self, case_id: str, mode: str, repeat: int) -> None:
        self.trial = Trial(case_id, mode, repeat)

    def _next_key(self) -> tuple[str, str, int, int]:
        if self.trial is None:
            raise ReplayFailure("model call outside a benchmark trial")
        self.trial.seq += 1
        t = self.trial
        return (t.case_id, t.mode, t.repeat, t.seq)


@dataclass
class RecordingAdapter(_Base):
    """Wraps a real adapter and records every call it serves."""

    inner: ModelAdapter | None = None
    entries: list[dict[str, Any]] = field(default_factory=list)

    @property
    def name(self) -> str:
        assert self.inner is not None
        return self.inner.name

    @property
    def capabilities(self) -> Capabilities:
        assert self.inner is not None
        return self.inner.capabilities

    def generate(self, request: ModelRequest) -> ModelResponse:
        assert self.inner is not None
        case_id, mode, repeat, seq = self._next_key()
        resp = self.inner.generate(request)
        origin = "mocked" if resp.origin == "mocked" else "recorded"
        self.entries.append(
            {
                "case_id": case_id,
                "mode": mode,
                "repeat": repeat,
                "seq": seq,
                "template_id": request.template_id,
                "request_sha256": request_digest(request),
                "response": {
                    "text": resp.text,
                    "provider": resp.provider,
                    "requested_model": resp.requested_model,
                    "resolved_model": resp.resolved_model,
                    "origin": origin,
                    "input_tokens": resp.input_tokens,
                    "output_tokens": resp.output_tokens,
                    "latency_ms": resp.latency_ms,
                    "stop_reason": resp.stop_reason,
                    "effective_params": resp.effective_params,
                },
            }
        )
        return resp

    def save(self, path: Path, *, benchmark_id: str, split: str) -> None:
        origins = sorted({e["response"]["origin"] for e in self.entries})
        body = {
            "schema": SCHEMA,
            "benchmark_id": benchmark_id,
            "split": split,
            "response_origins": origins,
            "entries": sorted(
                self.entries, key=lambda e: (e["case_id"], e["mode"], e["repeat"], e["seq"])
            ),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(body, indent=1, sort_keys=True) + "\n", encoding="utf-8")


@dataclass
class ReplayAdapter(_Base):
    """Serves only recorded responses; anything unmatched fails the run."""

    path: Path | None = None
    index: dict[tuple[str, str, int, int], dict[str, Any]] = field(default_factory=dict)
    origins: list[str] = field(default_factory=list)
    name: str = "replay"
    capabilities: Capabilities = field(
        default_factory=lambda: Capabilities(
            provider="replay",
            structured_output="native_schema",
            supported_params=frozenset(),
            hosted=False,
            local=True,
            reports_token_usage=True,
            reports_resolved_model=True,
            notes="Strict replay of recorded responses; never calls a model.",
        )
    )

    @classmethod
    def load(cls, path: Path) -> ReplayAdapter:
        body = json.loads(path.read_text(encoding="utf-8"))
        if body.get("schema") != SCHEMA:
            raise ReplayFailure(f"{path} is not a model cassette ({body.get('schema')!r})")
        index = {(e["case_id"], e["mode"], e["repeat"], e["seq"]): e for e in body["entries"]}
        return cls(path=path, index=index, origins=list(body.get("response_origins", [])))

    @property
    def response_mode(self) -> str:
        """Run label: replayed mock output stays 'mocked'; replayed live output is 'recorded'."""
        return "mocked" if self.origins == ["mocked"] else "recorded"

    def generate(self, request: ModelRequest) -> ModelResponse:
        key = self._next_key()
        entry = self.index.get(key)
        if entry is None:
            msg = f"no recorded response for case={key[0]} mode={key[1]} repeat={key[2]} call={key[3]}"
            self.failures.append(msg)
            raise ReplayFailure(msg)
        if entry["request_sha256"] != request_digest(request):
            msg = (
                f"request diverged from the recording for case={key[0]} mode={key[1]} "
                f"repeat={key[2]} call={key[3]} ({entry['template_id']})"
            )
            self.failures.append(msg)
            raise ReplayFailure(msg)
        r = entry["response"]
        return ModelResponse(
            text=r["text"],
            provider=r["provider"],
            requested_model=r["requested_model"],
            resolved_model=r["resolved_model"],
            origin=r["origin"],
            input_tokens=r["input_tokens"],
            output_tokens=r["output_tokens"],
            latency_ms=0,
            stop_reason=r["stop_reason"],
            effective_params=r["effective_params"],
            endpoint=f"replay:{self.path.name if self.path else 'cassette'}",
            sdk_version=None,
        )
