import { expect, test } from "@playwright/test";
import { runJson, signUp, startBuild } from "./helpers";

/**
 * The core journey in every browser and screen size the config lists: sign up,
 * start a build, follow it live to completion, decide an approval, and get the
 * verified project. The full journey set runs in Chromium.
 */
test("sign up, build, follow it live and download the result", async ({ page }) => {
  await signUp(page);
  const runId = await startBuild(page, "Build me a booking app for a tattoo studio");
  await expect(page.getByRole("heading", { name: "Build complete" })).toBeVisible();
  expect((await runJson(page, runId)).state).toBe("complete");
  await expect(page.getByRole("link", { name: "Download project" })).toBeVisible();
  const download = await page.request.get(`/api/cavman/runs/${runId}/delivery/download`);
  expect(download.status()).toBe(200);
  expect((await download.body()).length).toBeGreaterThan(100);
});

test("an approval can be read and decided", async ({ page }) => {
  await signUp(page);
  const runId = await startBuild(page, "Booking core #approval");
  const card = page.locator("#approvals");
  await expect(card.getByRole("button", { name: "Approve" })).toBeVisible();
  await card.getByRole("button", { name: "Approve" }).click();
  await expect(page.getByRole("heading", { name: "Build complete" })).toBeVisible();
  expect((await runJson(page, runId)).approvals[0].status).toBe("approved");
});
