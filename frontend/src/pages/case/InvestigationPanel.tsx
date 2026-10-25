import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { ApiError, api, tenantPath, type Citation, type Finding, type Investigation, type Run } from "../../api/client";
import { EmptyState, ErrorState, Loading } from "../../components/States";
import { statusLabel } from "../../components/StatusBadge";
import { useSession } from "../../session";

const MODE_LABEL: Record<string, string> = {
  rules_only: "Rules only (no model)",
  mock: "Deterministic mock model (mocked)",
  anthropic: "Hosted model (live, requires opt-in)",
  ollama: "Local model (Ollama)",
};

const FINDING_LABEL: Record<string, string> = {
  quantity_mismatch: "Quantity mismatch",
  price_mismatch: "Price mismatch",
  missing_receipt: "Missing receipt",
  source_conflict: "Source conflict",
  manual_review: "Needs review",
  note: "Note",
};

export function InvestigationPanel({ tenant, caseId, onCite }: { tenant: string; caseId: string;
  onCite: (c: Citation) => void }) {
  const { can } = useSession();
  const qc = useQueryClient();
  const [runId, setRunId] = useState<string | null>(null);
  const inv = useQuery<Investigation, ApiError>({
    queryKey: ["investigation", tenant, caseId],
    queryFn: ({ signal }) => api<Investigation>(tenantPath(tenant, `/cases/${caseId}/investigation`), { signal }),
    retry: false,
  });
  const run = useQuery<Run, ApiError>({
    queryKey: ["run", tenant, caseId, runId],
    queryFn: ({ signal }) => api<Run>(tenantPath(tenant, `/cases/${caseId}/runs/${runId}`), { signal }),
    enabled: runId !== null,
    refetchInterval: (q) => (q.state.data && ["completed", "failed"].includes(q.state.data.status) ? false : 1000),
  });
  const { membership } = useSession();
  const modes = membership(tenant)?.model_modes ?? ["rules_only"];
  const [mode, setMode] = useState<string>("");
  const start = useMutation<Run, ApiError>({
    mutationFn: () => api<Run>(tenantPath(tenant, `/cases/${caseId}/investigations`), {
      method: "POST",
      body: mode ? { model_mode: mode } : {},
    }),
    onSuccess: (r) => setRunId(r.run_id),
  });
  const refreshedFor = useRef<string | null>(null);
  const finished = run.data?.status === "completed" ? run.data.run_id : null;
  useEffect(() => {
    if (finished && refreshedFor.current !== finished) {
      refreshedFor.current = finished;
      qc.invalidateQueries({ queryKey: ["investigation", tenant, caseId] });
      qc.invalidateQueries({ queryKey: ["case", tenant, caseId] });
      qc.invalidateQueries({ queryKey: ["documents", tenant, caseId] });
    }
  }, [finished, qc, tenant, caseId]);

  const running = runId !== null && !["completed", "failed"].includes(run.data?.status ?? "");
  const controls = can(tenant, "investigation.run") && (
    <div className="actions">
      <button type="button" className="button primary" onClick={() => start.mutate()} disabled={running || start.isPending}>
        {running ? "Investigating…" : inv.data ? "Re-run investigation" : "Run investigation"}
      </button>
      <label className="small">
        Model{" "}
        <select value={mode} onChange={(e) => setMode(e.target.value)}>
          <option value="">Organization default</option>
          {modes.map((m) => (
            <option key={m} value={m}>
              {MODE_LABEL[m] ?? m}
            </option>
          ))}
        </select>
      </label>
    </div>
  );

  return (
    <section aria-labelledby="inv-title">
      <h2 id="inv-title">Investigation</h2>
      {controls}
      {start.isError && <ErrorState error={start.error} />}
      {runId && run.data && run.data.status !== "completed" && (
        <div className="state" role="status" aria-live="polite">
          <strong>Run {run.data.status}</strong>
          <ol className="steps">
            {(run.data.steps ?? []).map((s) => (
              <li key={String(s.name)} className={`step step-${String(s.status)}`}>
                {String(s.name).replaceAll("_", " ")}: {String(s.status)}
                {s.error ? <span className="muted small"> — {String(s.error)}</span> : null}
              </li>
            ))}
          </ol>
          {run.data.status === "failed" && <p>The run failed. You can retry; nothing was concluded from the failure.</p>}
          {run.data.status === "running" && (run.data.steps ?? []).some((s) => s.status === "failed") && (
            <p className="muted small">A dependency failed; the worker will retry automatically.</p>
          )}
        </div>
      )}
      {inv.isPending ? (
        <Loading label="Loading investigation" />
      ) : inv.isError ? (
        inv.error.status === 404 ? (
          <EmptyState title="No investigation yet">
            <p>Run an investigation to collect the purchase order, receipts, invoice and correspondence.</p>
          </EmptyState>
        ) : (
          <ErrorState error={inv.error} onRetry={() => inv.refetch()} />
        )
      ) : (
        <InvestigationView inv={inv.data} onCite={onCite} />
      )}
    </section>
  );
}

