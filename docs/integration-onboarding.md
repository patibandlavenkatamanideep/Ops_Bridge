# Integration onboarding

How to bring a new organization or system into OpsBridge without changing the core. Every
organization-specific detail lives in a pack, a policy, a tenant record and (for new
systems) an adapter behind an existing interface. `tests/portability/` fails if core code
names a specific organization.

## 1. Discovery (with the customer's AP lead)

- Which exceptions block invoices, and what the team does next for each (docs/discovery.md
  lists the questions). Who may approve what; whether separation of duties is required.
- Source systems and formats for purchase orders, goods receipts, invoices (structured
  export or supplier PDFs), prior invoice allocations and supplier messages.
- Tolerances, units and conversions, currencies, source precedence, approval lifetime,
  whether data may go to a hosted model (`data_routing`).
- 20+ past exceptions they can label independently (validation set), and a separate set for
  held-out evaluation.

## 2. Policy (`packs/<pack>/policies/<id>-v1.yaml`)

Write the matching rules as configuration: `supported_currencies`, `price_tolerance`
(absolute/percent, either/both, over-only/both), `quantity_tolerance`, `price_rounding`,
`unit_aliases`, `unit_conversions`, `source_precedence` (leave empty if the customer has no
rule — conflicts then escalate), `action_routes` with assignee groups and title templates,
`allowed_actions`, `approval`, `data_routing`. Policies are versioned by `effective_from`;
never edit a published version — add a new one. Schema: docs/data-contracts.md.

## 3. Pack mapping (`packs/<pack>/pack.yaml`)

For each record type, describe the native format: JSON paths, CSV columns, or regex groups
for text/PDF layouts, with transforms and value maps. Start from `packs/furniture` (JSON,
CSV, PDF text, email assertions) or `packs/it_hardware` (camelCase JSON). Rules of thumb:
mark fields `optional` only when the business truly allows them to be missing; enable
`amount_check` for invoice PDFs; one layout per supplier template — unknown layouts go to
manual review rather than being guessed.

Check with: `tests/evidence/test_mapping.py`-style tests on real (approved) or synthetic
samples in the customer's format, and `opsbridge eval-rules --corpus <their validation set>`.

## 4. Source system adapter

The workflow fetches records through `PurchasingClient` (`connectors/purchasing.py`:
`list_keys(collection, po_number)` and `fetch(collection, key)` returning bytes, content
type and version). Today it talks to the simulator's HTTP API. For a real ERP, implement the
same two operations against its API or export location with a read-only service account,
map ERP collections to the pack's `collection` names, return the original bytes (digests are
taken over them), and raise `SourceUnavailable` for outages (retried) versus "not found"
(becomes needs-information). Wire it in `workflow/worker.py::build_investigation_deps`
(`purchasing_for`). Add contract tests against a fake written from the ERP's documentation,
plus a recorded sandbox run when authorized.

## 5. Task connector (follow-up work)

Implement `TaskConnector` (`connectors/tasks.py`): `create`, `get` (read-back) and
`find_by_marker`, and declare `capabilities` honestly — idempotency keys, lookup
consistency (strong, eventual or none), which HTTP statuses are definite vs ambiguous. If
the provider advertises capabilities, discover them per dispatch and assume the weakest
contract when discovery fails (as `SimTaskConnector` does); a capability the provider does
not actually honour can produce duplicates. Include the operation marker in every created
item. Restrict writes to an allow-listed project and an explicit "writes authorized" setting
(see `connectors/github.py`). Contract-test against a fake from the provider's documentation;
run the live test only with the owner's authorization.

## 6. Tenant, identity and access

Add the tenant to the seed fixture or provision it through an admin process: slug, tier,
pack, policies, members with roles and approval scopes. Connect the customer's OIDC
provider (issuer, client, redirect URIs); OpsBridge derives tenant and roles from its own
membership records, never from token claims or headers.

## 7. Prove it before go-live

- `make verify` and `make eval-canary` stay green (core unchanged).
- Their validation set through `eval-rules`; fix mappings/policy, not labels.
- Freeze and run a held-out set (`eval-freeze`, `eval-bench`) with their labels.
- Run the demo script against their pack in a non-production environment.
- Follow docs/pilot-plan.md for anything involving real users.
