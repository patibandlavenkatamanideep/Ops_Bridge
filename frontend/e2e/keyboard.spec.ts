import { expect, test, type Locator, type Page } from "@playwright/test";
import { signIn } from "./helpers";

// A complete operator task with the keyboard only (after signing in): every control on the
// way must be reachable with Tab, usable with Enter or the arrow keys, and must leave focus
// somewhere sensible. Uses INV-9003 so it does not meet the pointer-driven approval test.
const INVOICE = "INV-9003";

/** Press Tab until `target` has focus: a keyboard user can reach it from where they are. */
async function tabTo(page: Page, target: Locator, limit = 80): Promise<void> {
  await expect(target).toBeVisible();
  for (let i = 0; i < limit; i++) {
    if (await target.evaluate((el) => el.ownerDocument.activeElement === el)) return;
    await page.keyboard.press("Tab");
  }
  throw new Error(`not reachable with Tab within ${limit} presses: ${target.toString()}`);
}

/** After an action the keyboard user's place is kept: focus is not dropped to the page. */
async function expectFocusKept(page: Page, after: string): Promise<void> {
  const where = await page.locator("body").evaluate((body) => body.ownerDocument.activeElement?.tagName ?? "none");
  expect(where, `focus was lost after ${after}`).not.toBe("BODY");
}

/** From the queue, find the invoice's case by keyboard and open it. */
async function openFromQueue(page: Page): Promise<void> {
  await page.goto("/t/demo-furniture/cases");
  await tabTo(page, page.getByLabel("Search"));
  await page.keyboard.type(INVOICE);
  await tabTo(page, page.getByRole("link", { name: new RegExp(INVOICE) }).first());
  await page.keyboard.press("Enter");
  await expect(page.getByRole("heading", { level: 1 })).toContainText(INVOICE);
}

/** Move along the case tabs with the arrow keys until `name` is selected. */
async function arrowToTab(page: Page, name: string): Promise<void> {
  await tabTo(page, page.getByRole("tab", { selected: true }));
  const target = page.getByRole("tab", { name });
  for (let i = 0; i < 4 && (await target.getAttribute("aria-selected")) !== "true"; i++) {
    await page.keyboard.press("ArrowRight");
  }
  await expect(target).toHaveAttribute("aria-selected", "true");
  await expect(target).toBeFocused();
}

test("an investigator and an approver complete a case with the keyboard only", async ({ browser }) => {
  const inv = await browser.newPage();
  await signIn(inv, "furn-investigator");

  // Open the case: create it through the form, or follow the link to the existing one.
  await tabTo(inv, inv.getByRole("link", { name: "New case" }).first());
  await inv.keyboard.press("Enter");
  await tabTo(inv, inv.getByLabel("Title"));
  await inv.keyboard.type(`Invoice ${INVOICE} blocked`);
  await tabTo(inv, inv.getByLabel("Invoice number"));
  await inv.keyboard.type(INVOICE);
  await tabTo(inv, inv.getByRole("button", { name: "Create case" }));
  await inv.keyboard.press("Enter");
  const existing = inv.getByRole("link", { name: "Open the existing case" });
  const heading = inv.getByRole("heading", { level: 1 }).filter({ hasText: INVOICE });
  await expect(existing.or(heading)).toBeVisible();
  if (await existing.isVisible()) {
    await tabTo(inv, existing);
    await inv.keyboard.press("Enter");
  }
  await expect(inv.getByRole("heading", { level: 1 })).toContainText(INVOICE);

  // Take the case (unless an earlier run left it with this investigator).
  const take = inv.getByRole("group", { name: "Case owner" }).getByRole("button", { name: /^Take/ });
  if (await take.count()) {
    await tabTo(inv, take);
    await inv.keyboard.press("Enter");
    await expect(inv.getByRole("status").filter({ hasText: "You own this case." })).toBeFocused();
  }

  // Investigate, and wait for a new version.
  const version = inv.locator(".facts dt", { hasText: "Version" }).locator("xpath=following-sibling::dd[1]");
  const empty = inv.getByRole("heading", { name: "No investigation yet" });
  await expect(version.or(empty)).toBeVisible();
  const before = (await version.count()) ? await version.innerText() : "";
  await tabTo(inv, inv.getByRole("button", { name: /Run investigation|Re-run investigation/ }));
  await inv.keyboard.press("Enter");
  await expect(version).not.toHaveText(before, { timeout: 30_000 });
  await expectFocusKept(inv, "running the investigation");

  // Propose the validated draft.
  await arrowToTab(inv, "Proposals & execution");
  const create = inv.getByRole("button", { name: "Create proposal from the validated draft" });
  const open = inv.locator(".finding", { hasText: "awaiting approval" }).first();
  await expect(create.or(open)).toBeVisible();
  if (await create.isVisible()) {
    await tabTo(inv, create);
    await inv.keyboard.press("Enter");
    await expect(open).toBeVisible();
    await expectFocusKept(inv, "creating the proposal");
  }
  await expect(open).toBeVisible();

  // The approver finds the case and approves by keyboard.
  const approver = await browser.newPage();
  await signIn(approver, "furn-approver");
  await openFromQueue(approver);
  await arrowToTab(approver, "Proposals & execution");
  // Operations confirmed before this approval (earlier runs leave some in a reused database).
  const confirmed = approver.locator(".finding", { hasText: "via sim_tasks" })
    .filter({ has: approver.locator(".badge", { hasText: /^confirmed$/i }) });
  await expect(approver.getByRole("heading", { name: "Execution timeline" })).toBeVisible();
  const confirmedBefore = await confirmed.count();
  await tabTo(approver, approver.getByRole("button", { name: /^Approve v/ }).first());
  await approver.keyboard.press("Enter");
  await expect(approver.getByRole("status").filter({ hasText: /^Approved v\d+/ })).toBeFocused();
  await expect(confirmed).toHaveCount(confirmedBefore + 1, { timeout: 30_000 });
  await expectFocusKept(approver, "approving");
});
