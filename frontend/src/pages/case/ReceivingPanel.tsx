import { useQuery } from "@tanstack/react-query";
import { ApiError, apiAs, tenantPath, type Receiving } from "../../api/client";
import { EmptyState, ErrorState, Loading } from "../../components/States";

type Line = Receiving["lines"][number];

const DISPOSITION: Record<string, string> = {
  none: "Nothing rejected",
  unrecorded: "Rejected; no return, credit or replacement recorded",
  returned: "Returned; no credit recorded",
  credit_pending: "Credit promised; none posted",
  replacement_pending: "Replacement promised; not arrived",
  replacement_unaccepted: "Replacement arrived; acceptance not recorded",
  credited: "Covered by a posted credit",
  replaced: "Covered by accepted replacements",
  resolved: "Covered by credits and replacements",
};

const HOLD: Record<string, string> = {
  whole_invoice: "Policy holds the whole invoice",
  disputed_portion: "Policy reviews only the disputed portion",
  unconfigured: "Policy does not say; payment recommendation unresolved",
};

/** A quantity as the records give it: null is unknown, never zero. */
function qty(value: string | null | undefined, unit: string) {
  return value == null ? <span className="muted">unknown</span> : <span className="mono">{value} {unit}</span>;
}

function when(occurred: string | null, precision: string) {
  if (occurred == null || precision === "unknown") return <span className="muted">not stated</span>;
  return precision === "date" ? <>{occurred} <span className="muted small">(date only)</span></>
    : new Date(occurred).toLocaleString();
}

