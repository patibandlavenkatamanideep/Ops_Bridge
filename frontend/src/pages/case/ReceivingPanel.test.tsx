import { screen, within } from "@testing-library/react";
import { ReceivingPanel } from "./ReceivingPanel";
import { json, mockFetch, renderAt } from "../../test/utils";
import type { Receiving } from "../../api/client";

afterEach(() => vi.unstubAllGlobals());

const ref = { document_id: "d1", version_id: "v1", sha256: "sha256:0", locator: "csv:row=3;col=reason",
  field: "receipt_line.reason" };

const line: Receiving["lines"][number] = {
  po_line_number: "1", unit: "EA", ordered: "20", delivered: "20", accepted: "18", rejected: "2",
  undelivered: "0", returned_after_acceptance: "0", returned: "0", replacement_promised: "0",
  replacement_delivered: "0", replacement_accepted: "0", credit_promised: "0", credit_posted: "0",
  credits_elsewhere: [], covered: "18", open_rejected: "2", disposition: "unrecorded",
  invoiced: "20", matched: "18", disputed: "2",
  rejections: [{ source: "REJ-4710", receipt_number: "GR-4710", quantity: "2", reason: "damaged in transit",
    occurred: "2026-10-20", occurred_precision: "date", late: true, evidence: [ref] }],
  problems: [], evidence: [ref],
};

const view: Receiving = {
  invoice_number: "INV-4710", po_number: "PO-4710", investigation_version: 2,
  rules_version: "opsbridge-rules/1.4.0", policy_ref: "furniture-ap@v2",
  lines: [line], unattached_events: [],
  split: { hold: "unconfigured", matched_quantity: "18", disputed_quantity: "2", unit: "EA",
    summary: "Policy furniture-ap@v2 does not say whether a disputed quantity holds the whole invoice." },
  question: { decision: "escalate", action_type: "assign_case", owner: "ap-review", owner_configured: false,
    title: "Review CASE-00001: 2 condition(s) need manual review",
    basis: [{ finding_id: "manual_review:header:disposition_owner_unconfigured",
      reason_code: "disposition_owner_unconfigured", summary: "Policy furniture-ap@v2 names no owner for rejected goods." }] },
  timeline: [
    { kind: "receipt", id: "GR-4710", summary: "line 1: 20 EA receipt accepted", occurred: "2026-10-20",
      occurred_precision: "date", recorded_at: "2026-11-17T09:00:00+00:00", current: true, superseded_at: null,
      after_approval: false, document_id: "d0", document_title: "goods receipt GR-4710", version_id: "v0",
      version_no: 1, withdrawn: false },
    { kind: "approval", id: "p1", summary: "approved create follow up task (proposal version 1)", occurred: null,
      occurred_precision: "unknown", recorded_at: "2026-11-17T09:05:00+00:00", current: true, after_approval: false },
    { kind: "event", id: "REJ-4710", event_type: "rejection", summary: "rejection of 2 EA on PO line 1 (damaged in transit)",
      occurred: "2026-10-20", occurred_precision: "date", recorded_at: "2026-11-17T09:10:00+00:00", current: true,
      superseded_at: null, after_approval: true, document_id: "d1", document_title: "receiving event PO-4710-EVT-LATE",
      version_id: "v1", version_no: 1, withdrawn: false },
  ],
  since_approval: [{ proposal_id: "p1", action_type: "create_follow_up_task", proposal_status: "superseded",
    approved_at: "2026-11-17T09:05:00+00:00", operation_status: "cancelled_before_dispatch",
    changes: [{ change: "added", document_title: "receiving event PO-4710-EVT-LATE", kind: "receiving_event",
      version_no: 1, recorded_at: "2026-11-17T09:10:00+00:00" }],
    requires: "review: the approval does not carry over to the changed evidence" }],
};

function row(table: HTMLElement, label: string) {
  return within(table).getByRole("row", { name: new RegExp(`^${label}`) });
}

test("shows delivered, accepted and rejected apart, with the rejection's reason and dates", async () => {
  mockFetch(() => json(200, view));
  renderAt("/t/demo-furniture/cases/c1", "/t/:tenant/cases/:caseId", <ReceivingPanel tenant="demo-furniture" caseId="c1" />);
  const table = await screen.findByRole("table", { name: /PO line 1 \(EA\): Rejected; no return, credit or replacement recorded/ });
  expect(row(table, "Delivered")).toHaveTextContent("20 EA");
  expect(row(table, "Accepted at receiving")).toHaveTextContent("18 EA");
  expect(row(table, "Rejected at receiving")).toHaveTextContent("2 EA");
  expect(row(table, "Not delivered")).toHaveTextContent("0 EA");
  expect(row(table, "Disputed")).toHaveTextContent("2 EA");
  expect(screen.getByText(/damaged in transit/, { selector: "li" })).toHaveTextContent("recorded as REJ-4710, after the receipt");
  expect(screen.getByText(/No owner is configured for rejected goods/)).toBeInTheDocument();
  expect(screen.getByText(/payment recommendation unresolved/)).toBeInTheDocument();
});

test("shows what changed since approval and what was learned after it", async () => {
  mockFetch(() => json(200, view));
  renderAt("/t/demo-furniture/cases/c1", "/t/:tenant/cases/:caseId", <ReceivingPanel tenant="demo-furniture" caseId="c1" />);
  expect(await screen.findByText(/the approval does not carry over/)).toBeInTheDocument();
  expect(screen.getByText(/added: receiving event PO-4710-EVT-LATE version 1/)).toBeInTheDocument();
  const timeline = screen.getByRole("table", { name: /by when OpsBridge recorded them/ });
  const learned = within(timeline).getByRole("row", { name: /REJ-4710/ });
  expect(learned).toHaveTextContent("learned after approval");
  expect(learned).toHaveTextContent("2026-10-20 (date only)");
  expect(within(timeline).getByRole("row", { name: /GR-4710/ })).not.toHaveTextContent("learned after approval");
});

test("an unknown quantity reads unknown, never zero", async () => {
  mockFetch(() => json(200, { ...view, lines: [{ ...line, accepted: null, covered: null, disposition: "none",
    rejected: "0", rejections: [] }] }));
  renderAt("/t/demo-furniture/cases/c1", "/t/:tenant/cases/:caseId", <ReceivingPanel tenant="demo-furniture" caseId="c1" />);
  const table = await screen.findByRole("table", { name: /PO line 1/ });
  expect(row(table, "Accepted at receiving")).toHaveTextContent("unknown");
});
