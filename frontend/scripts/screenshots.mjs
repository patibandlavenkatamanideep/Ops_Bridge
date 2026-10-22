// Capture README screenshots from the running dev stack (synthetic demo data only).
// Usage: (make dev running) cd frontend && node scripts/screenshots.mjs
import { chromium } from "@playwright/test";
import { readFileSync } from "node:fs";

const BASE = process.env.OPSBRIDGE_UI_URL ?? "http://localhost:5173";
const OUT = new URL("../../docs/images/", import.meta.url).pathname;
const env = readFileSync(new URL("../../.env", import.meta.url), "utf8");
const password = env.split("\n").find((l) => l.startsWith("OPSBRIDGE_DEMO_USER_PASSWORD=")).split("=", 2)[1].trim();

async function session(browser, username) {
  const page = await browser.newPage({ viewport: { width: 1280, height: 860 } });
  await page.goto(BASE);
  await page.getByRole("link", { name: "Sign in with your organization" }).click();
  await page.locator("#username").fill(username);
  await page.locator("#password").fill(password);
  await page.locator("#kc-login").click();
  await page.getByText("OpsBridge").first().waitFor();
  return page;
}

async function openCase(page, invoice) {
  await page.goto(`${BASE}/t/demo-furniture/cases`);
  await page.getByLabel("Search").fill(invoice);
  await page.getByRole("link", { name: new RegExp(invoice) }).first().click();
  await page.getByRole("heading", { level: 1 }).waitFor();
}

// Synthetic demo cases with different outcomes (created once, investigated rules-only).
const DEMO = ["INV-9003", "INV-9010", "INV-9012", "INV-9019"];

async function ensureInvestigated(page, invoice) {
  await page.goto(`${BASE}/t/demo-furniture/cases/new`);
  await page.getByLabel("Title").fill(`Invoice ${invoice} blocked`);
  await page.getByLabel("Invoice number").fill(invoice);
  await page.getByRole("button", { name: "Create case" }).click();
  const existing = page.getByRole("link", { name: "Open the existing case" });
  const heading = page.getByRole("heading", { level: 1 }).filter({ hasText: invoice });
  await existing.or(heading).waitFor();
  if (await existing.isVisible()) return; // already there from an earlier run
  await page.getByRole("button", { name: /Run investigation|Re-run investigation/ }).waitFor();
  if (await page.locator(".facts dt", { hasText: "Version" }).count()) return; // already investigated
  await page.getByLabel("Model").selectOption("rules_only");
  await page.getByRole("button", { name: /Run investigation/ }).click();
  await page.locator(".facts dt", { hasText: "Version" }).waitFor({ timeout: 30_000 });
}

const browser = await chromium.launch();
try {
  const inv = await session(browser, "furn-investigator");
  for (const invoice of DEMO) await ensureInvestigated(inv, invoice);
  await inv.goto(`${BASE}/t/demo-furniture/cases`);
  await inv.getByLabel("Search").fill("INV-90"); // demo invoices, not leftover browser-test cases
  await inv.getByRole("table").waitFor();
  await inv.waitForTimeout(600);
  await inv.screenshot({ path: `${OUT}01-case-queue.png` });

  await openCase(inv, "INV-9001");
  await inv.getByText(/Findings|Cited findings/).first().waitFor();
  await inv.screenshot({ path: `${OUT}02-investigation.png`, fullPage: true });

  await inv.getByRole("button", { name: /Open evidence invoice_line.quantity/ }).first().click(); // cited line

  await inv.getByRole("heading", { name: "Source text" }).waitFor();
  await inv.screenshot({ path: `${OUT}03-evidence.png` });

  const appr = await session(browser, "furn-approver");
  await appr.goto(`${BASE}/t/demo-furniture/cases`);
  await appr.getByLabel("Search").fill("INV-9001");
  await appr.getByRole("link", { name: /INV-9001/ }).first().click();
  await appr.getByRole("tab", { name: "Proposals & execution" }).click();
  await appr.waitForTimeout(800);
  await appr.screenshot({ path: `${OUT}04-proposals-execution.png` }); // latest proposal (history is long)

  await inv.goto(`${BASE}/t/demo-furniture/evaluation`);
  const held = inv.getByRole("row").filter({ hasText: "heldout" }).filter({ hasText: "record" });
  await held.first().waitFor(); // the run list, not the session check that precedes it
  await inv.screenshot({ path: `${OUT}05-evaluation.png` });
  await held.first().getByRole("link").click();
  await inv.getByRole("heading", { name: /heldout · record/i }).waitFor();
  await inv.screenshot({ path: `${OUT}06-evaluation-run.png` }); // the summary, not every case

  const admin = await session(browser, "furn-admin");
  await admin.goto(`${BASE}/t/demo-furniture/settings`);
  await admin.getByRole("heading", { name: "Tenant settings" }).waitFor();
  await admin.screenshot({ path: `${OUT}07-tenant-settings.png` });
  console.log(`screenshots written to ${OUT}`);
} finally {
  await browser.close();
}
