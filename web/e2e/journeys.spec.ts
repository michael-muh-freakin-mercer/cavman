import { expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { runJson, signUp, startBuild, totp } from "./helpers";

test("journey 1: a landing-page prompt survives sign-up", async ({ page }) => {
  const prompt = "Build me a booking app for a tattoo studio";
  await page.goto("/");
  await expect(page.getByRole("heading", { level: 1 })).toContainText("We dug up a caveman who builds software.");
  await page.getByLabel("Describe the software you want").fill(prompt);
  await page.getByRole("button", { name: "Build it" }).click();
  await page.waitForURL(/\/sign-up\?next=/);
  await expect(page.getByTestId("pending-prompt")).toContainText(prompt);
  await page.getByLabel("Name").fill("Journey One");
  await page.getByLabel("Email").fill(`journey1-${Date.now()}@example.com`);
  await page.getByLabel("Password").fill("a-long-enough-password");
  await page.getByRole("button", { name: "Create account" }).click();
  await page.waitForURL(/\/app\/new\?prompt=/);
  await expect(page.getByLabel("What do you want to build?")).toHaveValue(prompt);
});

test("the dashboard requires authentication and keeps the destination", async ({ page }) => {
  await page.goto("/app/new?prompt=Build%20a%20CLI");
  await page.waitForURL(/\/sign-in\?next=/);
  await expect(page.getByTestId("pending-prompt")).toContainText("Build a CLI");
});

test("journeys 2, 3 and 6: a real run is created, streams progress, and delivers", async ({ page }) => {
  await signUp(page);
  // Before starting: the scripted executor reports no cost, so there is no history to quote, only limits.
  await page.goto("/app/new");
  await expect(page.getByTestId("cost-estimate")).toContainText("No cost history for Automatic builds yet");
  await expect(page.getByTestId("cost-estimate")).toContainText("This build stops at $5.00");
  const runId = await startBuild(page, "Build me a booking app for a tattoo studio");

  // Journey 2: the run exists in the backend and the dashboard shows it.
  const created = await runJson(page, runId);
  expect(created.id).toBe(runId);
  expect(created.executor).toBe("scripted");
  await expect(page.getByRole("heading", { name: "Booking app for a tattoo studio" })).toBeVisible();

  // Journey 3: backend changes arrive without a reload.
  await page.evaluate(() => ((window as unknown as { __noReload: boolean }).__noReload = true));
  await expect(page.getByText("Product specialist").first()).toBeVisible();
  await expect(page.getByText("Validation passed: Candidate tests")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Build complete" })).toBeVisible();
  expect(await page.evaluate(() => (window as unknown as { __noReload?: boolean }).__noReload)).toBe(true);

  // Journey 6: real deliverables.
  const download = page.getByRole("link", { name: "Download project" });
  await expect(download).toBeVisible();
  await expect(page.locator("#build-report")).toContainText("booking.py");
  const archive = await page.request.get(`/api/cavman/runs/${runId}/delivery/download`);
  expect(archive.status()).toBe(200);
  expect(archive.headers()["content-type"]).toContain("gzip");
  const final = await runJson(page, runId);
  expect(final.status).toBe("completed");
  expect(final.tasks.every((t: { state: string }) => t.state === "Accepted")).toBe(true);
  await expect(page.getByText("2 of 2 passed").or(page.getByText("3 of 3 passed"))).toBeVisible();
});

test("journey 4: an approval is shown and the exact-scoped decision reaches the backend", async ({ page }) => {
  await signUp(page);
  const runId = await startBuild(page, "Booking core #approval");
  const card = page.locator("#approvals");
  await expect(card.getByRole("heading", { name: "Grant a capability" })).toBeVisible();
  await expect(card.getByText("Executes candidate code inside the isolated sandbox")).toBeVisible();
  await expect(card.getByText(/Sandboxed development/).first()).toBeVisible();
  const pending = await runJson(page, runId);
  expect(pending.state).toBe("approval_needed");
  expect(pending.approvals[0].status).toBe("pending");

  await card.getByLabel(/Note/).fill("Reviewed the scope");
  await card.getByRole("button", { name: "Approve" }).click();
  await expect(page.getByRole("heading", { name: "Build complete" })).toBeVisible();
  const decided = await runJson(page, runId);
  expect(decided.approvals[0].status).toBe("approved");
  expect(decided.approvals[0].decision.reason).toBe("Reviewed the scope");
  expect(decided.approvals[0].decision.decided_by).toMatch(/^cavman-user:/);
});

test("journey 4b: rejecting is recorded and leaves the work honestly blocked; scope cannot be forged", async ({ page }) => {
  await signUp(page);
  const runId = await startBuild(page, "Booking core #approval");
  await expect(page.locator("#approvals").getByRole("button", { name: "Reject" })).toBeVisible();
  const pending = await runJson(page, runId);
  const approvalId = pending.approvals[0].id;
  const forged = await page.request.post(`/api/cavman/runs/${runId}/approvals/${approvalId}`, {
    data: { decision: "approve", scope_digest: "0".repeat(64) },
    headers: { Origin: "http://localhost:3100" },
  });
  expect(forged.status()).toBe(409);
  await page.locator("#approvals").getByRole("button", { name: "Reject" }).click();
  await expect(page.getByRole("status").filter({ hasText: "Blocked" })).toBeVisible();
  await expect(page.locator("#tasks").getByText("Blocked", { exact: true })).toBeVisible();
  const decided = await runJson(page, runId);
  expect(decided.approvals[0].status).toBe("rejected");
  expect(decided.status).toBe("active");
  expect(decided.capability_requests[0].status).toBe("denied");
});

test("journey 5: a failed validation is shown honestly with its recovery", async ({ page }) => {
  await signUp(page);
  const runId = await startBuild(page, "Booking core #fail-validation");
  await expect(page.getByText("Validation failed: Candidate tests")).toBeVisible();
  const failures = page.locator("#failures");
  await expect(failures.getByText("The specialist's output did not meet the task's acceptance criteria.")).toBeVisible();
  await expect(failures.getByText("Revision requested.")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Build complete" })).toBeVisible();
  await expect(page.locator("#artifacts").getByText("Rejected")).toBeVisible();
  const run = await runJson(page, runId);
  expect(run.failure_details[0].classification).toBe("BAD_OUTPUT");
  expect(run.artifacts[0].validation_state).toBe("failed");
});

test("runs are private to their owner", async ({ browser }) => {
  const owner = await browser.newPage();
  await signUp(owner);
  const runId = await startBuild(owner, "Private project");
  const stranger = await browser.newPage();
  await signUp(stranger);
  expect((await stranger.request.get(`/api/cavman/runs/${runId}`)).status()).toBe(404);
  await stranger.goto(`/app/runs/${runId}`);
  await expect(stranger.getByText("This does not exist, or it belongs to someone else.")).toBeVisible();
});

test("the API is unreachable from the browser without a session", async ({ request }) => {
  const response = await request.get("/api/cavman/runs");
  expect(response.status()).toBe(401);
  const crossSite = await request.post("/api/cavman/builds", {
    data: { prompt: "x" },
    headers: { Origin: "https://evil.example" },
  });
  expect(crossSite.status()).toBe(403);
});

test("dependent code tasks build on merged work and deliver one integrated project", async ({ page }) => {
  await signUp(page);
  const runId = await startBuild(page, "Booking API #dependent");
  await expect(page.getByRole("heading", { name: "Build complete" })).toBeVisible();
  await expect(page.locator("#activity").getByText(/^Merged into the project/)).toHaveCount(2);
  await expect(page.locator("#artifacts").getByText(/^merged [0-9a-f]{7}$/)).toHaveCount(2);
  await expect(page.locator("#build-report")).toContainText("api.py");
  await expect(page.locator("#build-report")).toContainText("booking.py");
  const run = await runJson(page, runId);
  const api = run.artifacts.find((a: { task_id: string }) => a.task_id === "api");
  expect(run.delivery.commit).toBe(api.integrated_commit);
});


test("a forgotten password is reset through the emailed link", async ({ browser }) => {
  const setup = await browser.newPage();
  const email = await signUp(setup);
  await setup.close();

  const page = await browser.newPage();
  await page.goto("/sign-in");
  await page.getByRole("link", { name: "Forgot password?" }).click();
  // The sign-in page has an Email field too: wait for the reset page before filling.
  await page.waitForURL(/\/forgot-password$/);
  await expect(page.getByRole("button", { name: "Send reset link" })).toBeEnabled();
  await page.getByLabel("Email").fill(email);
  await page.getByRole("button", { name: "Send reset link" }).click();
  await expect(page.getByRole("status")).toContainText("reset link is on its way");

  const outbox = readFileSync(join(process.env.CAVMAN_E2E_DIR!, "outbox.jsonl"), "utf8").trim().split("\n")
    .map((line) => JSON.parse(line) as { to: string; subject: string; text: string });
  const message = outbox.reverse().find((item) => item.to === email && item.subject.includes("Reset"));
  const link = message!.text.match(/https?:\/\/\S+/)![0];
  await page.goto(link);
  await page.waitForURL(/\/reset-password\?token=/);
  await page.getByLabel("New password").fill("a-brand-new-password");
  await page.getByRole("button", { name: "Set new password" }).click();
  await page.waitForURL(/\/sign-in\?reset=1/);
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill("a-brand-new-password");
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.waitForURL(/\/app$/);
});


test("pages are served with a strict CSP and run without violations", async ({ page }) => {
  const violations: string[] = [];
  page.on("console", (message) => {
    if (/Content.Security.Policy|Refused to (execute|load|apply)/i.test(message.text())) violations.push(message.text());
  });
  for (const path of ["/", "/how-it-works", "/pricing", "/docs", "/sign-in", "/does-not-exist"]) {
    const response = await page.goto(path);
    const policy = response!.headers()["content-security-policy"] ?? "";
    expect(policy).toContain("script-src 'self' 'nonce-");
    expect(policy).toContain("frame-ancestors 'none'");
  }
  await signUp(page);
  await page.goto("/app/new");
  await expect(page.getByLabel("What do you want to build?")).toBeVisible();
  await page.getByLabel("What do you want to build?").fill("Build a CLI");
  await expect(page.getByRole("button", { name: "Build it" })).toBeEnabled();
  expect(violations).toEqual([]);
});

test("an account's data can be exported and the account deleted", async ({ page }) => {
  const email = await signUp(page);
  const runId = await startBuild(page, "Build me a booking app for a tattoo studio");
  await expect(page.getByRole("heading", { name: "Build complete" })).toBeVisible();

  await page.goto("/app/settings");
  const exported = await page.request.get("/api/account/export");
  expect(exported.status()).toBe(200);
  expect(exported.headers()["content-disposition"]).toContain("cavman-export-");
  const data = await exported.json();
  expect(data.account.email).toBe(email);
  expect(data.runs.map((run: { id: string }) => run.id)).toEqual([runId]);
  expect(JSON.stringify(data)).not.toMatch(/accessToken|password/i);

  // Erasure is reachable only through the password-checked auth flow, never the API proxy.
  const viaProxy = await page.request.delete("/api/cavman/account", { headers: { Origin: new URL(page.url()).origin } });
  expect([404, 405]).toContain(viaProxy.status());

  await page.getByRole("button", { name: "Delete account" }).click();
  const confirm = page.getByRole("button", { name: "Delete permanently" });
  await expect(confirm).toBeDisabled();
  await page.getByLabel(/Type delete my account/).fill("delete my account");
  await page.getByLabel("Password", { exact: true }).fill("the-wrong-password");
  await confirm.click();
  await expect(page.getByRole("alert")).toBeVisible();
  expect((await page.request.get(`/api/cavman/runs/${runId}`)).status()).toBe(200);

  await page.getByLabel("Password", { exact: true }).fill("a-long-enough-password");
  await confirm.click();
  await page.waitForURL((url) => url.pathname === "/");
  expect((await page.request.get(`/api/cavman/runs/${runId}`)).status()).toBe(401);
  await page.goto("/sign-in");
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill("a-long-enough-password");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("alert")).toBeVisible();
});

test("forms cannot submit before the page is interactive, so fields never reach a URL", async ({ browser }) => {
  const context = await browser.newContext({ javaScriptEnabled: false });
  const page = await context.newPage();
  for (const [path, button] of [["/sign-in", "Sign in"], ["/sign-up", "Create account"], ["/forgot-password", "Send reset link"]]) {
    await page.goto(path);
    await expect(page.getByRole("button", { name: button })).toBeDisabled();
    await expect(page.locator("form").first()).toHaveAttribute("method", "post");
  }
  await page.goto("/");
  await expect(page.getByRole("button", { name: "Build it" })).toBeDisabled();
  await context.close();
});

test("a follow-up request builds on the project's delivered code", async ({ page }) => {
  await signUp(page);
  const first = await startBuild(page, "Build me a booking app for a tattoo studio");
  await expect(page.getByRole("heading", { name: "Build complete" })).toBeVisible();
  await page.getByRole("link", { name: "Ask for changes" }).click();
  await page.waitForURL(/\/app\/new\?project=[0-9a-f]{32}$/);
  await expect(page.getByText("starts from this project")).toBeVisible();
  await page.getByLabel("What do you want to build?").fill("Let clients cancel a booking #follow-up");
  await page.getByRole("button", { name: "Build it" }).click();
  await page.waitForURL(/\/app\/runs\/[0-9a-f]{32}$/);
  const second = page.url().split("/").pop()!;
  expect(second).not.toBe(first);
  await expect(page.getByRole("heading", { name: "Build complete" })).toBeVisible();
  const run = await runJson(page, second);
  expect(run.project_id).toBe((await runJson(page, first)).project_id);
  expect(run.delivery.files).toEqual(expect.arrayContaining(["booking.py", "cancel.py"]));
});

test("a user sees their signed-in devices, signs one out and changes their password", async ({ browser }) => {
  const first = await browser.newPage();
  const email = await signUp(first);
  const other = await (await browser.newContext()).newPage();
  await other.goto("/sign-in");
  await other.getByLabel("Email").fill(email);
  await other.getByLabel("Password").fill("a-long-enough-password");
  await other.getByRole("button", { name: "Sign in" }).click();
  await other.waitForURL(/\/app$/);

  await first.goto("/app/settings");
  await expect(first.getByText("This device")).toBeVisible();
  const signOutOther = first.getByRole("button", { name: /^Sign out (?!all other)/ });
  await expect(signOutOther).toHaveCount(1);
  await signOutOther.click();
  await expect(first.getByRole("button", { name: /^Sign out (?!all other)/ })).toHaveCount(0);
  expect((await other.request.get("/api/cavman/runs")).status()).toBe(401);

  await first.getByLabel("Current password").fill("a-long-enough-password");
  await first.getByLabel("New password").fill("another-long-password");
  await first.getByRole("button", { name: "Change password" }).click();
  await expect(first.getByRole("status").filter({ hasText: "Password changed" })).toBeVisible();
  await other.goto("/sign-in");
  await other.getByLabel("Email").fill(email);
  await other.getByLabel("Password").fill("another-long-password");
  await other.getByRole("button", { name: "Sign in" }).click();
  await other.waitForURL(/\/app$/);
});

test("an instruction given while a build runs is shown and reaches the work", async ({ page }) => {
  await signUp(page);
  await startBuild(page, "Build me a booking app for a tattoo studio");
  await page.getByRole("button", { name: "Add an instruction" }).click();
  await page.getByLabel("Instruction", { exact: true }).fill("Store times in UTC");
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.getByRole("list", { name: "Your instructions during the build" })).toContainText("Store times in UTC");
  await expect(page.getByRole("heading", { name: "Build complete" })).toBeVisible();
});

test("a build that needs its owner's input asks, and the answer continues it", async ({ page }) => {
  await signUp(page);
  const runId = await startBuild(page, "Booking app #questions");
  const card = page.locator("#questions");
  await expect(card.getByText("Should clients pay a deposit when they book?")).toBeVisible();
  await page.goto("/app");
  await expect(page.getByRole("heading", { name: "Waiting for your decision" })).toBeVisible();
  await page.goto(`/app/runs/${runId}`);
  await page.getByLabel("Your answers").fill("No deposit. Open 10:00 to 18:00.");
  await page.getByRole("button", { name: "Answer and continue" }).click();
  await expect(page.getByRole("heading", { name: "Build complete" })).toBeVisible();
  expect((await runJson(page, runId)).instructions[0].text).toBe("No deposit. Open 10:00 to 18:00.");
});

test("a user changes their email through a confirmation link and signs in with it", async ({ page }) => {
  await signUp(page);
  const newEmail = `moved-${Date.now()}@example.com`;
  await page.goto("/app/settings");
  await page.getByRole("button", { name: "Change email" }).click();
  await page.getByLabel("New email").fill(newEmail);
  await page.getByRole("button", { name: "Send confirmation" }).click();
  await expect(page.getByRole("status").filter({ hasText: newEmail })).toBeVisible();

  const outbox = readFileSync(join(process.env.CAVMAN_E2E_DIR!, "outbox.jsonl"), "utf8").trim().split("\n")
    .map((line) => JSON.parse(line) as { to: string; text: string });
  const link = outbox.reverse().find((item) => item.to === newEmail)!.text.match(/https?:\/\/\S+/)![0];
  await page.goto(link);
  await page.waitForURL(/\/app\/settings\?email=changed$/);
  await expect(page.getByText("Your email address was changed.")).toBeVisible();
  await expect(page.locator("#main").getByText(newEmail, { exact: true })).toBeVisible();

  const later = await (await page.context().browser()!.newContext()).newPage();
  await later.goto("/sign-in");
  await later.getByLabel("Email").fill(newEmail);
  await later.getByLabel("Password").fill("a-long-enough-password");
  await later.getByRole("button", { name: "Sign in" }).click();
  await later.waitForURL(/\/app$/);
});

test("two-factor sign-in: turn it on, sign in with a code and with a backup code, turn it off", async ({ browser }) => {
  const page = await browser.newPage();
  const email = await signUp(page);
  await page.goto("/app/settings");
  await page.getByRole("button", { name: "Turn on two-factor" }).click();
  await page.getByLabel("Your password").fill("a-long-enough-password");
  await page.getByRole("button", { name: "Turn on", exact: true }).click();
  const secret = (await page.getByTestId("totp-secret").textContent())!.trim();
  const backupCodes = await page.getByRole("list", { name: "Backup codes" }).getByRole("listitem").allTextContents();
  expect(secret).toMatch(/^[A-Z2-7]+=*$/);
  expect(backupCodes.length).toBeGreaterThanOrEqual(8);
  await expect(page.getByRole("img", { name: "QR code for your authenticator app" })).toBeVisible();
  await page.getByLabel("Code from the app, to confirm").fill(totp(secret));
  await page.getByRole("button", { name: "Confirm and turn on" }).click();
  await expect(page.getByRole("status").filter({ hasText: "Two-factor sign-in is on" })).toBeVisible();

  async function signInUntilChallenge() {
    const other = await (await browser.newContext()).newPage();
    await other.goto("/sign-in");
    await other.getByLabel("Email").fill(email);
    await other.getByLabel("Password").fill("a-long-enough-password");
    await other.getByRole("button", { name: "Sign in" }).click();
    await expect(other.getByLabel("Authentication code")).toBeVisible();
    // A password alone opens no session.
    expect((await other.request.get("/api/cavman/runs")).status()).toBe(401);
    return other;
  }

  const withCode = await signInUntilChallenge();
  await withCode.getByLabel("Authentication code").fill("000000");
  await withCode.getByRole("button", { name: "Verify" }).click();
  await expect(withCode.getByRole("alert")).toBeVisible();
  await withCode.getByLabel("Authentication code").fill(totp(secret));
  await withCode.getByRole("button", { name: "Verify" }).click();
  await withCode.waitForURL(/\/app$/);

  const withBackup = await signInUntilChallenge();
  await withBackup.getByRole("button", { name: "Lost your device? Use a backup code" }).click();
  await withBackup.getByLabel("Backup code").fill(backupCodes[0]);
  await withBackup.getByRole("button", { name: "Verify" }).click();
  await withBackup.waitForURL(/\/app$/);

  await page.reload();
  await page.getByRole("button", { name: "Turn off two-factor" }).click();
  await page.getByLabel("Your password").fill("a-long-enough-password");
  await page.getByRole("button", { name: "Turn off", exact: true }).click();
  await expect(page.getByRole("status").filter({ hasText: "Two-factor sign-in is off" })).toBeVisible();
  const plain = await (await browser.newContext()).newPage();
  await plain.goto("/sign-in");
  await plain.getByLabel("Email").fill(email);
  await plain.getByLabel("Password").fill("a-long-enough-password");
  await plain.getByRole("button", { name: "Sign in" }).click();
  await plain.waitForURL(/\/app$/);
});

test("a new account is shown how a build goes, and the docs answer common questions", async ({ page }) => {
  await signUp(page);
  await expect(page.getByRole("heading", { name: "How a build goes" })).toBeVisible();
  await page.getByRole("link", { name: "common questions" }).click();
  await page.waitForURL(/\/docs#faq$/);
  await expect(page.getByRole("heading", { name: "Questions" })).toBeVisible();
  await expect(page.getByText("What does a build cost?")).toBeVisible();
  await expect(page.getByRole("link", { name: "support@cavman.dev" })).toBeVisible();
});