function LineFacts({ line }: { line: Line }) {
  const u = line.unit;
  const rows: [string, React.ReactNode][] = [
    ["Ordered", qty(line.ordered, u)],
    ["Delivered", qty(line.delivered, u)],
    ["Accepted at receiving", qty(line.accepted, u)],
    ["Rejected at receiving", qty(line.rejected, u)],
    ["Not delivered", qty(line.undelivered, u)],
    ["Returned to supplier", qty(line.returned, u)],
    ["Replacements delivered / accepted",
      <>{qty(line.replacement_delivered, u)} / {qty(line.replacement_accepted, u)}</>],
    ["Credit promised / posted for this invoice",
      <>{qty(line.credit_promised, u)} / {qty(line.credit_posted, u)}</>],
    ["Billed on this invoice", qty(line.invoiced, u)],
    ["Matched", qty(line.matched, u)],
    ["Disputed", qty(line.disputed, u)],
  ];
  return (
    <table className="table">
      <caption>PO line {line.po_line_number} ({u}): {DISPOSITION[line.disposition] ?? line.disposition}</caption>
      <tbody>
        {rows.map(([label, value]) => (
          <tr key={label}>
            <th scope="row">{label}</th>
            <td>{value}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export function ReceivingPanel({ tenant, caseId }: { tenant: string; caseId: string }) {
  const q = useQuery<Receiving, ApiError>({
    queryKey: ["receiving", tenant, caseId],
    queryFn: ({ signal }) => apiAs("ReceivingOut", tenantPath(tenant, `/cases/${caseId}/receiving`), { signal }),
    retry: false,
  });
  if (q.isPending) return <Loading label="Loading receiving" />;
  if (q.isError) {
    return q.error.status === 404 ? (
      <EmptyState title="No investigation yet">
        <p>Run an investigation to read the receiving records for this invoice.</p>
      </EmptyState>
    ) : (
      <ErrorState error={q.error} onRetry={() => q.refetch()} />
    );
  }
  const v = q.data;
  const question = v.question;
  return (
    <section aria-labelledby="receiving-title">
      <h2 id="receiving-title">Receiving and disposition</h2>
      <p className="muted small">
        Quantities from the current receiving records for <span className="mono">{v.po_number ?? "—"}</span>;
        matched and disputed as concluded by investigation {v.investigation_version} ({v.rules_version},{" "}
        {v.policy_ref}). Unknown is shown as unknown, never as zero.
      </p>

      <h3>Next question</h3>
      <dl className="facts">
        <dt>Asked of</dt>
        <dd>
          {question.owner ?? "—"}
          {!question.owner_configured && (
            <span className="badge badge-needs_information"> No owner is configured for rejected goods; a person decides who asks</span>
          )}
        </dd>
        <dt>Step</dt>
        <dd>{question.title ?? "—"}</dd>
      </dl>
      {question.basis.length > 0 && (
        <>
          <h4>Basis</h4>
          <ul className="basis">
            {question.basis.map((b) => (
              <li key={b.finding_id}>
                <span className="mono small">{b.reason_code}</span> {b.summary}
              </li>
            ))}
          </ul>
        </>
      )}

      {v.split && (
        <div className="state" role="note">
          <strong>{HOLD[v.split.hold ?? "unconfigured"] ?? v.split.hold}.</strong> {v.split.summary}
        </div>
      )}

      {v.lines.map((line) => (
        <div key={line.po_line_number} className="receiving-line">
          <LineFacts line={line} />
          {line.rejections.length > 0 && (
            <>
              <h4>Rejections on PO line {line.po_line_number}</h4>
              <ul>
                {line.rejections.map((r) => (
                  <li key={r.source}>
                    <span className="mono">{r.quantity} {line.unit}</span> rejected
                    {r.reason ? <> — {r.reason}</> : <span className="badge badge-needs_information"> no reason recorded</span>}
                    {" "}on <span className="mono">{r.receipt_number ?? "—"}</span>, {when(r.occurred, r.occurred_precision)}
                    {r.late && <> (recorded as <span className="mono">{r.source}</span>, after the receipt)</>}
                  </li>
                ))}
              </ul>
            </>
          )}
          {line.credits_elsewhere.length > 0 && (
            <p className="small">A posted credit names {line.credits_elsewhere.join(", ")}, not this invoice.</p>
          )}
          {line.problems.length > 0 && (
            <div className="state state-denied" role="note">
              <strong>Not applied:</strong>
              <ul>
                {line.problems.map((p) => (
                  <li key={p.code + p.detail}>{p.detail}</li>
                ))}
              </ul>
            </div>
          )}
        </div>
      ))}
      {v.unattached_events.length > 0 && (
        <p className="state state-denied" role="note">
          Events naming a PO line this order does not have, attached to none:{" "}
          {v.unattached_events.map((e) => `${e.event_id} (line ${e.po_line_number})`).join(", ")}.
        </p>
      )}

      <h3>Since approval</h3>
      {v.since_approval.length === 0 ? (
        <p className="muted">No approval on this case yet.</p>
      ) : (
        <ul className="since-approval">
          {v.since_approval.map((a) => (
            <li key={a.proposal_id}>
              <strong>{a.action_type.replaceAll("_", " ")}</strong> approved{" "}
              {new Date(a.approved_at).toLocaleString()} (proposal {a.proposal_status}
              {a.operation_status ? `, ${a.operation_status.replaceAll("_", " ")}` : ""}): {a.requires}
              {a.changes.length > 0 && (
                <ul>
                  {a.changes.map((c) => (
                    <li key={c.document_title + c.version_no}>
                      {c.change}: {c.document_title} version {c.version_no}, recorded{" "}
                      {new Date(c.recorded_at).toLocaleString()}
                    </li>
                  ))}
                </ul>
              )}
            </li>
          ))}
        </ul>
      )}

      <h3>Timeline</h3>
      <table className="table">
        <caption className="visually-hidden">Receiving evidence, approvals and executions, by when OpsBridge recorded them</caption>
        <thead>
          <tr>
            <th scope="col">Recorded by OpsBridge</th>
            <th scope="col">Record</th>
            <th scope="col">What it says</th>
            <th scope="col">Occurred (as the source states)</th>
          </tr>
        </thead>
        <tbody>
          {v.timeline.map((e) => (
            <tr key={`${e.kind}-${e.id}-${e.recorded_at}`} className={e.current ? undefined : "superseded"}>
              <td>{new Date(e.recorded_at).toLocaleString()}</td>
              <td>
                <span className="mono">{e.id}</span> <span className="muted small">{e.kind}</span>
                {e.version_no != null && <span className="muted small"> v{e.version_no}</span>}
                {e.after_approval && <span className="badge badge-needs_information"> learned after approval</span>}
                {!e.current && <span className="badge"> superseded</span>}
              </td>
              <td>{e.summary}</td>
              <td>{e.kind === "receipt" || e.kind === "event" ? when(e.occurred, e.occurred_precision) : "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}
