import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { createOrOpenCase, rerunInvestigation, signIn } from "./helpers";

// Automated WCAG 2.1 A/AA checks with axe-core on every main screen. This catches a subset
// of accessibility problems; it is not a substitute for testing with assistive technology.
async function expectNoSeriousViolations(page: Page, screen: string): Promise<void> {
  const result = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
  const serious = result.violations.filter((v) => v.impact === "serious" || v.impact === "critical");
  const report = serious.map((v) => `${v.id} (${v.impact}): ${v.help} — ${v.nodes.map((n) => n.target.join(" ")).slice(0, 3).join(", ")}`);
  expect(report, `${screen}: axe violations`).toEqual([]);
}

test("sign-in page", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("link", { name: "Sign in with your organization" })).toBeVisible();
  await expectNoSeriousViolations(page, "sign-in");
});

test("investigator screens", async ({ page }) => {
  await signIn(page, "furn-investigator");
  await page.goto("/t/demo-furniture/cases");
  await expect(page.getByRole("heading", { name: "Case queue" })).toBeVisible();
  await expectNoSeriousViolations(page, "case queue");
  await page.goto("/t/demo-furniture/cases/new");
  await expect(page.getByRole("button", { name: "Create case" })).toBeVisible();
  await expectNoSeriousViolations(page, "new case");
  await createOrOpenCase(page, "INV-9001");
  await rerunInvestigation(page, "rules_only"); // scan a real investigation, also on a fresh database
  await expect(page.locator(".investigation")).toBeVisible();
  await expectNoSeriousViolations(page, "investigation tab");
  for (const tab of ["Evidence", "Proposals & execution", "Conversation"]) {
    await page.getByRole("tab", { name: tab }).click();
    await page.waitForLoadState("networkidle");
    await expectNoSeriousViolations(page, `${tab} tab`);
  }
  await page.goto("/t/demo-furniture/evaluation");
  await expect(page.getByRole("heading", { name: "Evaluation" })).toBeVisible();
  await expectNoSeriousViolations(page, "evaluation runs");
  await page.getByRole("row").filter({ hasText: "heldout" }).filter({ hasText: "record" }).getByRole("link").click();
  await expect(page.getByRole("heading", { level: 1 })).toContainText(/heldout/i);
  await expectNoSeriousViolations(page, "evaluation run");
});

test("administrator settings", async ({ page }) => {
  await signIn(page, "furn-admin");
  await page.goto("/t/demo-furniture/settings");
  await expect(page.getByRole("heading", { name: "Tenant settings" })).toBeVisible();
  await expectNoSeriousViolations(page, "tenant settings");
  await page.getByRole("button", { name: "Change Ana Notes-Approver" }).click();
  await expect(page.getByRole("form", { name: "Change membership of Ana Notes-Approver" })).toBeVisible();
  await expectNoSeriousViolations(page, "tenant settings with the member editor open");
});

test("the scanner reports a planted violation (self-check)", async ({ page }) => {
  // A minimal page with an image that has no alt text.
  await page.setContent('<html lang="en"><body><main><img src="data:image/gif;base64,R0lGODlhAQABAAAAACw="></main></body></html>');
  const result = await new AxeBuilder({ page }).withTags(["wcag2a"]).analyze();
  expect(result.violations.map((v) => v.id)).toContain("image-alt");
});
