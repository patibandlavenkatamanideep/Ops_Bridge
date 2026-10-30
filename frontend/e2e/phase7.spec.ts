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

// Rendering only: no scoring-2 result is committed (opsbridge-bench-v2 has not been run), so the
// API's own held-out run is served with its instability in the scoring-2 shape
// (evaluation/metrics.py instability_2) and its scheme set to scoring 2.
test("a scoring-2 run shows disagreement per axis and no scoring-1 note", async ({ page }) => {
  await page.route(/\/evaluations\/opsbridge-bench-v1\/heldout-record-\d{8}T\d{6}Z$/, async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    const axis = (k: number, mean: number) => ({ cases_with_disagreement: k, cases_with_disagreement_rate: k / 40, mean });
    for (const mode of Object.values(body.modes) as Record<string, unknown>[]) {
      mode.instability = {
        scheme: "opsbridge-scoring/2",
        definition: "per case, 1 - (count of the most common value) / repeats, on each axis",
        action: axis(2, 0.0333),
        correctness: axis(1, 0.0167),
        outcome_types: axis(0, 0),
        errors: { trials_with_error: 1 },
        repeats_per_case: 3,
        sampling_limit: "3 repeats per case",
      };
      mode.instability_note = null;
    }
    body.scoring_scheme = "opsbridge-scoring/2";
    await route.fulfill({ response, json: body });
  });
  await openHeldOutRecord(page);
  await expect(page.getByText("opsbridge-scoring/2", { exact: true })).toBeVisible();
  const rules = page.locator('section[aria-labelledby="mode-rules_only"]');
  await expect(rules).toContainText(
    "Next action: 2 case(s), mean 0.0333 · correctness: 1 case(s), mean 0.0167 · outcome and types: 0 case(s), " +
      "mean 0 · 1 errored repeat(s)",
  );
  await expect(page.getByText(/case\(s\) with any flip/)).toHaveCount(0);
  await expect(page.getByText(/^Scoring 1 measured disagreement/)).toHaveCount(0);
});
