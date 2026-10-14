import { expect, test, type Page } from "@playwright/test";
import { signIn } from "./helpers";

/** Re-run with a chosen model mode and wait for a NEW investigation version (not a stale one). */
async function rerun(page: Page, mode: string): Promise<void> {
  const version = page.locator(".facts dt", { hasText: "Version" }).locator("xpath=following-sibling::dd[1]");
  const before = (await version.count()) ? await version.innerText() : "";
  await page.getByLabel("Model").selectOption(mode);
  await page.getByRole("button", { name: /Run investigation|Re-run investigation/ }).click();
  await expect(version).not.toHaveText(before, { timeout: 30_000 });
}

async function openInvoiceCase(page: Page): Promise<void> {
  await page.goto("/t/demo-furniture/cases");
  await page.getByRole("link", { name: /INV-9001/ }).first().click();
  await expect(page.getByRole("button", { name: /Run investigation|Re-run investigation/ })).toBeEnabled();
}

test("mock-model run labels its origin and shows a gated draft follow-up", async ({ page }) => {
  await signIn(page, "furn-investigator");
  await openInvoiceCase(page);
  await rerun(page, "mock");
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
  await rerun(page, "rules_only");
  await expect(page.locator(".facts dd", { hasText: "explanation: template" })).toBeVisible();
});
