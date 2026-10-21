import { expect, test, type Page } from "@playwright/test";
import { signIn } from "./helpers";

const EMAIL = "new-hire@example.test"; // a Keycloak user with a verified address and no membership

/** Bring the local data back to "new-hire is not an active member and has no open invitation". */
async function resetNewHire(admin: Page): Promise<void> {
  const members = admin.getByRole("table", { name: "Members of this organization" });
  await expect(members).toBeVisible(); // decide only once the settings have loaded
  const withdraw = admin.getByRole("button", { name: `Withdraw invitation for ${EMAIL}` });
  if (await withdraw.count()) {
    await withdraw.click();
    await expect(withdraw).toHaveCount(0);
  }
  const row = members.getByRole("row", { name: /Nia Newhire/ });
  if ((await row.count()) && /active/i.test(await row.getByRole("cell").nth(2).innerText())) {
    await members.getByRole("button", { name: "Change Nia Newhire" }).click();
    const form = admin.getByRole("form", { name: "Change membership of Nia Newhire" });
    await form.getByLabel("Revoked: no access to this organization").check();
    await form.getByLabel("Reason for the change").fill("E2E: reset before the invitation test");
    await form.getByRole("button", { name: "Save change" }).click();
    await expect(row.getByRole("cell").nth(2)).toHaveText(/revoked/i);
  }
}

test("an invited person opens the link, signs in and joins with the invited roles", async ({ browser }) => {
  const admin = await browser.newPage();
  await signIn(admin, "furn-admin", "/t/demo-furniture/settings");
  await expect(admin.getByRole("heading", { name: "Tenant settings" })).toBeVisible();
  await resetNewHire(admin);

  await admin.getByRole("button", { name: "Invite a person" }).click();
  const form = admin.getByRole("form", { name: "Invite a person" });
  await expect(form.getByRole("heading")).toBeFocused();
  await form.getByLabel("Email address").fill(EMAIL);
  await form.getByLabel("auditor", { exact: true }).check();
  await form.getByRole("button", { name: "Create invitation" }).click();
  const panel = admin.getByRole("region", { name: `Invitation link for ${EMAIL}` });
  await expect(panel.getByRole("heading")).toBeFocused();
  const link = await panel.getByLabel("Invitation link").inputValue();
  expect(link).toMatch(/\/invite#[A-Za-z0-9_-]{40,}$/);
  await expect(admin.getByRole("table", { name: "Open invitations" })).toContainText(EMAIL);

  // The invited person, in their own browser session.
  const hireContext = await browser.newContext();
  const hire = await hireContext.newPage();
  await signIn(hire, "new-hire", link);
  await expect(hire).toHaveURL(/\/invite$/); // the token left the address bar
  await expect(hire.getByRole("heading", { name: "Accept an invitation" })).toBeVisible();
  await hire.getByRole("button", { name: "Accept invitation" }).click();
  await expect(hire).toHaveURL(/\/t\/demo-furniture\/cases$/);
  await expect(hire.getByRole("heading", { name: "Case queue" })).toBeVisible();

  await admin.reload();
  const row = admin.getByRole("table", { name: "Members of this organization" }).getByRole("row", { name: /Nia Newhire/ });
  await expect(row.getByRole("cell").nth(2)).toHaveText(/active/i);
  await expect(row).toContainText("auditor");
  await expect(row).toContainText(/joined by invitation/i);
  await expect(admin.getByRole("table", { name: "Open invitations" })).toHaveCount(0);
  await hireContext.close();
});

test("an invitation link does not work for anyone else", async ({ browser }) => {
  const admin = await browser.newPage();
  await signIn(admin, "furn-admin", "/t/demo-furniture/settings");
  await resetNewHire(admin);
  await admin.getByRole("button", { name: "Invite a person" }).click();
  const form = admin.getByRole("form", { name: "Invite a person" });
  await form.getByLabel("Email address").fill(EMAIL);
  await form.getByLabel("requester", { exact: true }).check();
  await form.getByRole("button", { name: "Create invitation" }).click();
  const link = await admin.getByLabel("Invitation link", { exact: true }).inputValue();

  const otherContext = await browser.newContext();
  const other = await otherContext.newPage();
  await signIn(other, "it-requester", link); // a forwarded link, opened by someone else
  await other.getByRole("button", { name: "Accept invitation" }).click();
  await expect(other.getByRole("alert")).toContainText("different email address");
  await otherContext.close();

  await admin.getByRole("button", { name: `Withdraw invitation for ${EMAIL}` }).click();
  await expect(admin.getByRole("status").filter({ hasText: `Invitation for ${EMAIL} withdrawn.` })).toBeVisible();
});
