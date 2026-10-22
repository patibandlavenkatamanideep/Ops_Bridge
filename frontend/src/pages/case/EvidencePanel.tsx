import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState, type FormEvent } from "react";
import {
  ApiError,
  api,
  tenantPath,
  type Citation,
  type DocumentList,
  type FieldItem,
  type UploadResult,
  type VersionDetail,
} from "../../api/client";
import { EmptyState, ErrorState, Loading } from "../../components/States";
import { statusLabel } from "../../components/StatusBadge";
import { useSession } from "../../session";

/** Line number referenced by a locator like "pdf:page=1;line=12" or "text:line=4". */
function locatorLine(locator: string): { page: number | null; line: number | null; row: number | null } {
  const page = /page=(\d+)/.exec(locator);
  const line = /line=(\d+)/.exec(locator);
  const row = /row=(\d+)/.exec(locator);
  return {
    page: page ? Number(page[1]) : null,
    line: line ? Number(line[1]) : null,
    row: row ? Number(row[1]) : null,
  };
}

export function EvidencePanel({ tenant, caseId, focus }: { tenant: string; caseId: string; focus: Citation | null }) {
  const docs = useQuery<DocumentList, ApiError>({
    queryKey: ["documents", tenant, caseId],
    queryFn: ({ signal }) => api<DocumentList>(tenantPath(tenant, `/cases/${caseId}/documents`), { signal }),
  });
  const { can } = useSession();
  const [selected, setSelected] = useState<string | null>(null);
  const versionId = focus?.version_id ?? selected ?? docs.data?.items[0]?.versions[0]?.version_id ?? null;
  const uploader = can(tenant, "evidence.upload") && (
    <UploadForm tenant={tenant} caseId={caseId} onUploaded={(r) => setSelected(r.version_id)} />
  );

  if (docs.isPending) return <Loading label="Loading evidence" />;
  if (docs.isError) return <ErrorState error={docs.error} onRetry={() => docs.refetch()} />;
  // The uploader keeps one position in the tree, so the first upload into an empty case does
  // not remount it and drop its result.
  return (
    <>
      {uploader}
      {docs.data.items.length === 0 ? (
        <EmptyState title="No evidence collected">
          <p>Run an investigation to collect source documents for this case, or upload one.</p>
        </EmptyState>
      ) : (
        <div className="evidence">
          <nav aria-label="Source documents" className="doc-list">
            <ul>
              {docs.data.items.map((d) => (
                <li key={d.document_id}>
                  <span className="doc-title">{d.title}</span>
                  <span className="muted small"> {statusLabel(d.kind)}</span>
                  <ul>
                    {d.versions.map((v) => (
                      <li key={v.version_id}>
                        <button
                          type="button"
                          className={`link-button ${v.version_id === versionId ? "current" : ""}`}
                          aria-current={v.version_id === versionId ? "true" : undefined}
                          onClick={() => setSelected(v.version_id)}
                        >
                          v{v.version_no} · {statusLabel(v.parse_status)}
                        </button>
                      </li>
                    ))}
                  </ul>
                </li>
              ))}
            </ul>
          </nav>
          {versionId && <VersionView tenant={tenant} caseId={caseId} versionId={versionId} focus={focus} />}
        </div>
      )}
    </>
  );
}

const UPLOAD_KINDS = [
  { id: "supplier_message", label: "Supplier message" },
  { id: "invoice", label: "Invoice" },
  { id: "goods_receipt", label: "Goods receipt" },
  { id: "attachment", label: "Other attachment" },
];

/** Uploads are untrusted: the server checks size and content against the declared type and parses with the same
 *  bounded parsers as source-system records. Nothing in a document can change permissions or trigger actions. */
