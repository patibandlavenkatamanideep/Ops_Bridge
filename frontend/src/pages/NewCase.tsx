import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useRef, useState, type FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { ApiError, apiAs, tenantPath, type CaseCreate, type CaseOut } from "../api/client";
import { ErrorState } from "../components/States";

export function NewCase() {
  const { tenant = "" } = useParams();
  const navigate = useNavigate();
  const qc = useQueryClient();
  // One key per form instance so a double submit returns the same case.
  const idempotencyKey = useRef(crypto.randomUUID());
  const [form, setForm] = useState({ title: "", description: "", invoice_number: "", po_number: "", supplier_id: "" });
  const mutation = useMutation<CaseOut, ApiError, CaseCreate>({
    mutationFn: (body) =>
      apiAs("CaseOut", tenantPath(tenant, "/cases"), {
        method: "POST",
        body,
        headers: { "Idempotency-Key": idempotencyKey.current },
      }),
    onSuccess: (created) => {
      qc.invalidateQueries({ queryKey: ["cases", tenant] });
      navigate(`/t/${tenant}/cases/${created.id}`);
    },
  });

  function send(confirmDistinct: boolean) {
    mutation.mutate({
      schema_version: 1,
      title: form.title,
      description: form.description,
      invoice_number: form.invoice_number || null,
      po_number: form.po_number || null,
      supplier_id: form.supplier_id || null,
      confirm_distinct: confirmDistinct,
    });
  }

  function submit(e: FormEvent) {
    e.preventDefault();
    send(false);
  }

  const duplicateId = mutation.error?.code === "duplicate_case" ? mutation.error.details.existing_case_id : null;
  const similar = mutation.error?.code === "possible_duplicate" ? mutation.error.details : null;

  return (
    <section aria-labelledby="new-case-title">
      <h1 id="new-case-title">New case</h1>
      <form className="form" onSubmit={submit}>
        <label>
          Title
          <input required minLength={3} maxLength={300} value={form.title}
                 onChange={(e) => setForm({ ...form, title: e.target.value })} />
        </label>
        <label>
          Invoice number
          <input maxLength={100} value={form.invoice_number}
                 onChange={(e) => setForm({ ...form, invoice_number: e.target.value })} />
        </label>
        <label>
          Purchase order number
          <input maxLength={100} value={form.po_number}
                 onChange={(e) => setForm({ ...form, po_number: e.target.value })} />
        </label>
        <label>
          Supplier ID (optional)
          <input maxLength={100} value={form.supplier_id} aria-describedby="supplier-hint"
                 onChange={(e) => setForm({ ...form, supplier_id: e.target.value })} />
        </label>
        <p id="supplier-hint" className="muted small">Needed when two suppliers use the same number.</p>
        <label>
          What is blocked and why do you think so?
          <textarea rows={4} maxLength={5000} value={form.description}
                    onChange={(e) => setForm({ ...form, description: e.target.value })} />
        </label>
        {mutation.isError &&
          (mutation.error.code === "duplicate_case" ? (
            <div className="state state-error" role="alert">
              A case already exists for this invoice.{" "}
              {duplicateId ? <Link to={`/t/${tenant}/cases/${String(duplicateId)}`}>Open the existing case</Link> : null}
            </div>
          ) : mutation.error.code === "possible_duplicate" ? (
            <div className="state state-error" role="alert">
              <p>
                {similar?.existing_invoice_number
                  ? <>A case exists for invoice <code>{String(similar.existing_invoice_number)}</code>, which differs only in punctuation, spacing or leading zeros.{" "}
                      <Link to={`/t/${tenant}/cases/${String(similar.existing_case_id)}`}>Open it</Link></>
                  : "A case exists for an invoice number that differs only in punctuation, spacing or leading zeros."}
              </p>
              <button type="button" className="button secondary" disabled={mutation.isPending} onClick={() => send(true)}>
                It is a different invoice: create the case
              </button>
            </div>
          ) : (
            <ErrorState error={mutation.error} />
          ))}
        <div className="actions">
          <button type="submit" className="button primary" disabled={mutation.isPending}>
            {mutation.isPending ? "Creating…" : "Create case"}
          </button>
          <Link className="button secondary" to={`/t/${tenant}/cases`}>
            Cancel
          </Link>
        </div>
      </form>
    </section>
  );
}
