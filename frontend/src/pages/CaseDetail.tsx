import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef, useState, type KeyboardEvent } from "react";
import { Link, useParams } from "react-router-dom";
import { ApiError, apiAs, tenantPath, type AssigneeList, type CaseOut, type Citation } from "../api/client";
import { ErrorState, Loading } from "../components/States";
import { StatusBadge, statusLabel } from "../components/StatusBadge";
import { useAnnouncement } from "../components/Announcement";
import { useSession } from "../session";
import { ConversationPanel } from "./case/ConversationPanel";
import { EvidencePanel } from "./case/EvidencePanel";
import { InvestigationPanel } from "./case/InvestigationPanel";
import { ProposalsPanel } from "./case/ProposalsPanel";

type Tab = "investigation" | "evidence" | "proposals" | "conversation";
const TABS: { id: Tab; label: string; permission?: string }[] = [
  { id: "investigation", label: "Investigation" },
  { id: "evidence", label: "Evidence", permission: "evidence.read" },
  { id: "proposals", label: "Proposals & execution", permission: "proposal.read" },
  { id: "conversation", label: "Conversation" },
];

export function CaseDetail() {
  const { tenant = "", caseId = "" } = useParams();
  const { can } = useSession();
  const qc = useQueryClient();
  const [tab, setTab] = useState<Tab>("investigation");
  const [focus, setFocus] = useState<Citation | null>(null);
  const tabRefs = useRef<Record<string, HTMLButtonElement | null>>({});
  const query = useQuery<CaseOut, ApiError>({
    queryKey: ["case", tenant, caseId],
    queryFn: ({ signal }) => apiAs("CaseOut", tenantPath(tenant, `/cases/${caseId}`), { signal }),
  });
  const resolve = useMutation<CaseOut, ApiError>({
    mutationFn: () => apiAs("CaseOut", tenantPath(tenant, `/cases/${caseId}/resolve`), { method: "POST", body: {} }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["case", tenant, caseId] }),
  });

  if (query.isPending) return <Loading label="Loading case" />;
  if (query.isError) return <ErrorState error={query.error} onRetry={() => query.refetch()} />;
  const c = query.data;
  const visibleTabs = TABS.filter((t) => !t.permission || can(tenant, t.permission));

  function onKey(e: KeyboardEvent<HTMLButtonElement>) {
    const idx = visibleTabs.findIndex((t) => t.id === tab);
    const delta = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
    if (!delta) return;
    const next = visibleTabs[(idx + delta + visibleTabs.length) % visibleTabs.length]!;
    setTab(next.id);
    tabRefs.current[next.id]?.focus();
  }

  return (
    <article aria-labelledby="case-title">
      <p>
        <Link to={`/t/${tenant}/cases`}>← Case queue</Link>
      </p>
      <div className="page-header">
        <h1 id="case-title">
          <span className="mono">{c.case_number}</span> {c.title}
        </h1>
        <StatusBadge status={c.status} />
      </div>
      <dl className="facts">
        <dt>Invoice</dt>
        <dd className="mono">{c.invoice_number ?? "—"}</dd>
        <dt>Purchase order</dt>
        <dd className="mono">{c.po_number ?? "—"}</dd>
        <dt>Discrepancies</dt>
        <dd>{c.discrepancy_types.length ? c.discrepancy_types.map(statusLabel).join(", ") : "—"}</dd>
        <dt>Evidence</dt>
        <dd>{statusLabel(c.evidence_readiness)}</dd>
        <dt>Owner</dt>
        <dd>{c.owner ?? "Unassigned"}</dd>
        <dt>Assigned group</dt>
        <dd>{c.assignee_group ?? "—"}</dd>
        <dt>Opened</dt>
        <dd>{new Date(c.created_at).toLocaleString()}</dd>
      </dl>
      {c.description && <p className="description">{c.description}</p>}
      {can(tenant, "case.assign") && !["resolved", "closed_without_resolution"].includes(c.status) && (
        <OwnerControls tenant={tenant} caseId={caseId} c={c} />
      )}
      {/* Offered only when the latest investigation found no discrepancy; the server enforces this too. */}
      {can(tenant, "investigation.run") && ["awaiting_review", "follow_up_pending"].includes(c.status) &&
        c.discrepancy_types.length === 0 && c.evidence_readiness === "complete" && (
        <div className="actions">
          <button type="button" className="button secondary" onClick={() => resolve.mutate()} disabled={resolve.isPending}>
            Mark business exception resolved
          </button>
        </div>
      )}
      {resolve.isError && <ErrorState error={resolve.error} />}

      <div role="tablist" aria-label="Case sections" className="tabs">
        {visibleTabs.map((t) => (
          <button key={t.id} type="button" role="tab" id={`tab-${t.id}`} aria-controls={`panel-${t.id}`}
                  aria-selected={tab === t.id} tabIndex={tab === t.id ? 0 : -1}
                  ref={(el) => { tabRefs.current[t.id] = el; }} onClick={() => setTab(t.id)} onKeyDown={onKey}>
            {t.label}
          </button>
        ))}
      </div>
      <div role="tabpanel" id={`panel-${tab}`} aria-labelledby={`tab-${tab}`} className="tabpanel">
        {tab === "investigation" ? (
          <InvestigationPanel tenant={tenant} caseId={caseId}
                              onCite={(cite) => { setFocus(cite); if (can(tenant, "evidence.read")) setTab("evidence"); }} />
        ) : tab === "evidence" ? (
          <EvidencePanel tenant={tenant} caseId={caseId} focus={focus} />
        ) : tab === "proposals" ? (
          <ProposalsPanel tenant={tenant} caseId={caseId} caseStatus={c.status} />
        ) : (
          <ConversationPanel tenant={tenant} caseId={caseId} />
        )}
      </div>
    </article>
  );
}

