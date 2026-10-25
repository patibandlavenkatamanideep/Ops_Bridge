# Data contracts

The shapes OpsBridge depends on, where they are defined, and how they are versioned. Source of
truth is the code named in each section; this page is the map.

## Canonical records (`backend/src/opsbridge/domain/records.py`)

Pack mappings turn each organization's native documents into these frozen records; the
matcher only ever sees these. Money and quantities are `Decimal`; dates are `date`; every
record and line carries `refs` (field → `SourceRef`) for citations.

| Record | Fields |
|---|---|
| `PurchaseOrder` | `po_number`, `supplier_id`, `supplier_name`, `currency`, `status` (open, closed, cancelled), `lines[POLine]` |
| `POLine` | `line_number`, `item_code?`, `description`, `quantity`, `unit`, `unit_price`, `discount_amount` (total for the line) |
| `Invoice` | `invoice_number`, `supplier_id?`, `supplier_name`, `po_number?`, `currency`, `invoice_date`, `status` (open, cancelled), `lines[InvoiceLine]` |
| `InvoiceLine` | `line_number`, `po_line_number?`, `item_code?`, `description`, `quantity`, `unit`, `unit_price`, `discount_amount`, `kind` (goods, charge) |
| `GoodsReceipt` | `receipt_number`, `po_number`, `received_date`, `lines[ReceiptLine]` |
| `ReceiptLine` | `po_line_number`, `quantity`, `unit`, `kind` (receipt, return), `status` (accepted, rejected, reversed) |
| `InvoiceAllocation` | earlier invoice quantity already matched: `invoice_number`, `po_number`, `po_line_number`, `quantity`, `status` (active, cancelled) |
| `SupplierAssertion` | a parsed claim from a supplier message: `po_number`, `po_line_number?`, `claim` (delivered_quantity), `quantity`, `unit?`, `extraction_status` |

`SourceRef` = `document_id`, `version_id`, `sha256` (raw bytes), `locator`, `field`
(canonical name such as `invoice_line.quantity`), `classification`. Locators:
`json:/lines/0/qty_ordered` (JSON pointer), `csv:row=2;col=qty`, `pdf:page=1;line=9`,
`ocr:page=1;line=3` (text read by OCR, never authoritative), `text:line=4`. Extracted fields
read by OCR have `method = ocr` and `status = needs_review`, and no record is built from them,
so the matching rules never use them; corrections of them are refused (`ocr_candidate`).
A citation is valid only if the tenant, version, digest and locator still
resolve (`evidence/citations.py`).

## Pack mapping (`backend/src/opsbridge/mapping.py`, `packs/<pack>/pack.yaml`)

`PackSpec`: `schema_version` (1), `pack_id`, `version`, `display_name`, `policy_id`,
`sources` keyed by record type (`purchase_order`, `invoice`, `goods_receipt`,
`allocation_history`, `supplier_message`). Each `SourceSpec` has a `format` (`json`, `csv`,
`pdf_text`, `text`), a `collection` in the source system, `record` fields and optional
`lines`, plus `header_patterns`/`lines.pattern` (named regex groups) for text layouts,
`group_by` for CSV rows, `assertion_patterns` for supplier messages and `amount_check` for
invoice arithmetic. `FieldSpec`: one of `path` (JSON), `column` (CSV), `group` (regex), a
`transform` (`str`, `decimal`, `date`, `upper`, `lower`, `strip`), optional value `map`,
`default`, `optional`. Mapping problems (missing required field, failed amount check) mark
the document `needs_review`; values from it are not used.

## Policy (`backend/src/opsbridge/domain/policy.py`, `packs/<pack>/policies/*.yaml`)

`ReconciliationPolicy` (strict; unknown keys rejected): `policy_id`, `version`,
`effective_from` (selected by invoice date), `supported_currencies`, `price_tolerance`
(`absolute`, `percent`, `mode` either/both, `direction` over_only/both, basis net unit
price), `quantity_tolerance`, `price_rounding` (places, mode), `unit_aliases`,
`unit_conversions`, `source_precedence` (empty = conflicts escalate), `action_routes`
(per route: `action_type`, `assignee_group`, `title_template`), `allowed_actions`,
`approval` (`lifetime_minutes`, `separation_of_duties`), `data_routing`
(hosted_permitted, local_required, human_only). Policy versions are stored by digest; a new
version never rewrites findings made under an older one.

## Canonicalization and identifiers (`backend/src/opsbridge/canonical.py`)

`opsbridge-cjson/1`: UTF-8 JSON, sorted keys, compact separators, Decimals as plain strings;
digests are `sha256:<hex>`. Used for proposal payloads, source manifests, step inputs/outputs,
model/tool request and response digests and evaluation manifests. Raw document digests are
over the original bytes; normalized-text digests are separate (`opsbridge-norm/1`).