function UploadForm({ tenant, caseId, onUploaded }: { tenant: string; caseId: string;
  onUploaded: (r: UploadResult) => void }) {
  const qc = useQueryClient();
  const [kind, setKind] = useState("supplier_message");
  const [file, setFile] = useState<File | null>(null);
  const [open, setOpen] = useState(false);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const m = useMutation<UploadResult, ApiError>({
    mutationFn: () => {
      const form = new FormData();
      form.append("kind", kind);
      form.append("file", file!);
      return api<UploadResult>(tenantPath(tenant, `/cases/${caseId}/documents`), { method: "POST", body: form });
    },
    onSuccess: (r) => {
      void qc.invalidateQueries({ queryKey: ["documents", tenant, caseId] });
      setFile(null);
      if (inputRef.current) inputRef.current.value = "";
      onUploaded(r);
    },
  });
  function submit(e: FormEvent) {
    e.preventDefault();
    if (file) m.mutate();
  }
  if (!open) {
    return (
      <div className="actions">
        <button type="button" className="button secondary" onClick={() => setOpen(true)}>Upload a document</button>
        {m.isSuccess && <UploadSummary result={m.data} />}
      </div>
    );
  }
  return (
    <form className="form inline-form" onSubmit={submit} aria-label="Upload a document">
      <label>
        Document type
        <select value={kind} onChange={(e) => setKind(e.target.value)}>
          {UPLOAD_KINDS.map((k) => (
            <option key={k.id} value={k.id}>{k.label}</option>
          ))}
        </select>
      </label>
      <label>
        File (PDF, plain text, CSV or JSON)
        <input ref={inputRef} type="file" required accept=".pdf,.txt,.eml,.csv,.json,application/pdf,text/plain,text/csv,application/json"
               onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
      </label>
      {m.isError && <ErrorState error={m.error} />}
      {m.isSuccess && <UploadSummary result={m.data} />}
      <div className="actions">
        <button type="submit" className="button primary" disabled={!file || m.isPending}>Upload</button>
        <button type="button" className="button secondary" onClick={() => setOpen(false)}>Close</button>
      </div>
      <p className="muted small">
        A file with the same name becomes a new version of the earlier upload. Re-run the investigation to use it.
      </p>
    </form>
  );
}

function UploadSummary({ result }: { result: UploadResult }) {
  const records = Object.entries(result.records).map(([k, v]) => `${v} ${statusLabel(k)}`);
  return (
    <p role="status" className="small">
      Stored as version {result.version_no} · {statusLabel(result.parse_status)}
      {records.length > 0 && ` · extracted ${records.join(", ")}`}
      {result.problems.length > 0 && ` · needs review: ${result.problems.join("; ")}`}
    </p>
  );
}

function VersionView({ tenant, caseId, versionId, focus }: { tenant: string; caseId: string; versionId: string;
  focus: Citation | null }) {
  const detail = useQuery<VersionDetail, ApiError>({
    queryKey: ["version", tenant, caseId, versionId],
    queryFn: ({ signal }) =>
      api<VersionDetail>(tenantPath(tenant, `/cases/${caseId}/documents/versions/${versionId}`), { signal }),
  });
  const highlightRef = useRef<HTMLElement | null>(null);
  useEffect(() => {
    highlightRef.current?.scrollIntoView({ block: "center" });
  }, [detail.data, focus]);

  if (detail.isPending) return <Loading label="Loading document" />;
  if (detail.isError) return <ErrorState error={detail.error} onRetry={() => detail.refetch()} />;
  const d = detail.data;
  const focusHere = focus && focus.version_id === versionId ? focus : null;
  const target = focusHere ? locatorLine(focusHere.locator) : null;
  const version = d.version as Record<string, string | number | null>;

  return (
    <div className="side-by-side">
      <section aria-labelledby="fields-title">
        <h3 id="fields-title">Extracted fields</h3>
        <p className="muted small">
          {d.document.title} · v{String(version.version_no)} · raw <code title={String(version.raw_sha256)}>
            {String(version.raw_sha256).slice(7, 19)}…</code> · {String(version.parser)} ·{" "}
          {statusLabel(String(version.parse_status))}
          {version.parse_detail ? ` — ${String(version.parse_detail)}` : ""}
        </p>
        <a className="small" href={tenantPath(tenant, `/cases/${caseId}/documents/versions/${versionId}/original`)}>
          Download original
        </a>
        {d.fields.length === 0 ? (
          <p className="muted">No structured fields were extracted from this document.</p>
        ) : (
          <table className="table compact">
            <thead>
              <tr>
                <th scope="col">Field</th>
                <th scope="col">Value</th>
                <th scope="col">Source</th>
                <th scope="col">Status</th>
              </tr>
            </thead>
            <tbody>
              {d.fields.map((f) => (
                <FieldRow key={f.field_id} tenant={tenant} caseId={caseId} versionId={versionId} field={f}
                          focused={!!focusHere && focusHere.locator === f.locator && focusHere.field === f.field_path}
                          rowRef={highlightRef} />
              ))}
            </tbody>
          </table>
        )}
      </section>
      <section aria-labelledby="source-title">
        <h3 id="source-title">Source text</h3>
        {d.chunks.some((c) => c.locator.startsWith("ocr:")) && (
          <p className="notice small" role="note">
            Read by OCR from the page image. It may contain errors and is not used by the
            matching rules; compare it with the original.
          </p>
        )}
        {d.chunks.length === 0 ? (
          <p className="muted">No text representation (see the original).</p>
        ) : (
          <div className="source">
            {d.chunks.map((c) => {
              const isCsv = c.locator.startsWith("csv:");
              return c.text.split("\n").map((text, i) => {
                // CSV chunks carry the header line first, then exactly one data row.
                const isHeader = isCsv && i === 0;
                const lineNo = isCsv ? (c.line_start ?? 0) : (c.line_start ?? 0) + i;
                const hit = target !== null && !isHeader && (
                  (target.row !== null && isCsv && c.locator === `csv:row=${target.row}`) ||
                  (target.line !== null && !isCsv && target.line === lineNo &&
                    (target.page === null || target.page === c.page))
                );
                return (
                  <div key={`${c.locator}-${i}`} className={`src-line ${hit ? "hit" : ""} ${isHeader ? "muted" : ""}`}
                       ref={hit ? (el) => { highlightRef.current = el; } : undefined}>
                    <span className="ln" aria-hidden="true">
                      {isHeader ? "hdr" : `${c.page !== null ? `p${c.page}:` : ""}${isCsv ? "row " : ""}${lineNo}`}
                    </span>
                    <span>{text}</span>
                  </div>
                );
              });
            })}
          </div>
        )}
      </section>
    </div>
  );
}

