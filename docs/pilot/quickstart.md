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

Keep outbound writes disabled with an export. An export cannot show what the purchasing
system holds now, so even with writes enabled every follow-up task would be held for review
(`source_unverified`) and none would be sent.

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
| INV-8401 (scenario A) | Needs information. "2 EA delivered on PO line 1 were rejected at receiving (GR-6401A), and no reason is recorded for the rejection. Ask receiving for the inspection record before the supplier is asked for a remedy." A note: "Supplier correspondence claims 20 delivered on PO line 1, which agrees with receiving: 18 were accepted and 2 rejected." The evidence chips open the order line, the invoice line, both receipt rows with their status, and the supplier's sentence. The **Receiving** tab shows 20 delivered, 18 accepted, 2 rejected, 0 not delivered, and 18 matched and 2 disputed. Next step: a follow-up for **receiving**, "Provide receiving record for PO-4001". Rules 1.4.0 (docs/receiving-disposition.md); the rehearsal's receipt format has no reason column |
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
3. The pilot needs its own tenant. Never use a demo tenant: demo accounts share one password.
   - Copy `fixtures/pilot/tenant.example.yaml` outside Git (for example
     `var/pilot/tenant.yaml`) and fill in every `<<FILL…>>` value:
     - the slug, which also names the export folder (`<dir>/<slug>/`);
     - the identity provider's issuer, and the first administrator's subject;
     - the organization's agreed policy file. The demo's policy is not theirs.
   - Check it, then provision. The dry run writes nothing; any refusal writes nothing:

         uv --directory backend run --frozen python -m opsbridge.cli tenant-provision --spec ../var/pilot/tenant.yaml --dry-run
         uv --directory backend run --frozen python -m opsbridge.cli tenant-provision --spec ../var/pilot/tenant.yaml

   - The report's `tenant_id` is what `pilot-report --tenant-id` takes (docs/pilot-plan.md).
   - The administrator signs in and invites the reviewer from **Settings** (an invitation
     link, accepted with the address their identity provider verified). Members, roles and
     the model mode are managed there, and each change is audited.
   - Re-running publishes policy versions added to the spec, and leaves everything else as it
     is. A changed policy is published as a new version.
   - `make reset-demo` deletes the whole local database, a provisioned tenant included.
4. If the export is older than `OPSBRIDGE_SOURCE_MAX_AGE_HOURS` (24), its results cannot be
   proposed or approved. Raise the limit to cover the export's age and the session, and record
   the value.
5. The reviewer fills Part 1 of docs/pilot/reviewer-sheet.md for each invoice **before** its
   case is opened, then works the case and fills Part 2.
