# Pilot export contract

What a purchasing export must hold for OpsBridge to investigate blocked invoices from it, how
to check one before the first case, and how to keep it private. This describes what the code
reads today (`connectors/exports.py`, `packs/*/pack.yaml`, `pilot/export_check.py`). It has
been tested on hand-written and synthetic exports only. No customer export has been read, and
no ERP is supported by name: a system is usable when its export can be written in this shape.

## The envelope: `opsbridge-export/1`

One directory per tenant, named by the tenant's slug, under the directory that
`OPSBRIDGE_PURCHASING_EXPORT_DIR` names:

    <export dir>/<tenant slug>/manifest.json
    <export dir>/<tenant slug>/<the files the manifest lists>

`manifest.json`:

| Field | Required | Meaning |
|---|---|---|
| `schema` | yes | Exactly `opsbridge-export/1` |
| `source_system` | yes | The system and version that produced it (free text) |
| `extracted_at` | yes | The one moment the snapshot shows, RFC 3339 with a zone (`2026-11-10T09:00:00+00:00` or `…Z`) |
| `complete` | yes | `true` if every record in the agreed scope is in the export. Use `true` for a pilot (see "Missing records") |
| `records` | yes | One entry per record (below) |
| `withdrawn` | no | Records the source deleted, voided or reversed: `collection`, `key`, `po_number` |

Each `records` entry:

| Field | Required | Meaning |
|---|---|---|
| `collection` | yes | `purchase_orders`, `invoices`, `receipts`, `receiving_events`, `allocations` or `messages` (lower-case letters and `_`) |
| `key` | yes | The record's identifier: an invoice's number, a purchase order's number, a receipt's or message's own id. Letters, digits, `.`, `_`, `-`, up to 120 |
| `po_number` | for receipts, receiving events, allocations and messages | The purchase order it belongs to. A case reads these records by listing its order's; one listed under no order, or under another, is never read |
| `file` | yes | A path relative to the tenant's directory, inside it |
| `content_type` | yes | `application/json`, `text/csv`, `application/pdf` (with a text layer) or `text/plain`, as the pack reads that collection |
| `sha256` | yes | The SHA-256 of the file's bytes, in lower-case hex |
| `modified_at` | no | When the source last changed the record; one later than `extracted_at` refuses the export as a mixed snapshot |

Limits: a manifest up to 5 MiB, up to 10,000 records, and each file up to 5 MiB.

The export is refused as a whole in these cases:
- an unknown schema;
- a file outside the directory, missing, or not matching its digest;
- a record listed twice, or listed both as current and withdrawn;
- a mixed snapshot;
- an `extracted_at` more than 5 minutes ahead of this host's clock.

## The records: what each pack reads

A pack maps the system's own field names and formats; the core never names a customer.
Two packs exist. A new system needs its own mapping (docs/integration-onboarding.md), which is
format mapping, not a change to the rules.

**`furniture`** (snake_case JSON orders, CSV receipts, text-layer PDF invoices, JSON
allocations, plain-text supplier messages):

| Record | Required | Optional |
|---|---|---|
| Purchase order (JSON) | `po_number`, `supplier.id`, `supplier.name`, `currency`, `status` (`OPEN`, `CLOSED`, `CANCELLED`); per line `line_no`, `qty_ordered`, `uom`, `unit_price` | per line `sku`, `description`, `discount` (default 0) |
| Invoice (PDF with a text layer, one supplier layout) | `Invoice No:`, `Invoice Date:` (YYYY-MM-DD), `Supplier:`, `Currency:`, `Total:`; per line number, description, quantity, unit, unit price, discount, `GOODS` or `CHARGE`, amount | `Supplier ID:`, `PO Reference:`; per line the PO line and SKU |
| Receipt (CSV, one row per movement) | `receipt_id`, `po_ref`, `po_line`, `received_on` (YYYY-MM-DD), `qty`, `uom`, `movement` (`RECEIPT` or `RETURN`), `status` (`ACCEPTED`, `REJECTED`, `REVERSED`) | `reason` (why receiving rejected the units) |
| Receiving event (CSV, one row per event; docs/receiving-disposition.md, section 3) | `event_id`, `event`, `po_ref`, `po_line`; `qty` and `uom` for rejections, returns, replacement deliveries and acceptances and posted credits; what it refers to (`receipt_id` or `follows`) | `occurred_on` (date or zoned time), `invoice` and `document` (a posted credit names both), `reason` |
| Allocation history (JSON) | per entry `invoice`, `po`, `po_line`, `qty`, `state` (`MATCHED`, `CANCELLED`) | none |
| Supplier message (text) | none: a sentence such as "delivered all 20 chairs … PO-4001" is read as a delivery claim, anything else is kept as text | |

**`it_hardware`** reads camelCase JSON for orders, invoices, receipts and allocations, and plain
text for vendor messages (`packs/it_hardware/pack.yaml` lists every path).

Identifiers and line references:
- An invoice's and an order's `key` must be the number the document carries; the preflight
  checks this.
- An invoice line should name its order line. Without one, it is matched by a unique item
  code and says so; otherwise the case goes to review.

Units and dates:
- A unit must be one the tenant's policy knows (`unit_aliases`). A conversion must be
  configured (`unit_conversions`). Anything else goes to review; it is never guessed.
- Dates are calendar dates.
- The invoice date chooses the policy version in effect.

How receipts count (rules `opsbridge-rules/1.4.0`, docs/receiving-disposition.md):
- Only `ACCEPTED` units count as accepted, net of accepted returns and of units a later
  rejection event names.
- `REJECTED` units are delivered and rejected. Their disposition is asked of the policy's
  disposition owner, or of receiving when no reason is recorded.
