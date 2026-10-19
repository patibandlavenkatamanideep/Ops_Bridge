import { expect, test } from "@playwright/test";
import { openCase, signIn } from "./helpers";

test("case notes can be added, corrected and have their text deleted with a reason", async ({ page }) => {
  const stamp = Date.now().toString(36);
  await signIn(page, "furn-investigator");
  await openCase(page, "INV-9001");
  await page.getByRole("tab", { name: "Conversation" }).click();

  await page.getByLabel("Add a note").fill(`Receiving counted 18 chairs (${stamp})`);
  await page.getByRole("button", { name: "Add note" }).click();
  const original = page.locator(".message", { hasText: `Receiving counted 18 chairs (${stamp})` });
  await expect(original).toBeVisible();

  await original.getByRole("button", { name: "Correct" }).click();
  await page.getByLabel(/Correction to message/).fill(`Receiving counted 18 chairs, 2 back-ordered (${stamp})`);
  await page.getByRole("button", { name: "Save correction" }).click();
  const correction = page.locator(".message", { hasText: `2 back-ordered (${stamp})` });
  await expect(correction).toContainText("correction");
  await expect(original).toContainText("corrected by #");

  await correction.getByRole("button", { name: "Delete text" }).click();
  await correction.getByLabel(/Reason for deletion/).fill("Duplicate of the receiving log");
  await correction.getByRole("button", { name: "Delete message text" }).click();
  await expect(page.getByText(`2 back-ordered (${stamp})`)).toHaveCount(0);
  await expect(page.locator(".message", { hasText: "Duplicate of the receiving log" }).last())
    .toContainText("Text deleted");
});

test("an uploaded supplier message is parsed and shown as evidence", async ({ page }) => {
  await signIn(page, "furn-investigator");
  await openCase(page, "INV-9001");
  await page.getByRole("tab", { name: "Evidence" }).click();
  await page.getByRole("button", { name: "Upload a document" }).click();
  await page.getByLabel("Document type").selectOption("supplier_message");
  await page.getByLabel(/^File/).setInputFiles({
    name: "supplier-reply.txt",
    mimeType: "text/plain",
    buffer: Buffer.from("Hello,\nAll 20 chairs for PO-1001 were delivered.\nRegards (synthetic)\n"),
  });
  await page.getByRole("button", { name: "Upload", exact: true }).click();
  await expect(page.getByRole("status").filter({ hasText: "Stored as version" })).toContainText(/parsed/i);
  await expect(page.getByText("Uploaded supplier message").first()).toBeVisible();
  await expect(page.locator(".source")).toContainText("All 20 chairs for PO-1001 were delivered.");
});

test("a disguised upload is rejected with an explanation", async ({ page }) => {
  await signIn(page, "furn-investigator");
  await openCase(page, "INV-9001");
  await page.getByRole("tab", { name: "Evidence" }).click();
  await page.getByRole("button", { name: "Upload a document" }).click();
  await page.getByLabel(/^File/).setInputFiles({
    name: "invoice.pdf",
    mimeType: "application/pdf",
    buffer: Buffer.from("<html><body>not a pdf</body></html>"),
  });
  await page.getByRole("button", { name: "Upload", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("declared application/pdf but content looks like text/html");
  await expect(page.getByRole("status").filter({ hasText: "Stored as version" })).toHaveCount(0);
});

test("tenant settings: admins see tier-limited options; investigators are denied", async ({ page }) => {
  await signIn(page, "furn-admin");
  await page.getByRole("link", { name: "Tenant settings" }).click();
  await expect(page.getByRole("heading", { name: "Tenant settings" })).toBeVisible();
  await expect(page.locator(".facts dd").filter({ hasText: "Standard" })).toBeVisible();
  const modes = await page.getByLabel("Default investigation mode").locator("option").allInnerTexts();
  expect(modes.join(" ")).not.toMatch(/anthropic/i);
  await expect(page.getByRole("row").filter({ has: page.getByText("furn-approver", { exact: true }) }))
    .toContainText("Approver");
  await page.getByRole("button", { name: "Save settings" }).click();
  await expect(page.getByRole("status").filter({ hasText: "Settings saved." })).toBeVisible();

  await page.context().clearCookies();
  await signIn(page, "furn-investigator");
  await expect(page.getByRole("link", { name: "Tenant settings" })).toHaveCount(0);
  await page.goto("/t/demo-furniture/settings");
  await expect(page.getByRole("heading", { name: "You don't have access to this" })).toBeVisible();
});
