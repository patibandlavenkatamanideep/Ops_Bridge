import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState, type FormEvent } from "react";
import {
  ApiError,
  api,
  tenantPath,
  type DecisionResult,
  type OperationItem,
  type OperationList,
  type ProposalItem,
  type ProposalList,
} from "../../api/client";
import { EmptyState, ErrorState, Loading } from "../../components/States";
import { statusLabel } from "../../components/StatusBadge";
import { useSession } from "../../session";

const TERMINAL = ["confirmed", "failed", "outcome_unknown", "cancelled_before_dispatch"];

export function ProposalsPanel({ tenant, caseId, caseStatus }: { tenant: string; caseId: string; caseStatus: string }) {
  const { can } = useSession();
  const qc = useQueryClient();
  const proposals = useQuery<ProposalList, ApiError>({
    queryKey: ["proposals", tenant, caseId],
    queryFn: ({ signal }) => api<ProposalList>(tenantPath(tenant, `/cases/${caseId}/proposals`), { signal }),
    enabled: can(tenant, "proposal.read"),
  });
  const ops = useQuery<OperationList, ApiError>({
    queryKey: ["operations", tenant, caseId],
    queryFn: ({ signal }) => api<OperationList>(tenantPath(tenant, `/cases/${caseId}/operations`), { signal }),
    enabled: can(tenant, "operation.read"),
    refetchInterval: (q) => (q.state.data?.items.some((o) => !TERMINAL.includes(o.status)) ? 1500 : false),
  });
  // Operations finish asynchronously (worker); refresh case state when their statuses change.
  const opSignature = ops.data?.items.map((o) => `${o.id}:${o.status}`).join(",") ?? "";
  useEffect(() => {
    if (opSignature) qc.invalidateQueries({ queryKey: ["case", tenant, caseId] });
  }, [opSignature, qc, tenant, caseId]);
  const create = useMutation<ProposalItem, ApiError>({
    mutationFn: () => api<ProposalItem>(tenantPath(tenant, `/cases/${caseId}/proposals`), { method: "POST", body: {} }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["proposals", tenant, caseId] }),
  });
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["proposals", tenant, caseId] });
    qc.invalidateQueries({ queryKey: ["operations", tenant, caseId] });
    qc.invalidateQueries({ queryKey: ["case", tenant, caseId] });
  };

  if (!can(tenant, "proposal.read")) {
    return <EmptyState title="Proposals are not visible to your role" />;
  }
  if (proposals.isPending) return <Loading label="Loading proposals" />;
  if (proposals.isError) return <ErrorState error={proposals.error} onRetry={() => proposals.refetch()} />;
  const items = proposals.data.items;
  const hasOpen = items.some((p) => p.status === "awaiting_approval");

  return (
    <section aria-labelledby="proposals-title">
      <h2 id="proposals-title">Proposals</h2>
      {can(tenant, "proposal.create") && !hasOpen && ["awaiting_review", "needs_information"].includes(caseStatus) && (
        <div className="actions">
          <button type="button" className="button primary" onClick={() => create.mutate()} disabled={create.isPending}>
            Create proposal from the validated draft
          </button>
        </div>
      )}
      {create.isError && <ErrorState error={create.error} />}
      {items.length === 0 ? (
        <EmptyState title="No proposals yet">
          <p>An investigator turns the validated draft into a versioned proposal for approval.</p>
        </EmptyState>
      ) : (
        <ul className="findings">
          {items.map((p) => (
            <ProposalCard key={p.id} tenant={tenant} caseId={caseId} p={p} onChange={refresh} />
          ))}
        </ul>
      )}
      <h2>Execution timeline</h2>
      {ops.isPending ? (
        <Loading label="Loading operations" />
      ) : ops.isError ? (
        <ErrorState error={ops.error} onRetry={() => ops.refetch()} />
      ) : ops.data.items.length === 0 ? (
        <EmptyState title="Nothing executed">
          <p>Approved proposals become operations; every attempt is listed here.</p>
        </EmptyState>
      ) : (
        <ul className="findings">
          {ops.data.items.map((o) => (
            <OperationCard key={o.id} tenant={tenant} caseId={caseId} op={o} onChange={refresh} />
          ))}
        </ul>
      )}
    </section>
  );
}

