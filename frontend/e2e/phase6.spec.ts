import { expect, test } from "@playwright/test";
import { createOrOpenCase, signIn } from "./helpers";

test("case notes can be added, corrected and have their text deleted with a reason", async ({ page }) => {
  const stamp = Date.now().toString(36);
  await signIn(page, "furn-investigator");
  await createOrOpenCase(page, "INV-9001");
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
  await createOrOpenCase(page, "INV-9001");
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
  await createOrOpenCase(page, "INV-9001");
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

test("tenant settings show the policy in force and the connector's current promises", async ({ page }) => {
  await signIn(page, "furn-admin");
  await page.goto("/t/demo-furniture/settings");
  const health = page.getByRole("region", { name: "Connector health" });
  await expect(health.locator("dd").filter({ hasText: /^sim_tasks$/ })).toBeVisible();
  await expect(health).toContainText("Reachable");
  await expect(health).toContainText(/idempotency keys|no idempotency keys/);
  await expect(health).toContainText("After an ambiguous create");
  await health.getByRole("button", { name: "Check again" }).click();
  await expect(health.getByRole("button", { name: "Check again" })).toBeEnabled();
  const policies = page.getByRole("region", { name: "Policies" });
  await expect(policies.getByRole("row").filter({ hasText: "furniture-ap" }).filter({ hasText: "v2" }))
    .toContainText(/active/i);
  await expect(policies.getByRole("row").filter({ hasText: "furniture-ap" }).filter({ hasText: "v1" }))
    .toContainText(/superseded/i);
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

test("an administrator revokes and reinstates a member, giving a reason each time", async ({ page }) => {
  await signIn(page, "furn-admin");
  await page.goto("/t/demo-furniture/settings");
  const members = page.getByRole("table", { name: "Members of this organization" });
  await expect(members.getByRole("row", { name: /Ada Admin/ })).toContainText("You");
  await expect(members.getByRole("button", { name: "Change Ada Admin" })).toHaveCount(0); // not their own

  const row = members.getByRole("row", { name: /Ana Notes-Approver/ });
  // Reinstate first if an interrupted earlier run left the member revoked.
  const steps: [string, string, RegExp][] = [
    ["Revoked: no access to this organization", "E2E: access paused", /revoked/i],
    ["Active", "E2E: access restored", /active/i],
  ];
  if (/revoked/i.test(await row.getByRole("cell").nth(2).innerText())) steps.reverse();
  for (const [access, reason, shown] of steps) {
    await members.getByRole("button", { name: "Change Ana Notes-Approver" }).click();
    const form = page.getByRole("form", { name: "Change membership of Ana Notes-Approver" });
    await expect(form.getByRole("heading")).toBeFocused();
    await form.getByLabel(access).check();
    await form.getByLabel("Reason for the change").fill(reason);
    await form.getByRole("button", { name: "Save change" }).click();
    await expect(page.getByText("Membership of Ana Notes-Approver updated.")).toBeVisible();
    await expect(form).toHaveCount(0);
    await expect(row.getByRole("cell").nth(2)).toHaveText(shown);
    await expect(row).toContainText(reason);
  }
  await expect(row.getByRole("cell").nth(2)).toHaveText(/active/i);
});