function FieldRow({ tenant, caseId, versionId, field, focused, rowRef }: { tenant: string; caseId: string;
  versionId: string; field: FieldItem; focused: boolean; rowRef: React.MutableRefObject<HTMLElement | null> }) {
  const { can } = useSession();
  const [editing, setEditing] = useState(false);
  const corrected = field.corrections.length > 0;
  return (
    <>
      <tr className={focused ? "hit" : undefined} ref={focused ? (el) => { rowRef.current = el; } : undefined}>
        <th scope="row" className="mono small">{field.field_path}</th>
        <td className="mono">
          {field.current_value ?? "—"}
          {corrected && <span className="muted small"> (was {field.original_value})</span>}
        </td>
        <td className="mono small">{field.locator}</td>
        <td>
          <span className="small">{statusLabel(field.status)}</span>
          {field.note && <div className="muted small">{field.note}</div>}
          {can(tenant, "evidence.correct") && !editing && field.correctable && (
            <button type="button" className="button link small" onClick={() => setEditing(true)}>
              Correct
            </button>
          )}
        </td>
      </tr>
      {corrected && (
        <tr>
          <td colSpan={4} className="small muted">
            {field.corrections.map((c) => (
              <div key={c.correction_id}>
                {new Date(c.created_at).toLocaleString()} · {c.author}: {c.previous_value} → {c.new_value} — “{c.reason}”
              </div>
            ))}
          </td>
        </tr>
      )}
      {editing && (
        <tr>
          <td colSpan={4}>
            <CorrectionForm tenant={tenant} caseId={caseId} versionId={versionId} field={field}
                            onDone={() => setEditing(false)} />
          </td>
        </tr>
      )}
    </>
  );
}

function CorrectionForm({ tenant, caseId, versionId, field, onDone }: { tenant: string; caseId: string;
  versionId: string; field: FieldItem; onDone: () => void }) {
  const qc = useQueryClient();
  const [value, setValue] = useState(field.current_value ?? "");
  const [reason, setReason] = useState("");
  const m = useMutation<unknown, ApiError>({
    mutationFn: () =>
      api(tenantPath(tenant, `/cases/${caseId}/fields/${field.field_id}/corrections`), {
        method: "POST",
        body: { new_value: value, reason, expected_current_value: field.current_value },
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["version", tenant, caseId, versionId] });
      onDone();
    },
  });
  function submit(e: FormEvent) {
    e.preventDefault();
    m.mutate();
  }
  return (
    <form className="form inline-form" onSubmit={submit} aria-label={`Correct ${field.field_path}`}>
      <label>
        Corrected value
        <input required value={value} maxLength={500} onChange={(e) => setValue(e.target.value)} />
      </label>
      <label>
        Reason (kept in the audit history)
        <input required minLength={5} maxLength={1000} value={reason} onChange={(e) => setReason(e.target.value)} />
      </label>
      {m.isError && <ErrorState error={m.error} />}
      <div className="actions">
        <button type="submit" className="button primary" disabled={m.isPending}>Save correction</button>
        <button type="button" className="button secondary" onClick={onDone}>Cancel</button>
      </div>
      <p className="muted small">The original extracted value is preserved. Re-run the investigation to apply the correction.</p>
    </form>
  );
}