- `REVERSED` movements are ignored.
- A receipt without an acceptance status is delivered, never accepted: the case asks for the
  acceptance.
- Receiving events are optional; an export without any is read as before.

## Missing records: never zero, never a silent match

What a missing record means depends on whether the export says it is complete (`complete`):

| Situation | What a case does |
|---|---|
| Complete export, no receipt for the order | `needs_information`: "No receiving record is available …; this does not establish whether the goods were delivered." The follow-up asks receiving for it |
| Complete export, the invoice's order is not there | `needs_information`: no order, nothing matched |
| Partial export (`complete: false`) | Absence is unknown, so every case goes to manual review. The finding's wording was written for paged sources and speaks of pages |
| A record that cannot be read (bad digest, unparsable, a required field blank) | That record is not used and the case goes to review; a blank quantity is never zero |
| A record listed as withdrawn | "No longer in the purchasing system"; not used |

The data is as old as the export. An investigation records `extracted_at` as its read time,
and its result may be acted on for `OPSBRIDGE_SOURCE_MAX_AGE_HOURS` (24) after that. For a dry
run on an export older than that, raise the limit to cover the export's age and the session.
Record the value in the pre-registration (docs/pilot/results-template.md). Nothing can be sent
in a dry run.

## Preflight: `export-check`

Run it before the first case, on the export exactly as delivered:

    uv --directory backend run python -m opsbridge.cli export-check \
      --export <export dir>/<tenant slug> --pack <pack id> --out <check.json>

It reads every record the way an investigation does and stores nothing. It reports:
- **What was loaded:** records that parsed and mapped, by collection.
- **Which cases can be evaluated:** for each invoice, `evaluable` or the reasons it cannot be
  evaluated reliably. Reasons include the invoice or its order unreadable, a record listed
  under that order with a problem, the order missing, and a partial export. Notes, such as no
  receipt exported, say what the case will ask for.
- **What could not be linked:** records listed under no order, or under one the export lacks.
- **Problems:** every record-level problem, by collection and key.
- **This installation:** whether outbound writes are enabled and where investigations read
  purchasing data. This describes the environment the command runs in; the running worker
  reports its own at `/readyz` (docs/pilot/quickstart.md).

Exit status: 0 when every record and every case is fine, 1 when something needs review, 2 when
the export or the pack cannot be opened. A problem can quote a value from a document, so the
report stays where the export is kept. On macOS the parsers are not confined; for a customer
export, run the check in the image with the network off (docs/pilot-plan.md).

Its output on the synthetic rehearsal export (`pilot-rehearsal-export`, five invoices, one of
them naming an order the export lacks):

    …/demo-furniture: 13 records checked with furniture@v1 (extracted_at …, complete)
    This installation: outbound writes disabled; investigations read …/var/pilot/export
      loaded: invoices 5, messages 1, purchase_orders 4, receipts 3
      cases: 4 of 5 invoice(s) can be evaluated; 1 cannot be evaluated reliably
        INV-8405 (PO-4005): purchase order PO-4005 is not in this export, so an investigation will find no purchase order
        note, INV-8403: no receipt is exported for PO-4003: the investigation will report the receiving record as missing (needs information), never as zero received
      1 problem(s) to review before the first case
        invoices INV-8405: purchase order PO-4005 is not in this export, so an investigation will find no purchase order

## A small synthetic example

From the rehearsal export (`fixtures/pilot/rehearsal.yaml`, case `pilot-a`; synthetic):

`manifest.json` (two of its thirteen records):

    {
      "schema": "opsbridge-export/1",
      "source_system": "OpsBridge synthetic pilot rehearsal (not a customer export)",
      "extracted_at": "2026-11-17T12:00:00+00:00",
      "complete": true,
      "records": [
        {"collection": "purchase_orders", "key": "PO-4001", "po_number": "PO-4001",
         "file": "purchase_orders/PO-4001.json", "content_type": "application/json",
         "sha256": "<64 hex>"},
        {"collection": "receipts", "key": "PO-4001-GR", "po_number": "PO-4001",
         "file": "receipts/PO-4001-GR.csv", "content_type": "text/csv", "sha256": "<64 hex>"}
      ],
      "withdrawn": []
    }

`receipts/PO-4001-GR.csv`:

    receipt_id,po_ref,po_line,received_on,qty,uom,movement,status
    GR-6401A,PO-4001,1,2026-10-20,18,EA,RECEIPT,ACCEPTED
    GR-6401A,PO-4001,1,2026-10-20,2,EA,RECEIPT,REJECTED

`messages/MSG-P4010.txt`:

    Hello AP team,
    We delivered all 20 chairs for PO-4001 on 20 October. Please pay the full invoice.
    Regards, Acme billing

The order (`purchase_orders/PO-4001.json`) is 20 `CH-100` chairs at 185.00 USD. The invoice
(`invoices/INV-8401.pdf`) bills 20.

## Keeping the data private

- Keep the export, the preflight report, the cohort and reference files, and the reviewer's
  notes outside the repository, or under `var/`, which Git ignores. Never commit them, attach
  them to an issue or pull request, or upload them as a CI artifact. CI runs on synthetic data
  only.
- `scripts/check_secrets.py` checks for local secrets, not for documents: check `git status`
  before every commit during a pilot.
- The code avoids logging document content, and a filter scrubs tokens, keys and identifier
  patterns from every process's logs (docs/privacy-and-retention.md). That is pattern
  redaction, not anonymization: treat logs from a pilot session as pilot data too.
- Take no screenshots of real cases for documentation. Use the rehearsal cases.
- Results leave the agreed environment only as counts and examples the owner approves
  (docs/pilot/results-template.md).
