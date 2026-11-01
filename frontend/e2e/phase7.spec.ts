import { expect, test } from "@playwright/test";
import { heldOutRecordLink, signIn } from "./helpers";

test("evaluation view shows the held-out run, its uncertainty and safe misses for the user's pack", async ({ page }) => {
  await signIn(page, "furn-investigator");
  await page.getByRole("link", { name: "Evaluation" }).click();
  await expect(page.getByRole("heading", { name: "Evaluation" })).toBeVisible();
  await expect(page.getByText(/not evidence of real-model quality/)).toBeVisible();
  const held = page.getByRole("row").filter({ has: heldOutRecordLink(page) });
  await expect(held).toContainText("36/40");
  await expect(held).toContainText("95% CI");
  await heldOutRecordLink(page).click();

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

async function openHeldOutRecord(page: import("@playwright/test").Page) {
  await signIn(page, "furn-investigator");
  await page.getByRole("link", { name: "Evaluation" }).click();
  await heldOutRecordLink(page).click();
  await expect(page.getByRole("heading", { level: 1 })).toContainText(/heldout · record/i);
}

test("a scoring-1 run names its scheme and says what its flip rate measured", async ({ page }) => {
  await openHeldOutRecord(page);
  await expect(page.getByText("opsbridge-scoring/1", { exact: true })).toBeVisible();
  for (const key of ["rules_only", "mock"]) {
    const mode = page.locator(`section[aria-labelledby="mode-${key}"]`);
    await expect(mode.getByText(/case\(s\) with any flip; mean/)).toBeVisible();
    await expect(
      mode.getByText(/^Scoring 1 measured disagreement in whether the next action was correct, not in the action itself/),
    ).toBeVisible();
  }
});

async function openRerunHeldOut(page: import("@playwright/test").Page) {
  await signIn(page, "furn-investigator");
  await page.getByRole("link", { name: "Evaluation" }).click();
  await heldOutRecordLink(page, "opsbridge-bench-v1-rerun").click();
  await expect(page.getByRole("heading", { level: 1 })).toContainText(/heldout · record/i);
}

// The committed engineering re-run (opsbridge-bench-v1-rerun): a real scoring-2 result.
test("a scoring-2 run shows disagreement per axis, its purpose and no scoring-1 note", async ({ page }) => {
  await openRerunHeldOut(page);
  await expect(page.getByText("opsbridge-scoring/2", { exact: true })).toBeVisible();
  await expect(page.getByText("investigation/1.2.0", { exact: true })).toBeVisible();
  await expect(page.getByText(/^Regression re-run of the public v1 cases/)).toBeVisible();
  const rules = page.locator('section[aria-labelledby="mode-rules_only"]');
  await expect(rules).toContainText(
    "Next action: 0 case(s), mean 0 · correctness: 0 case(s), mean 0 · outcome and types: 0 case(s), mean 0 · " +
      "0 errored repeat(s)",
  );
  await expect(page.getByText(/case\(s\) with any flip/)).toHaveCount(0);
  await expect(page.getByText(/^Scoring 1 measured disagreement/)).toHaveCount(0);
});

test("the re-run's held-out gate is not decided, and the list says what the re-run is", async ({ page }) => {
  await openRerunHeldOut(page);
  const gates = page.locator("section", { has: page.getByRole("heading", { name: "Acceptance" }) });
  await expect(gates.getByText(/heldout_next_action \((rules_only|mock)\): not evaluated — the split's cases are public/))
    .toHaveCount(2);
  await page.getByRole("link", { name: "← Evaluation runs" }).click();
  const row = page.getByRole("row").filter({ has: heldOutRecordLink(page, "opsbridge-bench-v1-rerun") });
  await expect(row).toContainText("opsbridge-bench-v1-rerun");
  await expect(row).toContainText("not held-out or out-of-sample evidence");
  await expect(row).toContainText("36/40");
});