function ProposalCard({ tenant, caseId, p, onChange }: { tenant: string; caseId: string; p: ProposalItem;
  onChange: () => void }) {
  const { me, membership, can } = useSession();
  const [reason, setReason] = useState("");
  const decide = useMutation<DecisionResult, ApiError, "approve" | "reject">({
    mutationFn: (decision) =>
      api<DecisionResult>(tenantPath(tenant, `/cases/${caseId}/proposals/${p.id}/decision`), {
        method: "POST",
        body: { decision, version: p.version, payload_digest: p.payload_digest, reason: reason || null },
      }),
    onSuccess: onChange,
  });
  const inScope = membership(tenant)?.approval_scope.includes(p.action_type) ?? false;
  const own = p.author_id === me.user_id;
  const [renderedAt] = useState(() => Date.now());
  const expired = new Date(p.expires_at).getTime() < renderedAt;
  const params = p.parameters as Record<string, string>;
  return (
    <li className={`finding finding-${p.status === "awaiting_approval" ? "needs_review" : "informational"}`}>
      <div className="finding-head">
        <span className="badge">{statusLabel(p.status)}</span>
        <span className="mono small">v{p.version}</span>
        <span className="mono small">{p.action_type}</span>
      </div>
      <p>{p.effect_summary}</p>
      <dl className="facts">
        {params.title && (<><dt>Title</dt><dd>{params.title}</dd></>)}
        {params.assignee_group && (<><dt>Assignee group</dt><dd>{params.assignee_group}</dd></>)}
        <dt>Affects</dt>
        <dd>{p.affected_resources.join(", ")}</dd>
        <dt>Evidence</dt>
        <dd>{p.evidence_refs.length} cited field(s) · manifest <code>{p.source_manifest_digest.slice(7, 19)}…</code></dd>
        <dt>Policy</dt>
        <dd className="mono">{p.policy_ref}</dd>
        <dt>Payload digest</dt>
        <dd><code title={p.payload_digest}>{p.payload_digest.slice(0, 19)}…</code> ({p.canonicalization})</dd>
        <dt>Requires</dt>
        <dd className="mono">{p.required_permission}</dd>
        <dt>Expires</dt>
        <dd>{new Date(p.expires_at).toLocaleString()} {expired && p.status === "awaiting_approval" ? "(expired)" : ""}</dd>
      </dl>
      {params.body && <pre className="explanation draft-body">{params.body}</pre>}
      {p.status === "awaiting_approval" && can(tenant, "proposal.decide") && (
        own ? (
          <p className="muted small">You authored this proposal; separation of duties requires another approver.</p>
        ) : !inScope ? (
          <p className="muted small">Your approval scope does not include {p.action_type}.</p>
        ) : (
          <div className="decision">
            <label>
              Reason (optional)
              <input value={reason} maxLength={1000} onChange={(e) => setReason(e.target.value)} />
            </label>
            <div className="actions">
              <button type="button" className="button primary" disabled={decide.isPending}
                      onClick={() => decide.mutate("approve")}>
                Approve v{p.version}
              </button>
              <button type="button" className="button secondary" disabled={decide.isPending}
                      onClick={() => decide.mutate("reject")}>
                Reject
              </button>
            </div>
            <p className="muted small">You are approving exactly this version (digest {p.payload_digest.slice(7, 19)}…).</p>
          </div>
        )
      )}
      {decide.isError && <ErrorState error={decide.error} />}
    </li>
  );
}

