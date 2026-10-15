import { expect, test, type Browser, type Locator, type Page } from "@playwright/test";
import { createHmac } from "node:crypto";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { openCase, rerunInvestigation, signIn } from "./helpers";

// The recovery and portability steps of docs/demo-script.md, against the live dev stack.
const SIM = "http://127.0.0.1:8090";
const ORG = "demo-furniture";

function envValue(name: string): string {
  const env = readFileSync(fileURLToPath(new URL("../../.env", import.meta.url)), "utf8");
  return env.split("\n").find((l) => l.startsWith(`${name}=`))!.slice(name.length + 1).trim();
}

async function control(page: Page, path: string, body?: object, method = "POST"): Promise<unknown> {
  const r = await page.request.fetch(`${SIM}/_control/${path}`, { method, data: body });
  expect(r.ok()).toBeTruthy();
  return r.json();
}

async function taskCount(page: Page): Promise<number> {
  return ((await control(page, `stats/${ORG}`, undefined, "GET")) as { tasks: number }).tasks;
}

/** Fresh investigation → proposal (investigator) → approval (approver); returns the newest operation card. */
async function proposeAndApprove(browser: Browser): Promise<{ approver: Page; op: Locator }> {
  const inv = await browser.newPage();
  await signIn(inv, "furn-investigator");
  await openCase(inv, "INV-9001");
  await rerunInvestigation(inv, "rules_only");
  await inv.getByRole("tab", { name: "Proposals & execution" }).click();
  const create = inv.getByRole("button", { name: "Create proposal from the validated draft" });
  const waiting = inv.locator(".finding", { hasText: "awaiting approval" });
  await expect(create.or(waiting.first())).toBeVisible();
  if (await create.isVisible()) await create.click();
  await expect(waiting.first()).toBeVisible();
  await inv.close();
  const approver = await browser.newPage();
  await signIn(approver, "furn-approver");
  await openCase(approver, "INV-9001");
  await approver.getByRole("tab", { name: "Proposals & execution" }).click();
  await expect(approver.getByRole("button", { name: /^Approve v/ }).first()).toBeVisible();
  await approver.waitForLoadState("networkidle"); // the operation history has loaded
  const before = await approver.locator(".finding", { hasText: "via sim_tasks" }).count();
  await approver.getByRole("button", { name: /^Approve v/ }).first().click();
  await expect(approver.locator(".finding", { hasText: "via sim_tasks" })).toHaveCount(before + 1, { timeout: 15_000 });
  return { approver, op: approver.locator(".finding", { hasText: "via sim_tasks" }).first() };
}

test("demo step 4a: a timeout after commit is reconciled to exactly one task", async ({ browser, page }) => {
  const before = await taskCount(page);
  await control(page, "faults", { org: ORG, route: "tasks.create", mode: "timeout_after_commit" });
  try {
    const { approver, op } = await proposeAndApprove(browser);
    await expect(op.locator(".badge")).toHaveText(/confirmed/i, { timeout: 45_000 });
    await expect(op.getByRole("list", { name: "Attempts" })).toContainText(/ambiguous/i);
    expect(await taskCount(page)).toBe(before + 1);
    await approver.close();
  } finally {
    await control(page, "faults", undefined, "DELETE");
  }
});

test("demo step 4b: without reliable lookup the outcome is unknown and a person records it", async ({ browser, page }) => {
  const before = await taskCount(page);
  await control(page, "capabilities", { org: ORG, idempotency: false, search: false });
  await control(page, "faults", { org: ORG, route: "tasks.create", mode: "timeout_after_commit" });
  let approver: Page | undefined;
  try {
    const flow = await proposeAndApprove(browser);
    approver = flow.approver;
    await expect(flow.op.locator(".badge")).toHaveText(/outcome unknown/i, { timeout: 45_000 });
    await expect(flow.op).toContainText("OpsBridge will not retry blindly");
    expect(await taskCount(page)).toBe(before + 1); // the task exists; it was not created twice
  } finally {
    await control(page, "faults", undefined, "DELETE");
    await control(page, "capabilities", { org: ORG, idempotency: true, search: true });
  }
  // The reviewer checks the task system for the operation marker, then records what they found.
  const op = approver!.locator(".finding", { hasText: "via sim_tasks" }).first();
  const key = (await op.innerText()).match(/opk-[0-9a-f]{40}/)![0];
  const orgKey = createHmac("sha256", envValue("OPSBRIDGE_SIM_MASTER_SECRET")).update(`opsbridge-sim:${ORG}`).digest("hex");
  const found = await page.request.get(`${SIM}/orgs/${ORG}/tasks?marker=opsbridge-op:${key}`, {
    headers: { Authorization: `Bearer ${orgKey}` },
  });
  const items = ((await found.json()) as { items: { id: string }[] }).items;
  expect(items).toHaveLength(1);
  await op.getByLabel("Remote reference").fill(items[0]!.id);
  await op.getByLabel("Note").fill("Found by operation marker in the task system");
  await op.getByRole("button", { name: "Record review" }).click();
  await expect(op.locator(".badge")).toHaveText(/confirmed/i);
  await expect(op).toContainText("human_review");
});

test("demo step 6: the IT pack's local-only policy blocks the model with no hosted fallback", async ({ page }) => {
  await signIn(page, "it-investigator");
  await page.goto("/t/demo-it/cases/new");
  await page.getByLabel("Title").fill("Invoice INV-7002 blocked");
  await page.getByLabel("Invoice number").fill("INV-7002");
  await page.getByRole("button", { name: "Create case" }).click();
  const existing = page.getByRole("link", { name: "Open the existing case" });
  await expect(existing.or(page.getByRole("heading", { level: 1 }).filter({ hasText: "INV-7002" }))).toBeVisible();
  if (await existing.isVisible()) await existing.click();
  await rerunInvestigation(page, "");
  const facts = page.locator(".investigation > .facts");
  await expect(facts).toContainText("discrepancies found");
  await expect(facts).toContainText("it-procurement@v1");
  // Refused, and no hosted model is used instead (the routing reason names why).
  await expect(facts).toContainText(/ollama — blocked: local inference unavailable/);
  await expect(facts).toContainText("local_required");
  await expect(facts).toContainText("explanation: template");
  await expect(page.getByText("it-asset-intake").first()).toBeVisible();
});
