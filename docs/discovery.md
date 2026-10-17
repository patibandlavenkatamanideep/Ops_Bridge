# Discovery: blocked procurement exceptions

Status: **assumptions only**. No stakeholder interviews, operator observations, or
customer data have been collected for this project. Everything below is a working
hypothesis drawn from the build brief and public descriptions of three-way matching.
Each assumption is labelled so it can be confirmed or replaced during a pilot.

## 1. Problem statement

An accounts-payable (AP) or procurement operator receives an invoice that the
purchasing system will not release for payment. They must answer:

> Why is this invoice or purchase blocked, what evidence supports the explanation,
> and what should happen next?

Today (assumed, A-01) the operator opens the purchasing system, the receiving log,
email threads with the supplier, and the AP policy document in separate tools, compares
quantities and prices by hand, and then emails a colleague or opens a ticket. The answer
is rarely written down with its evidence, so the next person repeats the search.

OpsBridge supports that investigation and the follow-up step. It does **not** post
invoices, pay suppliers, change purchase orders, or change bank details.

## 2. Users and roles

| Role | Goal in OpsBridge | Must not be able to |
|---|---|---|
| Requester | Raise a case about a blocked invoice; see its status and outcome | See other tenants' data; approve actions |
| Investigator | Review evidence, correct extraction, prepare a resolution proposal | Approve their own proposal when separation of duties applies |
| Approver | Approve or reject permitted actions within their scope | Approve action types outside their scope |
| Tenant administrator | Manage members, connector configuration, policy versions | Read document content or approve actions by virtue of the admin role alone |
| Auditor | Inspect permitted history, approvals, attempts, and evaluation receipts | Create, approve, or execute actions |
| Worker / MCP service identity | Run investigation steps and approved operations | Act outside the human principal's recorded authority |

A-02: one person can hold several roles; authorization is the union of role
permissions, constrained by separation-of-duties checks.

## 3. Workflow map (assumed current state → supported state)

```text
Invoice blocked in purchasing system
  → operator gathers PO, invoice, receipts, supplier emails, policy   [manual today]
  → operator compares quantities/prices                               [manual today]
  → operator decides: request info / escalate / open follow-up task   [manual today]
  → follow-up task worked by receiving, buyer, or supplier            [outside OpsBridge]
  → new evidence arrives (receipt posted, credit note)                [outside OpsBridge]
  → purchasing system re-matches and releases invoice                 [outside OpsBridge]
```

OpsBridge covers the first three steps and the creation of an approved follow-up
work item. Three outcomes are kept separate:

1. **Investigation completed** — findings and missing information documented with evidence.
2. **Follow-up completed** — an authorized task or draft was created and read back.
3. **Business exception resolved** — new evidence satisfies the reconciliation policy.

Creating a ticket is outcome 2, never outcome 3.

## 4. Discrepancy types in scope

| Type | Detection rule (deterministic) | Missing-data behaviour |
|---|---|---|
| Missing receipt | Invoice line references a PO line with no valid receiving record | `needs_information`; never "goods not delivered" |
| Quantity mismatch | Invoiced quantity exceeds allocatable received quantity (accepted receipts − returns − prior allocations) beyond policy tolerance | Unknown/ambiguous unit → manual review |
| Price mismatch | Invoice net unit price differs from PO net unit price beyond configured absolute/percent tolerance | Zero-price PO, unsupported currency → manual review |

## 5. Representative case (first vertical slice)

Furniture organization (synthetic): PO `PO-1001` orders 20 task chairs at 185.00 USD
each from supplier `SUP-ACME`. Receiving record `GR-5001` accepts 18. Invoice
`INV-9001` bills 20 at 185.00 USD. A supplier email claims all 20 were delivered.

Expected result: quantity mismatch of 2 units; citations to the PO line, the receipt
line, the invoice line, and the email; the conflict between the email and the receipt
is shown, and the receiving record controls because the furniture policy defines that
precedence. Proposed next action: create a follow-up task for the receiving team to
confirm the missing 2 chairs. The case remains open after the task is created.

## 6. Success criteria (prototype targets, not measured results)

- Correct discrepancy type(s) and next action on synthetic held-out cases (target ≥ 90 %
  case-level, see `docs/evaluation-plan.md`; a prototype design target only).
- Every decisive statement cites a source version, or is labelled uncertain.
- No unauthorized writes and no duplicate external effects in the declared trial set.
- Time from case intake to an approved follow-up is measured per case; operator time
  savings are claimed only after timed trials with real participants.

## 7. Baseline measures to collect in a pilot

- Median and p90 time from "invoice blocked" to "first correct explanation written".
- Share of investigations that required a second person to re-gather evidence.
- Number of follow-up tasks opened per blocked invoice; share later found unnecessary.
- Share of blocked invoices resolved within N days after the follow-up task.

None of these have been measured. They are listed so a pilot can collect them.

## 8. Interview questions (not yet asked)

1. Walk me through the last blocked invoice you handled. Which systems did you open?
2. Which document wins when the receiving log and the supplier disagree?
3. What price/quantity tolerances does your policy define? Absolute, percent, or both?
4. Who is allowed to approve a follow-up request to a supplier? Is there a dollar limit?
5. Which data must never leave your network (supplier bank details, employee names)?
6. How do you record that an exception is resolved, and who decides?
7. How often do returns or partial deliveries make the arithmetic ambiguous?
8. What would make you distrust an automated explanation?

## 9. Assumption register

| ID | Assumption | Status | How to validate |
|---|---|---|---|
| A-01 | Operators assemble evidence manually across several tools | unverified | Shadow two operators for one week |
| A-02 | Roles combine additively with separation-of-duties limits | unverified | Review client role matrix |
| A-03 | Receiving records control over supplier messages in furniture pack | product choice for demo | Confirm with AP policy owner |
| A-04 | IT hardware pack has **no** defined precedence → conflicts escalate | product choice for demo | Confirm with IT procurement lead |
| A-05 | Price comparison basis is net unit price after line discount, before tax | product choice | Confirm with controller |
| A-06 | Multi-currency invoices are rare enough to route to manual review | unverified | Pull 3 months of exception reasons |
| A-07 | A follow-up ticket in a task system is an acceptable first action | unverified | Confirm with AP lead |
| A-08 | 30-minute approval lifetime is acceptable for the demo | product choice | Confirm with approvers |
| A-09 | Synthetic data reflects realistic document variety | unverified; limited | Compare against redacted samples in a pilot |

## 10. Data boundary for the first slice

- Synthetic data only, generated by scripts in this repository and labelled synthetic.
- No real supplier names, people, bank details, or customer documents.
- No paid model calls; the default model adapter is a deterministic mock.
- The purchasing system is a local simulator; the GitHub sandbox connector writes only to
  an explicitly configured demo repository and only when credentials and authorization exist.
