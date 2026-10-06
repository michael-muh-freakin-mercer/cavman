import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { signUp, startBuild } from "./helpers";

/**
 * Accessibility (WCAG 2.1 AA, checked by axe-core) and small-screen layout on
 * every kind of page, signed out and signed in. These also run in Firefox,
 * WebKit (Safari's engine) and on phone-sized screens; see playwright.config.ts.
 */
const PUBLIC_PAGES = ["/", "/how-it-works", "/pricing", "/docs", "/terms", "/privacy", "/acceptable-use",
  "/sign-in", "/sign-up", "/forgot-password"];

async function audit(page: Page, path: string) {
  const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
  const problems = results.violations.map((violation) =>
    `${path}: ${violation.id} (${violation.impact}) ${violation.help}\n  ${violation.nodes.slice(0, 3).map((node) => `${node.target.join(" ")}: ${(node.failureSummary ?? "").split("\n").slice(1, 2).join("").trim()}`).join("\n  ")}`);
  expect(problems, problems.join("\n")).toEqual([]);
}

/** Loaded and hydrated. Not "network idle": run pages keep a live event stream open. */
async function settled(page: Page) {
  await page.waitForLoadState("load");
  await page.locator("#main").first().waitFor();
  await page.waitForTimeout(300);
}

async function fitsTheScreen(page: Page, path: string) {
  // Nothing may force sideways scrolling: the page is no wider than the screen.
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow, `${path} is ${overflow}px wider than the screen`).toBeLessThanOrEqual(1);
}

for (const path of PUBLIC_PAGES) {
  test(`public page ${path} is accessible and fits the screen`, async ({ page }) => {
    await page.goto(path);
    await settled(page);
    await audit(page, path);
    await fitsTheScreen(page, path);
  });
}

test("signed-in pages are accessible and fit the screen", async ({ page }) => {
  await signUp(page);
  const runId = await startBuild(page, "Booking core #approval");
  await expect(page.locator("#approvals")).toBeVisible();
  for (const path of ["/app", "/app/new", "/app/runs", "/app/projects", "/app/settings", `/app/runs/${runId}`]) {
    await page.goto(path);
    await settled(page);
    await audit(page, path);
    await fitsTheScreen(page, path);
  }
});

test("a completed run is accessible and fits the screen", async ({ page }) => {
  await signUp(page);
  const runId = await startBuild(page, "Build me a booking app for a tattoo studio");
  await expect(page.getByRole("heading", { name: "Build complete" })).toBeVisible();
  await audit(page, `/app/runs/${runId}`);
  await fitsTheScreen(page, `/app/runs/${runId}`);
});
