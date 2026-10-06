import { defineConfig, devices } from "@playwright/test";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

/**
 * End-to-end tests run the real stack: the Cavman API, a durable worker using
 * the scripted test executor (scripted models, real kernel, real sandbox), and
 * the production web build with real authentication.
 */
process.env.CAVMAN_E2E_DIR ??= mkdtempSync(join(tmpdir(), "cavman-e2e-"));
const dir = process.env.CAVMAN_E2E_DIR;
const here = dirname(fileURLToPath(import.meta.url));
const repo = resolve(here, "..");
const python = process.env.CAVMAN_PYTHON_BIN ?? join(repo, ".venv/bin");
const apiPort = 8765;
const webPort = 3100;
const token = "e2e-service-token-0123456789abcdef0123456789";
// Set by deploy/e2e.sh: the stack is already running (docker compose), so
// Playwright only drives the browser against it.
const external = process.env.CAVMAN_E2E_BASE_URL;

const OTHER_BROWSERS = /(quality|cross-browser)\.spec\.ts$/;

const shared = {
  CAVMAN_API_TOKEN: token,
  NEXT_TELEMETRY_DISABLED: "1",
};

export default defineConfig({
  testDir: "./e2e",
  timeout: 120_000,
  expect: { timeout: 60_000 },
  fullyParallel: false,
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  use: {
    baseURL: external ?? `http://localhost:${webPort}`,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  // Every journey runs in Chromium. Accessibility and layout checks, and the core
  // build journey, also run in Firefox, WebKit (Safari's engine) and on phones.
  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"] } },
    { name: "firefox", use: { ...devices["Desktop Firefox"] }, testMatch: OTHER_BROWSERS },
    { name: "webkit", use: { ...devices["Desktop Safari"] }, testMatch: OTHER_BROWSERS },
    { name: "mobile-chrome", use: { ...devices["Pixel 7"] }, testMatch: OTHER_BROWSERS },
    { name: "mobile-safari", use: { ...devices["iPhone 14"] }, testMatch: OTHER_BROWSERS },
  ],
  webServer: external ? [] : [
    {
      // The worker runs beside the API; both stop when the tests finish.
      command: `sh -c '${python}/cavman worker & exec ${python}/cavman api --port ${apiPort}'`,
      cwd: repo,
      url: `http://127.0.0.1:${apiPort}/api/health`,
      env: {
        ...shared,
        CAVMAN_EXECUTOR: "scripted",
        CAVMAN_DATA_DIR: join(dir, "data"),
        CAVMAN_SCRIPTED_STEP_DELAY: "0.15",
        CAVMAN_HEARTBEAT_SECONDS: "0.5",
      },
      reuseExistingServer: false,
      timeout: 60_000,
    },
    {
      command: process.env.PW_SKIP_BUILD ? `npx next start --port ${webPort}` : `npx next build && npx next start --port ${webPort}`,
      cwd: here,
      url: `http://localhost:${webPort}/`,
      env: {
        ...shared,
        CAVMAN_API_URL: `http://127.0.0.1:${apiPort}`,
        BETTER_AUTH_SECRET: "e2e-auth-secret-0123456789abcdef0123456789",
        BETTER_AUTH_URL: `http://localhost:${webPort}`,
        // A postgres:// URL runs the journeys against Better Auth on Postgres.
        AUTH_DATABASE_URL: process.env.AUTH_DATABASE_URL ?? join(dir, "auth.db"),
        CAVMAN_DEV_OUTBOX: join(dir, "outbox.jsonl"),
        CAVMAN_E2E: "1",
      },
      reuseExistingServer: false,
      timeout: 300_000,
    },
  ],
});
