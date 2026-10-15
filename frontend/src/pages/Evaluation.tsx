import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { ApiError, api, tenantPath } from "../api/client";
import { EmptyState, ErrorState, Loading } from "../components/States";
import { statusLabel } from "../components/StatusBadge";

interface Rate {
  correct: number;
  n: number;
  rate: number | null;
  wilson95: [number, number] | null;
}
interface Gate {
  gate: string;
  mode: string;
  status: string;
  detail?: string;
  reason?: string;
}
interface RunSummary {
  benchmark_id: string;
  run_id: string;
  split: string;
  kind: string;
  repeats: number;
  code_commit: string | null;
  modes: Record<string, { next_action: Rate; case: Rate; safe_misses: number; unsafe_misses: number;
    trial_errors: number }>;
  acceptance: Gate[];
  receipts_verified: boolean;
}
interface PackCase {
  case_id: string;
  family: string;
  template: string;
  expected: { outcome: string; types: string[]; next_action: string };
  majority: { next_action: string | null; outcome: string | null; types: string[]; error: string | null };
  verdicts: string[];
  correct: { next_action: boolean; case: boolean };
  safe_miss: boolean;
  flip_rate: number;
}
interface ModeDetail {
  n_cases: number;
  n_trials: number;
  trial_errors: number;
  next_action: Rate;
  outcome: Rate;
  types: Rate;
  case: Rate;
  safe_misses: number;
  unsafe_misses: number;
  by_type: Record<string, { tp: number; fp: number; fn: number; precision: number | null; recall: number | null }>;
  instability: { definition: string; cases_with_any_flip: number; mean_flip_rate: number | null };
  agent: { explanation_origins: Record<string, number>; model_calls: number; model_calls_rejected: number;
    citations: number; citations_valid: number; tool_calls: number; cost_usd: string };
  latency_ms: { median: number | null; p95: number | null };
  pack_cases: PackCase[];
}
interface RunDetail {
  benchmark_id: string;
  run_id: string;
  split: string;
  kind: string;
  repeats: number;
  manifest_sha256: string;
  response_origin: string;
  network: string;
  code_version: { commit: string | null; uncommitted_changes: boolean | null };
  pack: string;
  modes: Record<string, ModeDetail>;
  paired_rules_vs_mock: { n_cases: number; both_correct: number; neither_correct: number; regressed: string[];
    improved: string[]; exact_mcnemar_p: number | null; note: string } | null;
  acceptance: Gate[];
  verification: { ok: boolean; chain: string | null; signatures: string | null };
  not_evaluated: string[];
}

const pct = (r: Rate) =>
  r.rate === null ? "—" : `${r.correct}/${r.n} (${(r.rate * 100).toFixed(1)}%${r.wilson95 ?
    `, 95% CI ${(r.wilson95[0] * 100).toFixed(1)}–${(r.wilson95[1] * 100).toFixed(1)}%` : ""})`;
const when = (runId: string) => {
  const m = /(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})/.exec(runId);
  return m ? `${m[1]}-${m[2]}-${m[3]} ${m[4]}:${m[5]} UTC` : runId;
};

export function Evaluation() {
  const { tenant = "", benchmark, runId } = useParams();
  if (benchmark && runId) return <RunView tenant={tenant} benchmark={benchmark} runId={runId} />;
  return <RunList tenant={tenant} />;
}

