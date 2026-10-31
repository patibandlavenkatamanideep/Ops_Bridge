import { expect, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

/** Read the development-only demo password from the repository .env without printing it. */
export function demoPassword(): string {
  const env = readFileSync(fileURLToPath(new URL("../../.env", import.meta.url)), "utf8");
  const line = env.split("\n").find((l) => l.startsWith("OPSBRIDGE_DEMO_USER_PASSWORD="));
  if (!line) throw new Error("OPSBRIDGE_DEMO_USER_PASSWORD missing from .env (run make setup)");
  return line.slice("OPSBRIDGE_DEMO_USER_PASSWORD=".length).trim();
}

/** Sign in through the real Keycloak login form. */
export async function signIn(page: Page, username: string, path = "/"): Promise<void> {
  await page.goto(path);
  await page.getByRole("link", { name: "Sign in with your organization" }).click();
  await page.locator("#username").fill(username);
  await page.locator("#password").fill(demoPassword());
  await page.locator("#kc-login").click();
  await expect(page.getByText("OpsBridge").first()).toBeVisible();
}

/**
 * Re-run the investigation on the open case with a model mode and wait for a NEW version.
 * Captures the current version only after the investigation panel has finished loading.
 */
export async function rerunInvestigation(page: Page, mode: string): Promise<void> {
  const version = page.locator(".facts dt", { hasText: "Version" }).locator("xpath=following-sibling::dd[1]");
  const empty = page.getByRole("heading", { name: "No investigation yet" });
  await expect(version.or(empty)).toBeVisible();
  const before = (await version.count()) ? await version.innerText() : "";
  await page.getByLabel("Model").selectOption(mode);
  await page.getByRole("button", { name: /Run investigation|Re-run investigation/ }).click();
  await expect(version).toBeVisible({ timeout: 30_000 });
  await expect(version).not.toHaveText(before, { timeout: 30_000 });
}

/**
 * Create a case for an invoice, or open the existing one. Use this where a test may run on a
 * fresh database (as in CI) instead of relying on a case another spec created earlier.
 */
export async function createOrOpenCase(page: Page, invoice: string, tenant = "demo-furniture"): Promise<void> {
  await page.goto(`/t/${tenant}/cases/new`);
  await page.getByLabel("Title").fill(`Invoice ${invoice} blocked`);
  await page.getByLabel("Invoice number").fill(invoice);
  await page.getByRole("button", { name: "Create case" }).click();
  const existing = page.getByRole("link", { name: "Open the existing case" });
  const heading = page.getByRole("heading", { level: 1 });
  await expect(existing.or(heading.filter({ hasText: invoice }))).toBeVisible();
  if (await existing.isVisible()) await existing.click();
  await expect(page.getByRole("heading", { level: 1 })).toContainText(invoice);
}

/**
 * The link to a benchmark's held-out record run on the evaluation list. Several benchmarks
 * re-run the same split, so rows are told apart by the benchmark in the link, not by text.
 */
export function heldOutRecordLink(page: Page, benchmark = "opsbridge-bench-v1") {
  return page.locator(`a[href*="/evaluation/${benchmark}/heldout-record-"]`);
}

/** Open an existing case from the queue by searching for it (robust to queue pagination). */
export async function openCase(page: Page, invoice: string, tenant = "demo-furniture"): Promise<void> {
  await page.goto(`/t/${tenant}/cases`);
  await page.getByLabel("Search").fill(invoice);
  await page.getByRole("link", { name: new RegExp(invoice) }).first().click();
  await expect(page.getByRole("heading", { level: 1 })).toContainText(invoice);
}
