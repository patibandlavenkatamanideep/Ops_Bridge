import { expect, test } from "@playwright/test";
import { signIn } from "./helpers";

test("investigator signs in, creates a case and sees it in the queue", async ({ page }) => {
  await signIn(page, "furn-investigator");
  await expect(page).toHaveURL(/\/t\/demo-furniture\/cases$/);
  await expect(page.getByRole("heading", { name: "Case queue" })).toBeVisible();
  await expect(page.getByText("Synthetic demo data")).toBeVisible();

  const invoice = `INV-E2E-${Date.now()}`;
  await page.getByRole("link", { name: "New case" }).first().click();
  await page.getByLabel("Title").fill(`Invoice ${invoice} blocked`);
  await page.getByLabel("Invoice number").fill(invoice);
  await page.getByRole("button", { name: "Create case" }).click();
  await expect(page.getByRole("heading", { name: new RegExp(invoice) })).toBeVisible();

  await page.getByRole("link", { name: "← Case queue" }).click();
  await expect(page.getByRole("link", { name: new RegExp(invoice) })).toBeVisible();
});

test("a user cannot open another organization's case", async ({ page, browser }) => {
  await signIn(page, "furn-investigator");
  const invoice = `INV-E2E-X-${Date.now()}`;
  await page.goto("/t/demo-furniture/cases/new");
  await page.getByLabel("Title").fill(`Cross-tenant probe ${invoice}`);
  await page.getByLabel("Invoice number").fill(invoice);
  await page.getByRole("button", { name: "Create case" }).click();
  await expect(page.getByRole("heading", { name: new RegExp(invoice) })).toBeVisible();
  const caseId = page.url().split("/").pop()!;

  const other = await browser.newPage();
  await signIn(other, "it-investigator");
  await other.goto(`/t/demo-it/cases/${caseId}`);
  await expect(other.getByRole("alert")).toContainText("Not found");
  await expect(other.getByText(invoice)).toHaveCount(0);
  await other.goto(`/t/demo-furniture/cases/${caseId}`);
  await expect(other.getByText("You are not a member of this organization")).toBeVisible();
});

test("keyboard users can skip to content and reach the queue controls", async ({ page }) => {
  await signIn(page, "furn-requester");
  await page.keyboard.press("Tab");
  const skip = page.getByRole("link", { name: "Skip to content" });
  await expect(skip).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.locator("#main")).toBeFocused();
});
