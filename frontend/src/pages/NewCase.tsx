import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useRef, useState, type FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { ApiError, api, tenantPath, type CaseCreate, type CaseOut } from "../api/client";
import { ErrorState } from "../components/States";

export function NewCase() {
  const { tenant = "" } = useParams();
  const navigate = useNavigate();
  const qc = useQueryClient();
  // One key per form instance so a double submit returns the same case.
  const idempotencyKey = useRef(crypto.randomUUID());
  const [form, setForm] = useState({ title: "", description: "", invoice_number: "", po_number: "" });
  const mutation = useMutation<CaseOut, ApiError, CaseCreate>({
    mutationFn: (body) =>
      api<CaseOut>(tenantPath(tenant, "/cases"), {
        method: "POST",
        body,
        headers: { "Idempotency-Key": idempotencyKey.current },
      }),
    onSuccess: (created) => {
      qc.invalidateQueries({ queryKey: ["cases", tenant] });
      navigate(`/t/${tenant}/cases/${created.id}`);
    },
  });

  function submit(e: FormEvent) {
    e.preventDefault();
    mutation.mutate({
      schema_version: 1,
      title: form.title,
      description: form.description,
      invoice_number: form.invoice_number || null,
      po_number: form.po_number || null,
    });
  }

  const duplicateId = mutation.error?.code === "duplicate_case" ? mutation.error.details.existing_case_id : null;

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