function RunList({ tenant }: { tenant: string }) {
  const q = useQuery<{ items: RunSummary[] }, ApiError>({
    queryKey: ["evaluations", tenant],
    queryFn: ({ signal }) => api(tenantPath(tenant, "/evaluations"), { signal }),
  });
  if (q.isPending) return <Loading label="Loading evaluation runs" />;
  if (q.isError) return <ErrorState error={q.error} onRetry={() => q.refetch()} />;
  return (
    <section aria-labelledby="eval-title">
      <div className="page-header">
        <h1 id="eval-title">Evaluation</h1>
      </div>
      <p className="muted small">
        Benchmark runs on the synthetic corpus. All model responses are from the deterministic mock (labelled mocked):
        these runs verify the engineering path and are not evidence of real-model quality.
      </p>
      {q.data.items.length === 0 ? (
        <EmptyState title="No benchmark runs yet">
          <p>Run <code>make eval-offline</code> or <code>opsbridge eval-bench</code>.</p>
        </EmptyState>
      ) : (
        <table className="table">
          <thead>
            <tr>
              <th scope="col">Run</th>
              <th scope="col">Split</th>
              <th scope="col">Rules only — next action</th>
              <th scope="col">Agent (mock) — next action</th>
              <th scope="col">Misses (safe / unsafe)</th>
              <th scope="col">Gate</th>
              <th scope="col">Receipts</th>
            </tr>
          </thead>
          <tbody>
            {q.data.items.map((r) => {
              const rules = r.modes.rules_only;
              const mock = r.modes.mock;
              const gates = r.acceptance.filter((g) => g.status !== "not_evaluated");
              return (
                <tr key={r.run_id}>
                  <th scope="row">
                    <Link to={`/t/${tenant}/evaluation/${r.benchmark_id}/${r.run_id}`}>
                      {when(r.run_id)} · {statusLabel(r.kind)}
                    </Link>
                  </th>
                  <td>{statusLabel(r.split)}</td>
                  <td className="small">{rules ? pct(rules.next_action) : "—"}</td>
                  <td className="small">{mock ? pct(mock.next_action) : "—"}</td>
                  <td>
                    {rules ? `${rules.safe_misses} / ${rules.unsafe_misses}` : "—"}
                    {mock ? ` · ${mock.safe_misses} / ${mock.unsafe_misses}` : ""}
                  </td>
                  <td className="small">
                    {gates.length ? gates.map((g) => `${g.mode}: ${g.detail || g.status}`).join("; ") : "not evaluated"}
                  </td>
                  <td>{r.receipts_verified ? "verified" : "NOT verified"}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </section>
  );
}

function RunView({ tenant, benchmark, runId }: { tenant: string; benchmark: string; runId: string }) {
  const q = useQuery<RunDetail, ApiError>({
    queryKey: ["evaluation", tenant, benchmark, runId],
    queryFn: ({ signal }) => api(tenantPath(tenant, `/evaluations/${benchmark}/${runId}`), { signal }),
  });
  if (q.isPending) return <Loading label="Loading evaluation run" />;
  if (q.isError) return <ErrorState error={q.error} onRetry={() => q.refetch()} />;
  const d = q.data;
  return (
    <article aria-labelledby="run-title">
      <p>
        <Link to={`/t/${tenant}/evaluation`}>← Evaluation runs</Link>
      </p>
      <div className="page-header">
        <h1 id="run-title">
          {statusLabel(d.split)} · {statusLabel(d.kind)} · {when(d.run_id)}
        </h1>
      </div>
      <dl className="facts">
        <dt>Benchmark</dt>
        <dd className="mono">{d.benchmark_id}</dd>
        <dt>Frozen manifest</dt>
        <dd className="mono" title={d.manifest_sha256}>{d.manifest_sha256.slice(0, 19)}…</dd>
        <dt>Responses</dt>
        <dd>{d.response_origin}</dd>
        <dt>Repeats</dt>
        <dd>{d.repeats} per case (verdicts aggregated by majority; repeats are not extra cases)</dd>
        <dt>Network</dt>
        <dd>{d.network}</dd>
        <dt>Code</dt>
        <dd className="mono">
          {d.code_version.commit?.slice(0, 10) ?? "unknown"}
          {d.code_version.uncommitted_changes ? " (uncommitted changes)" : ""}
        </dd>
        <dt>Receipts</dt>
        <dd>
          {d.verification.ok ? "verified" : "NOT verified"} when this run finished — {d.verification.chain};{" "}
          {d.verification.signatures} (re-check the whole ledger with <code>make eval-offline</code>){" "}
          <a href={tenantPath(tenant, `/evaluations/${benchmark}/${runId}/receipt`)}>Download EvalSeal records</a>
        </dd>
      </dl>
      <p className="muted small">
        A valid signature shows the sealed files are unchanged relative to the local signing key. It does not show
        when the run happened, that the inputs were true, or that no other runs were made.
      </p>

      {d.acceptance.length > 0 && (
        <section aria-labelledby="gate-title">
          <h2 id="gate-title">Acceptance</h2>
          <ul>
            {d.acceptance.map((g) => (
              <li key={`${g.gate}-${g.mode}`}>
                {g.gate} ({g.mode}): <strong>{g.detail || statusLabel(g.status)}</strong>
                {g.reason ? ` — ${g.reason}` : ""}
              </li>
            ))}
            {d.not_evaluated.map((n) => (
              <li key={n}>Not evaluated: {n}</li>
            ))}
          </ul>
        </section>
      )}

      {Object.entries(d.modes).map(([mode, m]) => (
        <section key={mode} aria-labelledby={`mode-${mode}`}>
          <h2 id={`mode-${mode}`}>{mode === "mock" ? "Agent-assisted (mock model)" : "Rules only"}</h2>
          <dl className="facts">
            <dt>Correct next action</dt>
            <dd>{pct(m.next_action)}</dd>
            <dt>Correct outcome / types / whole case</dt>
            <dd>
              {m.outcome.correct} / {m.types.correct} / {m.case.correct} of {m.n_cases}
            </dd>
            <dt>Misses</dt>
            <dd>
              {m.safe_misses} safe (escalated to a person) · {m.unsafe_misses} unsafe · {m.trial_errors} trial errors
            </dd>
            <dt>Instability</dt>
            <dd>
              {m.instability.cases_with_any_flip} case(s) with any flip; mean {m.instability.mean_flip_rate ?? "—"} (
              {m.instability.definition})
            </dd>
            <dt>Evidence</dt>
            <dd>
              {m.agent.citations_valid}/{m.agent.citations} citations valid · {m.agent.model_calls} model calls (
              {m.agent.model_calls_rejected} rejected) · {m.agent.tool_calls} tool calls · cost {m.agent.cost_usd}
            </dd>
            <dt>Latency per run</dt>
            <dd>
              median {m.latency_ms.median ?? "—"} ms · p95 {m.latency_ms.p95 ?? "—"} ms (local machine)
            </dd>
          </dl>
          <table className="table compact">
            <caption className="small muted">Discrepancy types over majority verdicts (all packs)</caption>
            <thead>
              <tr>
                <th scope="col">Type</th>
                <th scope="col">TP</th>
                <th scope="col">FP</th>
                <th scope="col">FN</th>
                <th scope="col">Precision</th>
                <th scope="col">Recall</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(m.by_type).map(([t, v]) => (
                <tr key={t}>
                  <th scope="row">{statusLabel(t)}</th>
                  <td>{v.tp}</td>
                  <td>{v.fp}</td>
                  <td>{v.fn}</td>
                  <td>{v.precision ?? "—"}</td>
                  <td>{v.recall ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <CaseTable cases={m.pack_cases} pack={d.pack} />
        </section>
      ))}

      {d.paired_rules_vs_mock && (
        <section aria-labelledby="paired-title">
          <h2 id="paired-title">Rules only vs agent-assisted (paired by case)</h2>
          <p>
            Both correct {d.paired_rules_vs_mock.both_correct}, neither {d.paired_rules_vs_mock.neither_correct}, agent
            worse on {d.paired_rules_vs_mock.regressed.length}, agent better on {d.paired_rules_vs_mock.improved.length}
            {d.paired_rules_vs_mock.exact_mcnemar_p !== null ? ` (exact McNemar p = ${d.paired_rules_vs_mock.exact_mcnemar_p})` : ""}.
          </p>
          {d.paired_rules_vs_mock.note && <p className="muted small">{d.paired_rules_vs_mock.note}</p>}
        </section>
      )}
    </article>
  );
}

function CaseTable({ cases, pack }: { cases: PackCase[]; pack: string }) {
  if (cases.length === 0) return <p className="muted">No cases from the {pack} pack in this split.</p>;
  return (
    <table className="table compact">
      <caption className="small muted">Cases from your organization’s pack ({pack}); other packs are summarized only</caption>
      <thead>
        <tr>
          <th scope="col">Case</th>
          <th scope="col">Family</th>
          <th scope="col">Expected next action</th>
          <th scope="col">Verdicts per repeat</th>
          <th scope="col">Result</th>
        </tr>
      </thead>
      <tbody>
        {cases.map((c) => (
          <tr key={c.case_id}>
            <th scope="row" className="mono small">{c.case_id}</th>
            <td className="small">
              {c.family} <span className="muted">({c.template})</span>
            </td>
            <td className="small mono">{c.expected.next_action}</td>
            <td className="small mono">{c.verdicts.map((v) => (v === c.expected.next_action ? "✓" : "✗")).join(" ")}</td>
            <td className="small">
              {c.correct.next_action ? "correct" : c.safe_miss ? `safe miss → ${c.majority.next_action}` :
                c.majority.error ? `error: ${c.majority.error}` : `wrong → ${c.majority.next_action}`}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
