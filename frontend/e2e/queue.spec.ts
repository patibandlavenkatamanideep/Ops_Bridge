import { expect, test } from "@playwright/test";
import { createOrOpenCase, signIn } from "./helpers";

test("an investigator takes a case, finds it under Mine, then releases it", async ({ page }) => {
  const invoice = `INV-Q-${Date.now()}`; // its own case: the demo database persists
  await signIn(page, "furn-investigator");
  await createOrOpenCase(page, invoice);
  const owner = page.locator(".facts dt", { hasText: "Owner" }).locator("xpath=following-sibling::dd[1]");
  await expect(owner).toHaveText("Unassigned");
  await page.getByRole("group", { name: "Case owner" }).getByRole("button", { name: "Take this case" }).click();
  await expect(owner).toHaveText("Ivy Investigator");

  await page.getByRole("link", { name: "← Case queue" }).click();
  await page.getByRole("navigation", { name: "Queue views" }).getByRole("button", { name: /^Mine/ }).click();
  await expect(page).toHaveURL(/view=mine/);
  await expect(page.getByRole("link", { name: new RegExp(invoice) })).toBeVisible();

  await page.getByRole("link", { name: new RegExp(invoice) }).click();
  await page.getByRole("group", { name: "Case owner" }).getByRole("button", { name: "Release" }).click();
  await expect(owner).toHaveText("Unassigned");
});
