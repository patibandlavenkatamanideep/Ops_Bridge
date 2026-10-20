import { expect, test, type Page } from "@playwright/test";
import { createOrOpenCase, rerunInvestigation, signIn } from "./helpers";

async function openInvoiceCase(page: Page): Promise<void> {
  await createOrOpenCase(page, "INV-9001");
  await expect(page.getByRole("button", { name: /Run investigation|Re-run investigation/ })).toBeEnabled();
}

test("mock-model run labels its origin and shows a gated draft follow-up", async ({ page }) => {
  await signIn(page, "furn-investigator");
  await openInvoiceCase(page);
  await rerunInvestigation(page, "mock");
  await expect(page.locator(".facts dd", { hasText: "explanation: model:mock" })).toBeVisible();
  await expect(page.locator(".explanation:not(.draft-body)")).toContainText("[E");
  const draft = page.locator(".state", { hasText: "Draft follow-up (not yet a proposal)" });
  await expect(draft).toContainText("create_follow_up_task");
  await expect(draft).toContainText("receiving");
  await expect(draft).toContainText("passed");
});

test("rules-only run uses the template explanation", async ({ page }) => {
  await signIn(page, "furn-investigator");
  await openInvoiceCase(page);
  await rerunInvestigation(page, "rules_only");
  await expect(page.locator(".facts dd", { hasText: "explanation: template" })).toBeVisible();
});
