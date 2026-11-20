# Reviewer sheet (one per invoice)

Print or copy one sheet per sampled invoice. **Part 1 is written before the case is opened in
OpsBridge** and is never changed afterwards. Part 2 is written after, and disagreement is
recorded beside the first answer, not instead of it. The sheet holds customer data: it stays
in the agreed environment and is never committed (docs/pilot/export-contract.md). Its Part 1
answers become one row of the reference file (`docs/pilot/templates/reference.csv`,
docs/pilot-plan.md).

**Invoice** (as it will be entered at intake): ____________ **Supplier id:** ____________

## Part 1: your own reading, before OpsBridge

**Recorded at** (date, time and time zone): ______________________

What the documents establish (quantities, prices, dates; name the document and line or row
you read each from):

| | Quantity or amount | From (document, line or row) |
|---|---|---|
| Ordered | | |
| Delivered | | |
| Accepted at receiving | | |
| Rejected or returned | | |
| Billed | | |
| Price ordered and billed | | |
| Supplier's statement, if any | | |

What remains unresolved: ______________________________________________________

Your outcome (tick one):

- [ ] Matched: nothing to follow up (`matched`)
- [ ] A discrepancy the documents show (`discrepancies_found`), type:
      [ ] missing receipt [ ] quantity [ ] price
- [ ] **Insufficient evidence:** something needed is missing (`needs_information`)
- [ ] **Ambiguous policy or conflicting sources:** a person must decide (`manual_review`)

Next action and whom to contact (tick one, then name the owner):

- [ ] Record a review note: nothing to send (`no_discrepancy:record_review_note`)
- [ ] Internal follow-up (`propose_action:create_follow_up_task`), owner: ____________
- [ ] Supplier query (`propose_action:create_supplier_message_draft`), owner: ____________
- [ ] Ask for the missing information (`request_information:create_follow_up_task`),
      from: ____________
- [ ] A person reviews it (`escalate:assign_case`), who: ____________
- [ ] **Owner unknown**, because: ________________________________________________

"Insufficient evidence", "ambiguous policy" and "owner unknown" are full answers, not gaps.

## Part 2: after seeing OpsBridge's result

OpsBridge's outcome: ____________ next action: ____________ owner: ____________

| Question | Answer |
|---|---|
| Does OpsBridge's next action match yours? | yes / no |
| Was each figure you relied on cited, and does each citation show the passage it claims? | all / some (which?) / none |
| Did OpsBridge cite anything wrong or leave out something decisive? | |
| Would you approve its drafted follow-up as written? | as written / with changes (which?) / no |
| **Correction required** | none / a field read wrongly / the pack mapping / the policy / a rule |
| Reason (one or two sentences) | |
| Having seen OpsBridge, would you change your own Part 1 answer? (Do not edit Part 1.) | no / yes, to: |

## Timing (only if agreed in the pre-registration)

- **Start:** the moment you open the case in OpsBridge (intake records it).
- **Finish:** the moment you record the decision, an approval or your "no follow-up" note.

Write down nothing else about time. Times for the manual arm come from that process's own
records (docs/pilot-plan.md), not from this sheet.