- Operation key: `opk-` + first 40 hex of SHA-256 over tenant, proposal key, version and
  payload digest — one per logical operation; attempts have their own ids.
- External marker: `opsbridge-op:<operation key>` in every created task, used for lookup.
- Proposal payload (`opsbridge.proposal/1`, `services/proposals.py::payload_of`): tenant,
  case, proposal key and version, action and parameters, policy and investigation versions,
  source-manifest digest, expiry and, since migration 0011, `destination`
  (`{connector, organization|repository}`; never credentials) and `outbound` (the title,
  body and assignee group exactly as sent, with `transform`
  `opsbridge-outbound/1+opsbridge-redact/2`; null for local actions). Proposals made before
  0011 omit both keys, so their digests still hold. A dispatch attempt's request records
  destination, title, the full body including the marker line, assignee group, marker and
  idempotency key.
- Investigation `evidence_state` (`services/freshness.py`, migration 0012):
  `{invoice_number, po_number, supplier_id, digest}`, the digest covering the versions
  matching would load, every version of the case's linked documents and the corrections on
  them, taken before matching.
- Business identifiers (`domain/identifiers.py`): trimmed, upper-cased, inner whitespace
  collapsed; punctuation and leading zeros kept. The similarity key (alphanumerics only,
  leading zeros folded) only prompts a person (`possible_duplicate`, overridden with
  `confirm_distinct`). Current invoice records are unique per tenant, supplier and number
  (migration 0013); a case may name `supplier_id`. Before 2026-10-22 intake removed every
  character except letters, digits and hyphens. An `Idempotency-Key` reused with a different
  request is refused (`idempotency_key_reused`); the key and a digest of the request are
  recorded when the case is created (migration 0014), and another key for an invoice that
  already has a case is a duplicate (`duplicate_case`).

## Purchasing source (`backend/src/opsbridge/connectors/source_contract.py`)

`purchasing-source/1`: `fetch(collection, key)` returns the original bytes, content type and
ETag, or None only when the source says the record does not exist; `list_for_po(collection,
po_number)` returns `Listing(keys, withdrawn, complete, pages)` after following every page.
Outages raise `SourceUnavailable`, unusable answers `SourceRecordInvalid`. The simulator's
listing route serves `{"items": [{"key", "content_type", "sha256", "updated_at",
"deleted"}], "next_cursor"}` in key order with `limit` and `cursor` parameters; deleted
records are tombstones. An investigation's `collect_context` output records `withdrawn`,
`incomplete` and `source` (contract, connector version, `read_at`); its `evidence_state`
adds `source_read_at` and `source_valid_until`. `source_documents.withdrawn_at` (0016) marks
documents the source no longer holds.

## API (`/api/v1`, OpenAPI at `/api/v1/openapi.json`)

Generated into `frontend/src/api/openapi.json` and `schema.d.ts` (`make openapi`). Errors are
`{"error": {"code", "message", "correlation_id", "retryable", "details"}}`; status codes
distinguish 401, 403, 404 (also for other tenants' resources), 409 (conflict, stale version,
not entitled), 411/413 (body), 422, 429, 503 (dependency). Writes need the CSRF header.

## Jobs and events

`jobs.payload`: `run_id` (investigate), `operation_id` (dispatch_operation),
`requested_by` (retention_sweep), plus `_trace` (W3C trace context) when a trace is active.
Outbox events are written in the approval transaction and marked delivered on confirmation.

## Evaluation artifacts

| Artifact | Schema id | Defined in |
|---|---|---|
| Scenario corpus (per split) | `schema_version: 1` (`Corpus`/`Scenario`) | `simulator/scenarios.py` |
| Frozen benchmark manifest | `opsbridge-eval-manifest/1` | `evaluation/manifest.py` |
| Acceptance policy | `opsbridge-acceptance/1` | `evals/acceptance-policy.json` |
| Model cassette | `opsbridge-model-cassette/1` | `evaluation/cassette.py` |
| Tool cassette | `opsbridge-tool-cassette/1` | `evaluation/cassette.py` |
| Results | `opsbridge-eval-results/1` | CLI `eval-bench` |
| Canary baseline | `opsbridge-canary/1` | `evaluation/canary.py` |
| Tool manifest / responses | `evalseal.agent-tools/1`, `evalseal.agent-tool-responses/1` | EvalSeal 2.2.0 (documented schemas) |

## Versioning rules

Changing a canonical record, a mapping semantic or the rules output bumps the relevant
version (`RULES_VERSION` in `domain/matching.py`, currently `opsbridge-rules/1.0.1`; pack and
policy versions in their YAML). Benchmark inputs are frozen by digest; changing them needs a
new benchmark id. Database changes go through Alembic migrations (0001–0004).
