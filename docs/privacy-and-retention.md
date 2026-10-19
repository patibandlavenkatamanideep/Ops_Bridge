# Privacy and retention

Scope: what OpsBridge stores, what each tenant can delete, how redaction works, and what it
does not guarantee. All statements describe the local build on `feat/opsbridge-foundation`;
nothing here has been reviewed by counsel or validated against a regulation.

## What is stored

| Data | Where | Deleted by retention? |
|---|---|---|
| Original document bytes (records, PDFs, emails, uploads) | tenant-scoped artifact store (`evidence/storage.py`), digest in `artifacts` / `document_versions` | yes — `raw_documents_days` |
| Extracted text chunks (search and excerpts) | `evidence_chunks` | yes — with the original, same key |
| Extracted field values and canonical records (quantities, prices, PO/invoice numbers, supplier assertions) | `extracted_fields`, record tables | **no** — they are the decision inputs referenced by findings, approvals and audit |
| Investigation snapshots, findings, citations (references and values, no excerpts) | `investigation_versions` | no |
| Model requests/responses (redacted prompts, outputs) | artifact store, `model_calls` | yes — `model_artifacts_days` |
| Tool requests/responses | artifact store, `tool_invocations` | yes — `tool_artifacts_days` |
| Case conversation message text | `conversation_messages.body` | yes — `conversation_days` (text cleared; row kept) |
| Audit events, approvals, operations, attempts | their tables (insert-only for the app role where applicable) | no |
| Application logs | process stdout/stderr | not managed by OpsBridge (see Logs) |

Deletion removes stored bytes and free text. It keeps the fact that something existed: the
artifact row keeps its digest, `deleted_at` and a `deletion_reason` such as
`retention:raw_documents_days=30`; a cleared message keeps author, time, reason and the
content digest. **A digest is not anonymization**: short or guessable text can be confirmed
by hashing candidates.

After deletion the API says so instead of failing silently: an original download returns 404
"original (withheld or deleted by retention policy)", the evidence view shows no source
text, and findings remain with their values and references. Replay or semantic review of
deleted material is no longer possible.

## Configuring and running retention

- Tenant administrators (`tenant.manage_policy`) set retention in days per key on the Tenant
  settings page or `PUT /api/v1/tenants/{tenant}/settings`
  (`{"retention": {"raw_documents_days": 30, ...}}`; 0–3650; a missing key means "keep").
  The PUT replaces the whole retention map. Every change is audited (`tenant.settings_updated`).
- `POST /api/v1/tenants/{tenant}/retention/run` (or "Run retention sweep") enqueues a
  `retention_sweep` job; the worker deletes artifact bytes, chunks and message text past
  the cutoff and writes `retention.sweep` with counts.
- The worker also schedules a sweep once per database day for every tenant that configured
  retention (`OPSBRIDGE_RETENTION_SCHEDULE_SECONDS`, default hourly checks; 0 disables). It
  learns which tenants are due only through the aggregate `ops.tenants_with_retention()`
  (migration 0005) and deduplicates by tenant and day. Cut-offs use the database clock.
- Backups taken before a sweep still contain the deleted content until those backups expire
  (backup handling: Phase 8, docs/operations.md when written).

Evidence: `tests/integration/test_hardening.py::test_retention_deletes_protected_content_and_says_so`
(original 200 → sweep → 404 with the retention message; chunks gone; digest and findings
kept), `test_tenant_settings_respect_roles_and_tiers` (invalid keys/values refused,
non-admins denied).

## Conversations

Case notes (`/cases/{id}/messages`) are case-scoped records. A correction is a new message
with `supersedes_id` (author only); the original stays visible as corrected. Deletion (author
or tenant administrator) clears the text and records who, when and why. Evidence:
`test_conversation_correction_and_controlled_deletion`, `frontend/e2e/phase6.spec.ts`.

## Redaction before model calls

`privacy/redaction.py` (`opsbridge-redact/2`) replaces emails, IBAN-like strings,
Luhn-valid card numbers, US SSN patterns, phone numbers and labelled bank account numbers
before hosted model calls, in logs and in the text of external follow-up tasks; the metadata
(`version`, counts, "pattern-based; not anonymization") is stored with the model call. Names,
free-text addresses and context-dependent identifiers pass through. Version 2 (2026-10-19)
never alters UUIDs and does not treat digits joined to a prefix by a hyphen
(`INV-100023456`, `T-1705292356`) as phone numbers: version 1 corrupted about 1.2% of random
UUIDs and any such id with nine or more digits. Before hosted model calls and external
follow-up tasks, the case's own identifiers (case, invoice and PO numbers, resolved supplier
id) are kept exactly, so a bare 10-digit PO number survives; personal data next to them is
still redacted (`test_hosted_calls_are_redacted_but_keep_the_cases_own_identifiers`,
`test_a_bare_numeric_po_number_stays_readable_in_the_created_task`). In logs such a bare
number can still be redacted. Span correlation ids skip the identifier patterns (credentials
are still removed). Local-required policies never fall back to a
hosted model (`test_local_required_policy_blocks_model_without_hosted_fallback`).

## Logs

`telemetry/logging.py` installs a redacting filter on every handler of the API, worker,
simulator and MCP processes. It scrubs bearer tokens, JWTs, provider API keys, secret-like
`key=value` pairs, sensitive URL query parameters (OIDC callback `code` and `state`,
tokens, signatures) and the identifier patterns above, including exception tracebacks.
uvicorn's access log keeps its format with the values replaced by `[REDACTED]`.
Evidence: `tests/privacy/test_log_redaction.py` (14). Limits: pattern-based; the code avoids
logging document content, but a new log statement that does so would only be pattern-scrubbed.
OpenTelemetry export is not implemented yet (Phase 8); when it is, span attributes must go
through the same scrubber.

## Uploads

Uploads are untrusted input: size-capped (`max_upload_bytes`, checked against the declared
`Content-Length` before buffering; chunked bodies without a length are refused with 411),
content sniffed against the declared type (HTML disguised as PDF or text is rejected),
filenames reduced to a safe basename, no URL fetching, and parsing through the same bounded
parsers and pack mappings as source records. Instructions inside documents are data
(`test_injected_document_instructions_cannot_change_authority`). Evidence:
`test_upload_*` and `test_chunked_body_without_length_is_refused_before_buffering` in
`test_hardening.py`; browser: `e2e/phase6.spec.ts`.

## Not provided

- No PII discovery across stored data, no data-subject export tooling, no legal hold.
- No encryption by OpsBridge itself. The local artifact store is plain files under
  `var/artifacts`; the S3 store can request SSE-KMS per upload and relies on the bucket's
  default encryption, not tested against AWS (docs/deployment.md). In a versioned bucket,
  retention deletes need a noncurrent-version lifecycle rule to remove the bytes.
- OCR is optional and off by default (`OPSBRIDGE_OCR_ENABLED`). It runs locally (tesseract in
  the app image); page images never leave the deployment. OCR text is stored like other
  extracted text (evidence chunks, same retention) and labelled as OCR; without OCR, scanned
  documents route to `needs_review` with the reason.
