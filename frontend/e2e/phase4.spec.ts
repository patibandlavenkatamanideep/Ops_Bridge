import { expect, test } from "@playwright/test";
import { createOrOpenCase, openCase, rerunInvestigation, signIn } from "./helpers";

test("investigator proposes, approver approves, execution is confirmed and the case stays open", async ({ browser }) => {
  const inv = await browser.newPage();
  await signIn(inv, "furn-investigator");
  await createOrOpenCase(inv, "INV-9001");
  // Fresh evidence state, then a proposal from the validated draft.
  await rerunInvestigation(inv, "rules_only");
  await inv.getByRole("tab", { name: "Proposals & execution" }).click();
  const create = inv.getByRole("button", { name: "Create proposal from the validated draft" });
  const existing = inv.locator(".finding", { hasText: "awaiting approval" });
  await expect(create.or(existing.first())).toBeVisible();
  if (await create.isVisible()) await create.click();
  const open = inv.locator(".finding", { hasText: "awaiting approval" }).first();
  await expect(open).toBeVisible();
  await expect(open).toContainText("create_follow_up_task");
  await expect(open).toContainText("Does not change the invoice");
  await expect(inv.getByRole("button", { name: /^Approve v/ })).toHaveCount(0); // investigators cannot approve

  const approver = await browser.newPage();
  await signIn(approver, "furn-approver");
  await openCase(approver, "INV-9001");
  await approver.getByRole("tab", { name: "Proposals & execution" }).click();
  await approver.getByRole("button", { name: /^Approve v/ }).first().click();
  const op = approver.locator(".finding", { hasText: "via sim_tasks" }).first();
  await expect(op).toContainText("confirmed", { timeout: 30_000 });
  await expect(op.getByRole("list", { name: "Attempts" })).toContainText("dispatch");
  await expect(op.getByRole("list", { name: "Attempts" })).toContainText("readback");
  await expect(op).toContainText("read_back");
  await expect(approver.locator(".badge").first()).toContainText("Follow-up pending");
});

test("requester cannot see proposals or approve", async ({ page }) => {
  await signIn(page, "furn-requester");
  await page.goto("/t/demo-furniture/cases");
  await expect(page.getByRole("heading", { name: "Case queue" })).toBeVisible();
  const first = page.getByRole("link", { name: /CASE-/ }).first();
  if (await first.count()) {
    await first.click();
    await expect(page.getByRole("tab", { name: "Proposals & execution" })).toHaveCount(0);
  }
});
