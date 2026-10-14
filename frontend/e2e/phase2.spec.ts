import { expect, test, type Page } from "@playwright/test";
import { signIn } from "./helpers";

/** Create a case for an invoice, or open the existing one (keeps the test repeatable). */
async function openCase(page: Page, invoice: string): Promise<void> {
  await page.goto("/t/demo-furniture/cases/new");
  await page.getByLabel("Title").fill(`Invoice ${invoice} blocked`);
  await page.getByLabel("Invoice number").fill(invoice);
  await page.getByRole("button", { name: "Create case" }).click();
  const existing = page.getByRole("link", { name: "Open the existing case" });
  const heading = page.getByRole("heading", { level: 1 });
  await expect(existing.or(heading.filter({ hasText: invoice }))).toBeVisible();
  if (await existing.isVisible()) await existing.click();
  await expect(page.getByRole("heading", { level: 1 })).toContainText(invoice);
}

test("investigation shows a cited quantity mismatch and opens the cited source line", async ({ page }) => {
  await signIn(page, "furn-investigator");
  await openCase(page, "INV-9001");
  await page.getByRole("button", { name: /Run investigation|Re-run investigation/ }).click();
  const finding = page.locator(".finding", { hasText: "Quantity mismatch" });
  await expect(finding).toBeVisible({ timeout: 30_000 });
  await expect(finding).toContainText("2 EA exceed the allocatable quantity");
  await expect(page.locator(".explanation")).toContainText("does not resolve the discrepancy");
  await expect(page.locator(".facts dd", { hasText: "furniture-ap@v2" })).toBeVisible();

  await finding.getByRole("button", { name: /receipt_line.quantity/ }).click();
  await expect(page.getByRole("tab", { name: "Evidence" })).toHaveAttribute("aria-selected", "true");
  const hit = page.locator(".src-line.hit");
  await expect(hit).toBeVisible();
  await expect(hit).toContainText("18");
  await expect(page.locator("tr.hit")).toContainText("receipt_line.quantity");
});

test("tabs support arrow-key navigation", async ({ page }) => {
  await signIn(page, "furn-investigator");
  await openCase(page, "INV-9001");
  const inv = page.getByRole("tab", { name: "Investigation" });
  await inv.focus();
  await page.keyboard.press("ArrowRight");
  await expect(page.getByRole("tab", { name: "Evidence" })).toBeFocused();
  await expect(page.getByRole("tab", { name: "Evidence" })).toHaveAttribute("aria-selected", "true");
});

test("approver can read evidence but cannot run investigations or correct fields", async ({ page }) => {
  await signIn(page, "furn-approver");
  await page.goto("/t/demo-furniture/cases");
  await page.getByRole("link", { name: /INV-9001/ }).first().click();
  await expect(page.getByRole("tab", { name: "Evidence" })).toBeVisible();
  await expect(page.getByRole("button", { name: /Run investigation|Re-run investigation/ })).toHaveCount(0);
  await page.getByRole("tab", { name: "Evidence" }).click();
  await expect(page.getByRole("button", { name: "Correct" })).toHaveCount(0);
});
