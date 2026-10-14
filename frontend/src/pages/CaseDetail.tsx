import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { ApiError, api, tenantPath, type CaseOut } from "../api/client";
import { ErrorState, Loading } from "../components/States";
import { StatusBadge, statusLabel } from "../components/StatusBadge";

export function CaseDetail() {
  const { tenant = "", caseId = "" } = useParams();
  const query = useQuery<CaseOut, ApiError>({
    queryKey: ["case", tenant, caseId],
    queryFn: ({ signal }) => api<CaseOut>(tenantPath(tenant, `/cases/${caseId}`), { signal }),
  });

  if (query.isPending) return <Loading label="Loading case" />;
  if (query.isError) return <ErrorState error={query.error} onRetry={() => query.refetch()} />;
  const c = query.data;
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
        <dt>Evidence</dt>
        <dd>{statusLabel(c.evidence_readiness)}</dd>
        <dt>Opened</dt>
        <dd>{new Date(c.created_at).toLocaleString()}</dd>
      </dl>
      {c.description && <p className="description">{c.description}</p>}
    </article>
  );
}
