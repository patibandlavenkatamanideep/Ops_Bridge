import { screen, within } from "@testing-library/react";
import { CaseQueue } from "./CaseQueue";
import { json, mockFetch, renderAt } from "../test/utils";

afterEach(() => vi.unstubAllGlobals());

const item = {
  id: "c1", case_number: "CASE-00001", title: "INV-9001 blocked", status: "needs_information",
  discrepancy_types: ["quantity_mismatch"], evidence_readiness: "complete", owner: null,
  assignee_group: "receiving", age_seconds: 7200, created_at: "2026-10-14T10:00:00Z",
};

test("shows loading then rows with case state", async () => {
  mockFetch(() => json(200, { items: [item], next_cursor: null }));
  renderAt("/t/demo-furniture/cases", "/t/:tenant/cases", <CaseQueue />);
  expect(screen.getByRole("status")).toHaveTextContent("Loading cases");
  expect(await screen.findByRole("link", { name: /CASE-00001/ })).toHaveAttribute("href", "/t/demo-furniture/cases/c1");
  const row = within(screen.getByRole("table")).getAllByRole("row")[1]!;
  expect(within(row).getByText("Needs information")).toBeInTheDocument();
  expect(within(row).getByText("receiving")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Load more" })).not.toBeInTheDocument();
});

test("shows an empty state", async () => {
  mockFetch(() => json(200, { items: [], next_cursor: null }));
  renderAt("/t/demo-furniture/cases", "/t/:tenant/cases", <CaseQueue />);
  expect(await screen.findByRole("heading", { name: "No cases" })).toBeInTheDocument();
});

test("shows a denied state with the correlation reference", async () => {
  mockFetch(() => json(403, { error: { code: "forbidden", message: "You do not have permission for this action.",
                                        correlation_id: "abc123def456", retryable: false, details: {} } }));
  renderAt("/t/demo-furniture/cases", "/t/:tenant/cases", <CaseQueue />);
  const alert = await screen.findByRole("alert");
  expect(alert).toHaveTextContent("You don't have access to this");
  expect(alert).toHaveTextContent("abc123def456");
  expect(screen.queryByRole("button", { name: "Try again" })).not.toBeInTheDocument();
});

test("retryable failures offer a retry", async () => {
  mockFetch(() => json(503, { error: { code: "dependency_unavailable", message: "Database is unavailable.",
                                        correlation_id: "x1y2z3w4", retryable: true, details: {} } }));
  renderAt("/t/demo-furniture/cases", "/t/:tenant/cases", <CaseQueue />);
  expect(await screen.findByRole("button", { name: "Try again" })).toBeInTheDocument();
});
