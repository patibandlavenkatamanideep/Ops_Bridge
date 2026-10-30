import { ApiError, apiAs } from "./client";
import { validate } from "./validate";
import { json, mockFetch } from "../test/utils";

afterEach(() => vi.unstubAllGlobals());

function omit(obj: Record<string, unknown>, key: string): Record<string, unknown> {
  return Object.fromEntries(Object.entries(obj).filter(([k]) => k !== key));
}

const caseOut = {
  id: "c1", case_number: "CASE-00001", title: "INV-9001 blocked", description: "", invoice_number: "INV-9001",
  po_number: null, supplier_id: null, status: "new", discrepancy_types: [], evidence_readiness: "not_collected",
  requester_id: "u1", assignee_id: null, assignee_group: null, created_at: "2026-10-26T10:00:00Z",
  updated_at: "2026-10-26T10:00:00Z", row_version: 1,
};

test("a response that matches its schema passes, with fields a newer server added", () => {
  expect(validate("CaseOut", caseOut)).toEqual([]);
  expect(validate("CaseOut", { ...caseOut, owner: "Ivy", something_new: { x: 1 } })).toEqual([]);
});

test("missing fields, wrong types and unknown values are reported with their path", () => {
  expect(validate("CaseOut", omit(caseOut, "status"))).toEqual(["$.status: missing"]);
  expect(validate("CaseOut", { ...caseOut, row_version: "1" })).toEqual(["$.row_version: expected integer, got string"]);
  expect(validate("CaseOut", { ...caseOut, discrepancy_types: ["x", 2] })).toEqual([
    "$.discrepancy_types[1]: expected string, got integer",
  ]);
  expect(validate("CaseOut", { ...caseOut, po_number: 42 })).toEqual(["$.po_number: matches none of the allowed shapes"]);
  expect(validate("QueuePage", { items: [caseOut], next_cursor: null })[0]).toMatch(/^\$\.items\[0\]\./);
});

test("apiAs refuses a response this page does not understand", async () => {
  const proposal = {
    id: "p1", proposal_key: "k", version: 1, action_type: "create_follow_up_task", parameters: {},
    effect_summary: "Create a task", affected_resources: [], evidence_refs: [], source_manifest_digest: "sha256:x",
    investigation_version_id: "iv", policy_ref: "furniture-ap@v2", destination: { connector: "sim_tasks" },
    outbound: null, required_permission: "proposal.decide:create_follow_up_task", payload_digest: "sha256:y",
    canonicalization: "opsbridge-cjson/1", author_kind: "user", author_id: "u1", status: "awaiting_approval",
    generated_at: "2026-10-26T10:00:00Z", expires_at: "2026-10-26T10:30:00Z",
  };
  mockFetch(() => json(200, { items: [omit(proposal, "destination")] }));
  const err = await apiAs("ProposalList", "/api/v1/x").catch((e: unknown) => e);
  expect(err).toBeInstanceOf(ApiError);
  expect((err as ApiError).code).toBe("unexpected_response");
  expect((err as ApiError).details.problems).toEqual(["$.items[0].destination: missing"]);

  mockFetch(() => json(200, caseOut));
  await expect(apiAs("CaseOut", "/api/v1/cases/c1")).resolves.toEqual(caseOut);
});
