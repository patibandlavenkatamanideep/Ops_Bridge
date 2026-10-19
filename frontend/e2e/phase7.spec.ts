import { expect, test } from "@playwright/test";
import { signIn } from "./helpers";

test("evaluation view shows the held-out run, its uncertainty and safe misses for the user's pack", async ({ page }) => {
  await signIn(page, "furn-investigator");
  await page.getByRole("link", { name: "Evaluation" }).click();
  await expect(page.getByRole("heading", { name: "Evaluation" })).toBeVisible();
  await expect(page.getByText(/not evidence of real-model quality/)).toBeVisible();
  const held = page.getByRole("row").filter({ hasText: "Heldout" }).filter({ hasText: "Record" });
  await expect(held).toContainText("36/40");
  await expect(held).toContainText("95% CI");
  await held.getByRole("link").click();

  await expect(page.getByRole("heading", { level: 1 })).toContainText(/heldout · record/i);
  await expect(page.getByText("pass (interval includes values below the target)").first()).toBeVisible();
  await expect(page.getByText(/Not evaluated: Real-model quality/)).toBeVisible();
  const rules = page.locator("section", { has: page.getByRole("heading", { name: "Rules only" }) });
  const layout = rules.getByRole("row").filter({ hasText: "fur-heldout-218" });
  await expect(layout).toContainText("safe miss → escalate:assign_case");
  await expect(rules.getByRole("row").filter({ hasText: "it-heldout-" })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "Download EvalSeal records" })).toBeVisible();
});

test("requesters have no evaluation access", async ({ page }) => {
  await signIn(page, "furn-requester");
  await expect(page.getByRole("link", { name: "Evaluation" })).toHaveCount(0);
  await page.goto("/t/demo-furniture/evaluation");
  await expect(page.getByRole("heading", { name: "You don't have access to this" })).toBeVisible();
});