function InvestigationView({ inv, onCite }: { inv: Investigation; onCite: (c: Citation) => void }) {
  const findings = inv.findings as unknown as Finding[];
  const rec = inv.recommendation as Record<string, unknown>;
  return (
    <div className="investigation">
      {inv.stale_reason && (
        <div className="state state-denied" role="status">
          <strong>Out of date.</strong> {inv.stale_reason.charAt(0).toUpperCase() + inv.stale_reason.slice(1)}. Proposals,
          approvals and resolution wait for a current investigation.
        </div>
      )}
      <dl className="facts">
        <dt>Outcome</dt>
        <dd>
          <strong>{statusLabel(inv.outcome)}</strong>
        </dd>
        <dt>Evidence</dt>
        <dd>{statusLabel(inv.evidence_readiness)}</dd>
        <dt>Policy</dt>
        <dd className="mono">{inv.policy_ref}</dd>
        <dt>Purchasing data read</dt>
        <dd>{inv.source_read_at ? new Date(inv.source_read_at).toLocaleString() : "not recorded"}</dd>
        <dt>Version</dt>
        <dd>
          v{inv.version} · {new Date(inv.created_at).toLocaleString()} · explanation: {inv.explanation_origin}
        </dd>
        {inv.model_routing && (
          <>
            <dt>Model use</dt>
            <dd>
              {String(inv.model_routing.mode)}
              {inv.model_routing.blocked ? " — blocked: " : " — "}
              {String(inv.model_routing.reason)}
              {inv.model_routing.data_routing ? ` (policy data routing: ${String(inv.model_routing.data_routing)})` : ""}
            </dd>
          </>
        )}
      </dl>
      <h3 className="section">Explanation</h3>
      <pre className="explanation">{inv.explanation}</pre>
      {rec && rec.action_type ? (
        <div className="state">
          <h3>Recommended next step</h3>
          <p>
            <span className="mono">{String(rec.action_type)}</span>
            {rec.assignee_group ? <> for <strong>{String(rec.assignee_group)}</strong></> : null}: {String(rec.title)}
          </p>
          <p className="muted small">A follow-up task is not a resolution. The case stays open until new evidence matches policy.</p>
        </div>
      ) : null}
      <ProposalDraft rec={rec} />
      {inv.uncertainty.length > 0 && (
        <div className="state state-denied">
          <h3>Uncertainty</h3>
          <ul>
            {inv.uncertainty.map((u) => (
              <li key={u}>{u}</li>
            ))}
          </ul>
        </div>
      )}
      <h3 className="section">Findings</h3>
      <ul className="findings">
        {findings.map((f) => (
          <li key={f.finding_id} className={`finding finding-${f.status}`}>
            <div className="finding-head">
              <span className="badge">{FINDING_LABEL[f.finding_type] ?? f.finding_type}</span>
              <span className="muted small">{statusLabel(f.status)}</span>
              {f.po_line_number && <span className="muted small">PO line {f.po_line_number}</span>}
            </div>
            <p>{f.summary}</p>
            {f.values && Object.keys(f.values).length > 0 && (
              <details>
                <summary>Computed values</summary>
                <table className="table compact">
                  <tbody>
                    {Object.entries(f.values).map(([k, v]) => (
                      <tr key={k}>
                        <th scope="row">{k.replaceAll("_", " ")}</th>
                        <td className="mono">{v || "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </details>
            )}
            {f.evidence && f.evidence.length > 0 && (
              <div className="citations">
                <span className="muted small">Evidence:</span>
                {f.evidence.map((c) => {
                  const resolved = (inv.citations as unknown as Citation[]).find(
                    (x) => x.version_id === c.version_id && x.locator === c.locator && x.field === c.field,
                  );
                  // The accessible name starts with the visible text (speech users say what
                  // they see); the field it cites is added for screen readers.
                  return (
                    <button type="button" key={`${c.version_id}${c.locator}${c.field}`} className="cite"
                            onClick={() => onCite(resolved ?? c)}>
                      {resolved?.document_title ?? c.field} · {c.locator}
                      {resolved && resolved.status !== "valid" ? ` (${resolved.status})` : ""}
                      <span className="visually-hidden">, open evidence for {c.field}</span>
                    </button>
                  );
                })}
              </div>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}

function ProposalDraft({ rec }: { rec: Record<string, unknown> }) {
  const draft = rec?.proposal_draft as Record<string, unknown> | undefined;
  if (!draft || !draft.action_type) return null;
  const gate = draft.gate as { passed: boolean; problems: string[] } | undefined;
  const validation = draft.validation as { accepted: boolean; problems: string[] } | undefined;
  return (
    <div className="state">
      <h3>Draft follow-up (not yet a proposal)</h3>
      <dl className="facts">
        <dt>Action</dt>
        <dd className="mono">{String(draft.action_type)}</dd>
        <dt>Assignee group</dt>
        <dd>{String(draft.assignee_group ?? "—")}</dd>
        <dt>Title</dt>
        <dd>{String(draft.title)}</dd>
        <dt>Drafted by</dt>
        <dd>{String(draft.origin)}</dd>
        <dt>Policy gate</dt>
        <dd>{gate?.passed ? "passed" : `failed: ${(gate?.problems ?? []).join("; ")}`}</dd>
      </dl>
      {draft.body ? <pre className="explanation draft-body">{String(draft.body)}</pre> : null}
      {validation && !validation.accepted && (
        <p className="muted small">Model draft rejected ({validation.problems.join("; ")}); rule-based text used.</p>
      )}
    </div>
  );
}
