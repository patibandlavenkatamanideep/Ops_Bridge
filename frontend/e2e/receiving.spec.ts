import { expect, test, type Page } from "@playwright/test";
import { createOrOpenCase, rerunInvestigation, signIn } from "./helpers";

// docs/receiving-disposition.md, section 8: the two 18-unit cases on the Receiving tab. The
// dev stack loads the synthetic receiving pack (scripts/devstack.py); rules only.

async function receiving(page: Page, invoice: string) {
  await createOrOpenCase(page, invoice);
  await rerunInvestigation(page, "rules_only");
  await page.getByRole("tab", { name: "Receiving" }).click();
  return page.getByRole("table", { name: /PO line 1 \(EA\)/ });
}

function row(table: ReturnType<Page["getByRole"]>, label: string) {
  return table.getByRole("row", { name: new RegExp(`^${label}`) });
}

test("a short delivery and a rejection with the same 18 accepted ask different questions", async ({ page }) => {
  await signIn(page, "furn-investigator");

  const a = await receiving(page, "INV-8601");
  await expect(row(a, "Delivered")).toContainText("18 EA");
  await expect(row(a, "Accepted at receiving")).toContainText("18 EA");
  await expect(row(a, "Not delivered")).toContainText("2 EA");
  await expect(row(a, "Rejected at receiving")).toContainText("0 EA");
  await expect(page.getByText("Confirm delivered quantity for PO-4601", { exact: false })).toBeVisible();

  const b = await receiving(page, "INV-8602");
  await expect(b).toHaveAccessibleName(/Rejected; no return, credit or replacement recorded/);
  await expect(row(b, "Delivered")).toContainText("20 EA");
  await expect(row(b, "Accepted at receiving")).toContainText("18 EA");
  await expect(row(b, "Rejected at receiving")).toContainText("2 EA");
  await expect(row(b, "Not delivered")).toContainText("0 EA");
  await expect(page.getByText(/damaged in transit/, { exact: false }).first()).toBeVisible();
  await expect(page.getByText("No owner is configured for rejected goods", { exact: false })).toBeVisible();
  await expect(page.getByText(/payment recommendation unresolved/)).toBeVisible();
});
