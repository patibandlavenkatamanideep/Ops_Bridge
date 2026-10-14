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
