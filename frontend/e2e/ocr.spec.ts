import { expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { createOrOpenCase, signIn } from "./helpers";

// The optional OCR path end to end: an image-only invoice is read, but only as labelled
// candidates for review. Needs tesseract and OPSBRIDGE_OCR_ENABLED=true in the stack; skipped
// otherwise, unless OPSBRIDGE_REQUIRE_OCR=1 (hosted CI), where that fails instead.
const RASTER = readFileSync(
  fileURLToPath(new URL("../../fixtures/documents/rasterized_invoice.pdf", import.meta.url)),
);

test("a scanned invoice is read by OCR but shown only as candidates for review", async ({ page }) => {
  await signIn(page, "furn-investigator");
  await createOrOpenCase(page, `INV-OCR-${Date.now()}`); // its own case: no other spec sees this upload
  await page.getByRole("tab", { name: "Evidence" }).click();
  await page.getByRole("button", { name: "Upload a document" }).click();
  await page.getByLabel("Document type").selectOption("invoice");
  await page.getByLabel(/^File/).setInputFiles({
    name: `scanned-invoice-${Date.now()}.pdf`,
    mimeType: "application/pdf",
    buffer: RASTER,
  });
  await page.getByRole("button", { name: "Upload", exact: true }).click();
  await expect(page.getByRole("status").filter({ hasText: "Stored as version" })).toContainText(/needs review/i, {
    timeout: 30_000,
  });
  await page.getByText("Uploaded invoice").first().click();
  const meta = page.locator("#fields-title + p");
  await expect(meta).toContainText("no extractable text layer");
  if (/OCR is not enabled|tesseract is not installed/.test(await meta.innerText())) {
    test.skip(process.env.OPSBRIDGE_REQUIRE_OCR !== "1", "OCR is not enabled in this stack");
    throw new Error("OPSBRIDGE_REQUIRE_OCR=1 but the stack did not run OCR");
  }
  await expect(meta).toContainText(/text read by OCR \(tesseract [\d.]+\)/);
  await expect(page.getByRole("note")).toContainText("Read by OCR from the page image");
  await expect(page.locator(".source")).toContainText("INV-CUPS-1");
  const row = page.getByRole("row").filter({ hasText: "invoice.invoice_number" });
  await expect(row).toContainText("INV-CUPS-1");
  await expect(row).toContainText(/needs review/i);
  await expect(row).toContainText("not used by the matching rules");
  await expect(row).toContainText(/ocr:page=1;line=\d+/); // line numbers follow the OCR layout
  await expect(row.getByRole("button", { name: "Correct" })).toHaveCount(0);
});
