# Pilot quickstart: a rules-only dry run

This runs OpsBridge the way the first pilot will:
- read-only, from an export (docs/pilot/export-contract.md);
- rules only;
- with every write outside OpsBridge refused by the worker itself.

It starts on the synthetic rehearsal export, which is checked in. A customer export takes the
rehearsal export's place only after the owner's written authorization (docs/pilot-plan.md);
none exists yet.

Checked from a clean clone of this branch on 17 November 2026 (macOS, Podman). That clone
reused the machine's running PostgreSQL and Keycloak and its `.env`; on a fresh machine,
`make setup` writes the `.env`.

## 1. From a clean checkout

    git clone https://github.com/patibandlavenkatamanideep/Ops_Bridge.git
    cd Ops_Bridge
    make doctor          # prerequisites; prints no secrets
    make setup           # locked dependencies; .env with generated local secrets
    make infra-up        # PostgreSQL :5433, Keycloak :8081, S3-compatible store :8333
    make migrate
    make seed-demo       # only the synthetic demo tenants

The normal demo (`make dev`, `make demo`) needs none of what follows: no export, no pilot
settings, no credentials.

## 2. Write the synthetic export and check it

    uv --directory backend run --frozen python -m opsbridge.cli pilot-rehearsal-export --out ../var/pilot/export

The first `uv --directory backend run` creates `backend/.venv` (make uses `.venv.nosync`).
Expected output:

    wrote the synthetic rehearsal export to ../var/pilot/export/demo-furniture (extracted_at now; not customer data)

It writes five synthetic invoices for the `demo-furniture` tenant under `var/`, which Git
ignores. It refuses to write over an existing export.

Turn the dry run on in `.env`. Relative paths there are relative to the checkout:

    OPSBRIDGE_PURCHASING_EXPORT_DIR=var/pilot/export
    OPSBRIDGE_OUTBOUND_WRITES=disabled

Then run the preflight:

    uv --directory backend run --frozen python -m opsbridge.cli export-check \
      --export ../var/pilot/export/demo-furniture --pack furniture \
      --out ../var/pilot/export-check.json

Expected output (exit status 1, because one invoice is blocked on purpose):

    ../var/pilot/export/demo-furniture: 13 records checked with furniture@v1 (extracted_at …, complete)
    This installation: outbound writes disabled; investigations read <checkout>/var/pilot/export
      loaded: invoices 5, messages 1, purchase_orders 4, receipts 3
      cases: 4 of 5 invoice(s) can be evaluated; 1 cannot be evaluated reliably
        INV-8405 (PO-4005): purchase order PO-4005 is not in this export, so an investigation will find no purchase order
        note, INV-8403: no receipt is exported for PO-4003: the investigation will report the receiving record as missing (needs information), never as zero received
      1 problem(s) to review before the first case
        invoices INV-8405: purchase order PO-4005 is not in this export, so an investigation will find no purchase order

## 3. Start it and confirm that nothing will be sent

    make dev             # API :8000, worker, MCP services, simulator, UI http://localhost:5173

Ask the running worker, the process that refuses the writes:

    curl -s http://127.0.0.1:9101/readyz

Expected:

    {"ready": true, "checks": {"loop": "ok", "database": "ok"}, "outbound_writes": "disabled", "purchasing_source": "export"}

`"outbound_writes": "enabled"` or `"purchasing_source": "simulator"` means the `.env` lines
were not read: stop and fix them before going on. The refusal is not a hidden button. The
worker and the Casework MCP tool both refuse the write where they would make it
(`services/execution_admission.py`). Tests:
- `tests/integration/test_approvals_execution.py` (worker);
- `tests/integration/test_mcp.py` (MCP boundary);
- `tests/integration/test_export_investigation.py` (the whole dry run).

## 4. Work the rehearsal cases

Sign in at http://localhost:5173 as `furn-investigator` (password:
`OPSBRIDGE_DEMO_USER_PASSWORD` in `.env`). Open each case with **New case**, entering the
invoice number. Run the investigation with **Model: Rules only (no model)**; or, as
`furn-admin`, set the tenant's model mode to rules only once.

| Invoice | What you should see |
|---|---|
| INV-8401 (scenario A) | Discrepancies found. "Invoice bills 20 EA on PO line 1, but only 18 were accepted at receiving (2 more were delivered and rejected, and rejected units are not counted as received); 2 EA exceed the allocatable quantity." A note: "Supplier correspondence claims 20 delivered on PO line 1, which agrees with receiving: 18 were accepted and 2 rejected." The evidence chips open the order line, the invoice line, both receipt rows with their status, and the supplier's sentence. Next step: a follow-up for **receiving**, "Confirm delivered quantity for PO-4001", which the page says is not a resolution |
| INV-8403 | Needs information: "No receiving record is available for PO PO-4003 line 1. This does not establish whether the goods were delivered." Next step: ask receiving for the record |
| INV-8405 | Needs information: "Purchase order PO-4005 referenced by the invoice is not available in the authorized sources." The preflight named this case before it was opened |
| INV-8404 | Matched; a review note |
| INV-8402 | A quantity mismatch (8 of 10 received). Scenario B, a receipt corrected after approval, needs the purchasing source to change and is run against the simulator: `tests/integration/test_evidence_freshness.py::test_pilot_scenario_b_a_receipt_corrected_after_approval_is_not_acted_on` |

Then on INV-8401, open **Proposals & execution** and choose **Create proposal from the
validated draft**. Sign in as `furn-approver` in another browser profile and **Approve** it.
The operation reads **cancelled before dispatch**, category **outbound_writes_disabled**. The
approval itself stands and counts in the pilot report. The simulator's task tracker stays
empty: on the checked run, `var/simulator.sqlite3` held 0 tasks after two approvals.

## 5. Reset

- Stop `make dev` with Ctrl-C.
- Remove the export and the preflight report: `rm -rf var/pilot`.
- Remove the two dry-run lines from `.env`. `make dev` then reads the simulator again and can
  send to its task tracker, as the normal demo does.
- The cases you opened stay in the local demo database. To delete all local demo data
  (destructive; development only): `make reset-demo CONFIRM=delete-local-demo-data`, then
  `make infra-up migrate seed-demo`.

## 6. With a customer export (after authorization only)

1. Agree the case selection, write the cohort file, and complete the pre-registration in
   docs/pilot/results-template.md before opening any case.
2. Put the export outside the repository, or under `var/`, as `<dir>/<tenant slug>/`. Run
   `export-check` on it in the image with the network off (docs/pilot-plan.md), and resolve or
   accept every problem it lists.
3. The pilot needs a tenant for the organization and the reviewer's sign-in. Only the
   synthetic demo tenants can be seeded today; provisioning a pilot tenant is a step for the
   owner (docs/integration-onboarding.md, section 6).
4. If the export is older than `OPSBRIDGE_SOURCE_MAX_AGE_HOURS` (24), its results cannot be
   proposed or approved. Raise the limit to cover the export's age and the session, and record
   the value.
5. The reviewer fills Part 1 of docs/pilot/reviewer-sheet.md for each invoice **before** its
   case is opened, then works the case and fills Part 2.
