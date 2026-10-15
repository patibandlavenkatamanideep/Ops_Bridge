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