function OperationCard({ tenant, caseId, op, onChange }: { tenant: string; caseId: string; op: OperationItem;
  onChange: () => void }) {
  const { can } = useSession();
  const cancel = useMutation<OperationItem, ApiError>({
    mutationFn: () => api<OperationItem>(tenantPath(tenant, `/cases/${caseId}/operations/${op.id}/cancel`), { method: "POST" }),
    onSuccess: onChange,
  });
  const confirmation = op.confirmation as Record<string, unknown> | null;
  return (
    <li className={`finding finding-${op.status === "confirmed" ? "informational" : op.status === "outcome_unknown" ? "needs_review" : "discrepancy"}`}>
      <div className="finding-head">
        <span className="badge">{statusLabel(op.status)}</span>
        <span className="mono small">{op.action_type}</span>
        <span className="muted small">via {op.connector}</span>
        {op.cancellation !== "none" && <span className="muted small">cancellation: {op.cancellation}</span>}
      </div>
      <dl className="facts">
        <dt>Operation key</dt>
        <dd className="mono small">{op.operation_key}</dd>
        <dt>Remote reference</dt>
        <dd className="mono">{op.remote_ref ?? "—"}</dd>
        <dt>Confirmation</dt>
        <dd>{confirmation ? String(confirmation.method) : op.status === "confirmed" ? "—" : "not confirmed"}</dd>
        {op.failure_category && (<><dt>Category</dt><dd>{op.failure_category}</dd></>)}
        <dt>Executed by</dt>
        <dd className="mono small">{op.executed_by ?? "—"}</dd>
      </dl>
      {op.review_required && (
        <p role="status" className="state state-denied">
          The outcome is unknown. Check the task system before any new attempt; OpsBridge will not retry blindly.
        </p>
      )}
      <ol className="attempts" aria-label="Attempts">
        {op.attempts.map((a) => (
          <li key={a.id} className={`step step-${a.status === "succeeded" ? "completed" : a.status.includes("failure") ? "failed" : ""}`}>
            <span className="mono small">#{a.retry_index}</span> {a.kind} · {a.tool_name} · <strong>{statusLabel(a.status)}</strong>
            {a.error_category && <span className="muted small"> ({a.error_category}{a.error_detail ? `: ${a.error_detail}` : ""})</span>}
            {a.remote_ref && <span className="mono small"> → {a.remote_ref}</span>}
            <span className="muted small"> · {new Date(a.started_at).toLocaleTimeString()}</span>
          </li>
        ))}
      </ol>
      {can(tenant, "operation.cancel") && ["pending_dispatch", "executing"].includes(op.status) && op.cancellation === "none" && (
        <button type="button" className="button secondary" onClick={() => cancel.mutate()} disabled={cancel.isPending}>
          Request cancellation
        </button>
      )}
      {op.status === "outcome_unknown" && can(tenant, "operation.cancel") && (
        <ReviewForm tenant={tenant} caseId={caseId} op={op} onDone={onChange} />
      )}
      {cancel.isError && <ErrorState error={cancel.error} />}
    </li>
  );
}

function ReviewForm({ tenant, caseId, op, onDone }: { tenant: string; caseId: string; op: OperationItem;
  onDone: () => void }) {
  const [finding, setFinding] = useState<"exists" | "absent">("exists");
  const [ref, setRef] = useState("");
  const [note, setNote] = useState("");
  const m = useMutation<OperationItem, ApiError>({
    mutationFn: () => api<OperationItem>(tenantPath(tenant, `/cases/${caseId}/operations/${op.id}/review`), {
      method: "POST",
      body: { finding, remote_ref: finding === "exists" ? ref : null, note },
    }),
    onSuccess: onDone,
  });
  function submit(e: FormEvent) {
    e.preventDefault();
    m.mutate();
  }
  return (
    <form className="form inline-form" onSubmit={submit} aria-label="Record review of unknown outcome">
      <fieldset>
        <legend>What did you find in the task system?</legend>
        <label><input type="radio" name={`f-${op.id}`} checked={finding === "exists"} onChange={() => setFinding("exists")} /> The task exists</label>
        <label><input type="radio" name={`f-${op.id}`} checked={finding === "absent"} onChange={() => setFinding("absent")} /> No task was created</label>
      </fieldset>
      {finding === "exists" && (
        <label>
          Remote reference
          <input required value={ref} maxLength={200} onChange={(e) => setRef(e.target.value)} />
        </label>
      )}
      <label>
        Note
        <input required minLength={5} maxLength={500} value={note} onChange={(e) => setNote(e.target.value)} />
      </label>
      {m.isError && <ErrorState error={m.error} />}
      <button type="submit" className="button primary" disabled={m.isPending}>Record review</button>
    </form>
  );
}