/** Take, hand over or release the case. The owner the page shows is sent along, so a change
 * someone else made meanwhile is refused instead of overwritten. */
function OwnerControls({ tenant, caseId, c }: { tenant: string; caseId: string; c: CaseOut }) {
  const { me } = useSession();
  const qc = useQueryClient();
  const [target, setTarget] = useState("");
  const [notice, announce] = useAnnouncement();
  const people = useQuery<AssigneeList, ApiError>({
    queryKey: ["assignees", tenant],
    queryFn: ({ signal }) => apiAs("AssigneeList", tenantPath(tenant, "/assignees"), { signal }),
  });
  const change = useMutation<CaseOut, ApiError, string | null>({
    mutationFn: (owner) =>
      apiAs("CaseOut", tenantPath(tenant, `/cases/${caseId}/owner`), {
        method: "POST",
        body: { owner_id: owner, expected_owner_id: c.assignee_id },
      }),
    onSuccess: (updated, owner) => {
      announce(owner === null ? "You released this case." : owner === me.user_id ? "You own this case."
        : `Given to ${updated.owner ?? "another investigator"}.`);
      setTarget("");
    },
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: ["case", tenant, caseId] });
      void qc.invalidateQueries({ queryKey: ["case-views", tenant] });
      void qc.invalidateQueries({ queryKey: ["cases", tenant] });
    },
  });
  const mine = c.assignee_id === me.user_id;
  const others = (people.data?.items ?? []).filter((m) => m.id !== c.assignee_id);
  return (
    <div className="actions" role="group" aria-label="Case owner">
      {!mine && (
        <button type="button" className="button secondary" disabled={change.isPending}
                onClick={() => change.mutate(me.user_id)}>
          {c.assignee_id ? "Take over this case" : "Take this case"}
        </button>
      )}
      {c.assignee_id && (
        <button type="button" className="button secondary" disabled={change.isPending}
                onClick={() => change.mutate(null)}>
          Release
        </button>
      )}
      {others.length > 0 && (
        <form className="inline-form" onSubmit={(e) => { e.preventDefault(); if (target) change.mutate(target); }}>
          <label>
            Give to{" "}
            <select value={target} onChange={(e) => setTarget(e.target.value)}>
              <option value="">Choose an investigator</option>
              {others.map((m) => (
                <option key={m.id} value={m.id}>{m.display_name}</option>
              ))}
            </select>
          </label>{" "}
          <button type="submit" className="button secondary" disabled={!target || change.isPending}>
            Assign
          </button>
        </form>
      )}
      {change.isError && <ErrorState error={change.error} />}
      {notice}
    </div>
  );
}
