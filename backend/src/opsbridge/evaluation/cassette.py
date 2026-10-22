"""Model and tool cassettes: record once, replay strictly.

A model-cassette entry is keyed by (case, mode, repeat, seq) — seq is the call's position
within that trial — and carries the canonical digest of the request that produced it. Replay
returns the recorded response only when the key exists *and* the request digest matches;
otherwise it raises `ReplayFailure`, which is deliberately not a `ModelError`: the
workflow must not treat it as an ordinary model failure and fall back to a template, and
nothing ever substitutes a live call. Responses keep their original origin label
(`mocked` when recorded from the mock adapter, `recorded` when captured from a live model).

Agent tool calls are recorded the same way in `tool-cassette.json` beside the model
cassette, keyed by (case, mode, repeat, tool seq) and checked against the tool name, version
and argument digest. Replay serves the recorded response and never executes the tool; a
recorded denial or error is raised again as recorded. The caller's `rebind` maps a recorded
response onto the replay environment (database ids differ between runs); if it cannot, the
replay fails. A recording without a tool cassette has no recorded tool calls to serve.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn

from opsbridge.agents.contracts import Capabilities, ModelAdapter, ModelRequest, ModelResponse
from opsbridge.agents.contracts import ReplayFailure as ReplayFailure  # re-exported
from opsbridge.canonical import digest

SCHEMA = "opsbridge-model-cassette/1"
TOOL_SCHEMA = "opsbridge-tool-cassette/1"
TOOL_CASSETTE = "tool-cassette.json"  # beside the model cassette

Key = tuple[str, str, int, int]


class RecordedToolFailure(RuntimeError):
    """A tool call that was denied or failed when recorded, raised again in replay."""

    origin = "recorded"

    def __init__(self, message: str, denied: bool) -> None:
        super().__init__(message)
        self.denied = denied


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
    tool_seq: int = 0


@dataclass
class _Base:
    trial: Trial | None = None
    failures: list[str] = field(default_factory=list)

    def start(self, case_id: str, mode: str, repeat: int) -> None:
        self.trial = Trial(case_id, mode, repeat)

    def _next_key(self) -> Key:
        if self.trial is None:
            raise ReplayFailure("model call outside a benchmark trial")
        self.trial.seq += 1
        t = self.trial
        return (t.case_id, t.mode, t.repeat, t.seq)

    def _next_tool_key(self) -> Key:
        if self.trial is None:
            raise ReplayFailure("tool call outside a benchmark trial")
        self.trial.tool_seq += 1
        t = self.trial
        return (t.case_id, t.mode, t.repeat, t.tool_seq)

    def _fail(self, msg: str) -> NoReturn:
        self.failures.append(msg)  # the harness also sees failures a caller swallowed
        raise ReplayFailure(msg)


@dataclass
class RecordingAdapter(_Base):
    """Wraps a real adapter and records every call it serves."""

    inner: ModelAdapter | None = None
    entries: list[dict[str, Any]] = field(default_factory=list)
    tool_entries: list[dict[str, Any]] = field(default_factory=list)
    tool_response_mode: str = "live"

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

    def call_tool(
        self,
        tool: str,
        version: str,
        args: dict[str, Any],
        execute: Callable[[], Any],
        rebind: Callable[[Any], Any],
    ) -> tuple[Any, Any, str]:
        """Execute the tool and record what it returned or how it failed. Returns (response,
        usable response, origin); `rebind` applies only in replay."""
        case_id, mode, repeat, seq = self._next_tool_key()
        entry = {
            "case_id": case_id,
            "mode": mode,
            "repeat": repeat,
            "seq": seq,
            "tool": tool,
            "version": version,
            "args": args,
            "args_sha256": digest(args),
        }
        try:
            response = execute()
        except Exception as exc:
            denied = type(exc).__name__ == "McpDenied"
            outcome = "denied" if denied else "error"
            self.tool_entries.append({**entry, "outcome": outcome, "error": str(exc)[:300]})
            raise
        self.tool_entries.append(
            {**entry, "outcome": "ok", "response": response, "response_sha256": digest(response)}
        )
        return response, response, "live"

    def save(self, path: Path, *, benchmark_id: str, split: str) -> None:
        origins = sorted({e["response"]["origin"] for e in self.entries})
        body = {
            "schema": SCHEMA,
            "benchmark_id": benchmark_id,
            "split": split,
            "response_origins": origins,
            "entries": sorted(self.entries, key=_order),
        }
        tools = {
            "schema": TOOL_SCHEMA,
            "benchmark_id": benchmark_id,
            "split": split,
            "response_mode": self.tool_response_mode,
            "entries": sorted(self.tool_entries, key=_order),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(body, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        (path.parent / TOOL_CASSETTE).write_text(
            json.dumps(tools, indent=1, sort_keys=True) + "\n", encoding="utf-8"
        )


def _order(e: dict[str, Any]) -> Key:
    return (e["case_id"], e["mode"], e["repeat"], e["seq"])


def load_tool_entries(path: Path) -> dict[Key, dict[str, Any]]:
    """Index a tool cassette, refusing one whose responses do not match their digests."""
    body = json.loads(path.read_text(encoding="utf-8"))
    if body.get("schema") != TOOL_SCHEMA:
        raise ReplayFailure(f"{path} is not a tool cassette ({body.get('schema')!r})")
    index: dict[Key, dict[str, Any]] = {}
    for e in body["entries"]:
        key = _order(e)
        if key in index:
            raise ReplayFailure(f"{path} records tool call {key} twice")
        if e["outcome"] == "ok" and digest(e["response"]) != e["response_sha256"]:
            raise ReplayFailure(
                f"{path}: the recorded response for {key} does not match its digest"
            )
        index[key] = e
    return index


@dataclass
class ReplayAdapter(_Base):
    """Serves only recorded responses; anything unmatched fails the run."""

    path: Path | None = None
    index: dict[Key, dict[str, Any]] = field(default_factory=dict)
    origins: list[str] = field(default_factory=list)
    tool_index: dict[Key, dict[str, Any]] = field(default_factory=dict)
    tool_cassette: Path | None = None
    tool_response_mode: str = "recorded"
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
        index = {_order(e): e for e in body["entries"]}
        tools = path.parent / TOOL_CASSETTE
        return cls(
            path=path,
            index=index,
            origins=list(body.get("response_origins", [])),
            tool_index=load_tool_entries(tools) if tools.exists() else {},
            tool_cassette=tools if tools.exists() else None,
        )

    @property
    def response_mode(self) -> str:
        """Run label: replayed mock output stays 'mocked'; replayed live output is 'recorded'."""
        return "mocked" if self.origins == ["mocked"] else "recorded"

    def generate(self, request: ModelRequest) -> ModelResponse:
        key = self._next_key()
        entry = self.index.get(key)
        if entry is None:
            self._fail(
                f"no recorded response for case={key[0]} mode={key[1]} repeat={key[2]} call={key[3]}"
            )
        if entry["request_sha256"] != request_digest(request):
            self._fail(
                f"request diverged from the recording for case={key[0]} mode={key[1]} "
                f"repeat={key[2]} call={key[3]} ({entry['template_id']})"
            )
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

    def call_tool(
        self,
        tool: str,
        version: str,
        args: dict[str, Any],
        execute: Callable[[], Any],
        rebind: Callable[[Any], Any],
    ) -> tuple[Any, Any, str]:
        """Serve the recorded response; the tool is never executed. Returns (recorded
        response, response rebound to this environment, "recorded")."""
        del execute
        key = self._next_tool_key()
        where = f"case={key[0]} mode={key[1]} repeat={key[2]} tool call={key[3]}"
        entry = self.tool_index.get(key)
        if entry is None:
            self._fail(f"no recorded tool response for {where} ({tool})")
        if (entry["tool"], entry["version"]) != (tool, version):
            self._fail(
                f"tool call diverged from the recording for {where}: recorded "
                f"{entry['tool']} v{entry['version']}, now {tool} v{version}"
            )
        if entry["args_sha256"] != digest(args):
            self._fail(f"tool call diverged from the recording for {where}: other arguments")
        if entry["outcome"] != "ok":
            raise RecordedToolFailure(
                entry.get("error") or entry["outcome"], denied=entry["outcome"] == "denied"
            )
        try:
            usable = rebind(entry["response"])
        except LookupError as exc:
            self._fail(f"recorded tool response for {where} does not fit this environment: {exc}")
        return entry["response"], usable, "recorded"


def backfill_tool_cassette(run_dir: Path, artifact_root: Path) -> Path:
    """Write the tool cassette of a run recorded before tool cassettes existed, from the
    tool-request and tool-response artifacts that run kept. Each entry must match the
    argument and response digests in the run's own trial records (bound in its sealed
    receipt); a call whose arguments or response cannot be found by digest stops the
    backfill, and an existing tool cassette is never overwritten."""
    out = run_dir / TOOL_CASSETTE
    if out.exists():
        raise FileExistsError(f"{out} already exists")
    calls = [
        (t, c)
        for log in sorted(run_dir.glob("trials-*.jsonl"))
        for t in map(json.loads, log.read_text(encoding="utf-8").splitlines())
        for c in t["tool_calls"]
    ]
    args_by: dict[str, Any] = {}
    responses_by: dict[str, Any] = {}
    for f in sorted(p for p in artifact_root.rglob("*") if p.is_file()):
        try:
            value = json.loads(f.read_bytes())
            responses_by[digest(value)] = value
        except (ValueError, TypeError):
            continue  # documents, and JSON that no canonical digest covers (floats)
        if isinstance(value, dict) and set(value) == {"tool", "args"}:
            args_by[digest(value["args"])] = value["args"]
    entries = []
    for t, c in calls:
        where = f"case={t['case_id']} mode={t['mode']} repeat={t['repeat']} tool call={c['seq']}"
        if c["input_sha256"] not in args_by:
            raise ReplayFailure(f"no kept request artifact matches {where}")
        entry = {
            "case_id": t["case_id"],
            "mode": t["mode"],
            "repeat": t["repeat"],
            "seq": c["seq"],
            "tool": c["tool"],
            "version": c["version"],
            "args": args_by[c["input_sha256"]],
            "args_sha256": c["input_sha256"],
            "outcome": c["outcome"],
        }
        if c["outcome"] == "ok":
            if c["response_sha256"] not in responses_by:
                raise ReplayFailure(f"no kept response artifact matches {where}")
            entry["response"] = responses_by[c["response_sha256"]]
            entry["response_sha256"] = c["response_sha256"]
        else:
            entry["error"] = "message not kept (recorded before tool cassettes)"
        entries.append(entry)
    manifest = json.loads((run_dir / "model-cassette.json").read_text(encoding="utf-8"))
    body = {
        "schema": TOOL_SCHEMA,
        "benchmark_id": manifest["benchmark_id"],
        "split": manifest["split"],
        "response_mode": "live",
        "backfilled": "from the tool artifacts this run kept; every entry matches the digests "
        "in its trials-*.jsonl",
        "entries": sorted(entries, key=_order),
    }
    out.write_text(json.dumps(body, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return out
