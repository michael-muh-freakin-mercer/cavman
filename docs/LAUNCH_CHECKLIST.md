# Cavman launch checklist

Everything between today and a consumer-ready hosted Cavman. Tick items as
they land and note the commit or PR.

Owner: 🧑 needs the project owner's decision, money or approval · 🤖 engineering
work that can be done now · 👥 needs outside people.

Approvals on record: live-model campaign spend up to **$50** (OpenRouter); misc budget **$50 a month** for model credit for free builds and demos, E2B and similar (2026-10-02; marketing has its own budget).

## P0: can't launch without these

### 1. Prove it works with real models
- [x] 🧑 OpenRouter key available to the environment (`OPENROUTER_API_KEY`) and `openrouter.ai` allowed by its network policy; spend approved up to $50
- [x] 🤖 Campaign: ~10 prompts across Python, TypeScript and documents; record success rate, cost and failure causes (`docs/live-campaign/`: 10/10 complete, median $0.09)
- [x] 🤖 Tune planner and specialist prompts, turn limits and recovery from the results. Bar: ≥70% complete, median cost under the $5 default budget (met: 100%, $0.09)
- [x] 🤖 Confirm OpenRouter reports cost on this path so USD budgets enforce real numbers (matches OpenRouter's usage counter within $0.005)
- [x] 🤖 Opt-in, budget-capped live smoke in CI (`.github/workflows/live-smoke.yml`, run by hand from Actions)
- [x] 🧑 Add the `OPENROUTER_API_KEY` repository secret so it can run (set 2026-09-28; first CI run 2026-09-30)
- [x] 🧑 First live build on cavman.dev (2026-09-30: a Linux secret-folder CLI, $0.35, 54 tests passing)
- [x] 🤖 Review must check delivered work against the plan, not just tests: that first build passed every review while missing planned items (launcher shortcut not delivered, vault not hidden) and taking secrets as command-line arguments (shell history) with non-atomic saves (reviewers now rule on every plan item and report findings by severity; an unmet or unruled item or a high or critical finding fails the review)
- [x] 🤖 Confirm the stricter review with real models (live smoke 2026-09-30: 3 of 3 builds completed, $1.00; see `docs/live-campaign/`)
- [x] 🤖 Measure what the stricter review costs (2026-10-02, `docs/live-campaign/README.md`: wide variance, mostly from reviews failed for reading no file and sent back as if the work were wrong; fixed): builds ran $0.14 to $0.67 against a $0.09 median before, and one took 33 minutes. Find out why reviewers asked for revisions and whether the requests were sound
- [ ] 🤖 Check the finished project as a whole against each success criterion shared between tasks (reviews are per task today)

### 2. Stronger isolation for untrusted code
- [x] 🧑 Choose a microVM or managed sandbox: E2B (free plan to start; upgrade before public launch for longer sandbox lifetimes and more concurrency)
- [x] 🤖 Implement it behind `ExecutionBackend`, keeping the fail-closed sandbox tests (`CAVMAN_SANDBOX_BACKEND=e2b`, template in `scripts/e2b_template.py`, live tests in the E2B sandbox workflow)
- [x] 🧑 Build the E2B template once and set `CAVMAN_SANDBOX_BACKEND=e2b` and `E2B_API_KEY` on the workers (template `cavman-sandbox`; isolation probe passed on cavman-1, 2026-09-29)
- [x] 🤖 If workers stay in containers, replace `seccomp=unconfined` with a hardened profile (`deploy/seccomp-worker.json`: Docker's default plus the six calls Bubblewrap needs; the CI job `deploy` runs under it. cavman.dev workers use E2B and need none of it)

### 3. Deployment
- [x] 🧑 Approve infrastructure: DigitalOcean (one 8 GB / 4 vCPU Droplet, managed Postgres, block storage volume, Caddy for TLS); domain cavman.dev
- [x] 🤖 Step-by-step DigitalOcean guide and production Compose overlay ([DEPLOY_DIGITALOCEAN.md](DEPLOY_DIGITALOCEAN.md))
- [x] 🤖 Verify `deploy/web.Dockerfile` and `deploy/compose.yaml` end to end (`deploy/e2e.sh`, CI job `deploy`: all 15 journeys pass)
- [x] 🤖 Verify Better Auth on Postgres (same run)
- [x] 🧑 Single host or several: one Droplet for now (owner, 2026-10-01). Multi-host on Postgres stays unverified until a second machine is wanted; two workers sharing one queue on one host pass
- [x] 🤖 SQLite → Postgres data migration tool (`cavman ops migrate-to-postgres`, auth included)
- [x] 🧑 Approve the first deploy (live at https://cavman.dev on Droplet cavman-1, 2026-09-29)

### 4. Security review
- [ ] 👥 External review or pen test: sandbox, browser-to-API proxy and CSP, authentication, repository import, GitHub token handling, account deletion
- [x] 🤖 Secret scanning in CI (`scripts/check_secrets.py`, `.secrets.baseline`)
- [x] 🤖 Written threat model in the docs (`docs/THREAT_MODEL.md`)

### 5. Email
- [x] 🧑 Resend account, sending domain verified (SPF and DKIM): cavman.dev, sender no-reply@cavman.dev; inbound support@cavman.dev forwards via Cloudflare Email Routing (2026-09-30)
- [x] 🤖 Require email verification in hosted mode; test reset and verification against the real provider (`CAVMAN_REQUIRE_EMAIL_VERIFICATION=1` on cavman.dev; password reset delivered through Resend 2026-09-30; a fresh sign-up's verification email delivered and confirmed by the owner 2026-10-02)

### 6. Legal
- [x] 🧑👥 Terms of service, privacy policy, acceptable-use policy: live at `/terms`, `/privacy` and `/acceptable-use` (signed off by the owner 2026-09-30; not reviewed by a lawyer)
- [x] 🧑👥 List of third parties processing user data (OpenRouter and model providers, email, hosting): listed in the privacy policy (signed off by the owner 2026-09-30; not reviewed by a lawyer)
- [x] 🧑👥 Data retention policy, cookie notice, minimum age: in the privacy policy and terms; minimum age 18 (signed off by the owner 2026-09-30; not reviewed by a lawyer)
- [x] 🤖 Pages and links in the app (data export and account deletion already exist): live at `/terms`, `/privacy`, `/acceptable-use`; owner facts in `web/lib/legal.ts`

### 7. Abuse and tenancy limits
- [x] 🤖 Per-user rate limits on starting builds, imports and continuations (only sign-in is rate limited today)
- [x] 🤖 Per-account caps: disk, projects, concurrent builds
- [x] 🤖 CAPTCHA or equivalent on sign-up (Cloudflare Turnstile on sign-up and password-reset requests; off until keys are set)
- [x] 🧑 Create a Turnstile widget in Cloudflare and set `TURNSTILE_SITE_KEY` / `TURNSTILE_SECRET_KEY` on the web app (live on cavman.dev, 2026-09-30)
- [x] 🤖 Authenticated GitHub requests for imports (anonymous limit is 60 per hour per IP)
- [x] 🧑 Prompt and content policy: what gets refused (Acceptable Use Policy, approved by the owner 2026-09-30)

### 8. Operations
- [x] 🤖 Scheduled cleanup of finished runs' worktrees and stale dependency caches (delivery archives are kept; they count toward the account's disk cap)
- [x] 🤖 Dashboards and alerts: queue age, failed jobs, sandbox health, spend (`deploy/monitoring/`; needs a Prometheus and Grafana to run in, chosen with error reporting below)
- [x] 🧑 Choose error reporting (e.g. Sentry) and log hosting (Sentry live on cavman.dev since 2026-09-30 for the API, workers and web server; Grafana Cloud chosen for logs 2026-09-30)
- [x] 🤖 Integrate them (Sentry in the API, workers and web server, off until `SENTRY_DSN` is set; log shipping to Grafana Cloud with Alloy in `compose.prod.yaml`, off until `COMPOSE_PROFILES=logs`; both listed in the privacy policy)
- [x] 🧑 Create the Grafana Cloud stack, set `GRAFANA_LOKI_*` and `COMPOSE_PROFILES=logs` on cavman.dev, and confirm logs arrive (2026-10-01, after #40: Alloy pushing to Grafana Cloud, no dropped entries)
- [x] 🤖 Runbook: stuck runs, worker outage, restore from backup (`docs/RUNBOOK.md`)
- [x] 🤖 Worker capacity on one host: `CAVMAN_WORKER_CONCURRENCY` (idle slots cost nothing). Autoscaling across machines is deferred with multi-host (owner, 2026-10-01)
- [x] 🧑 Backups for Postgres and the shared volume (nightly at ~07:30 UTC, encrypted, off-server to the `cavman-backups` Space: `deploy/backup/`; live since 2026-09-30)
- [x] 🤖 Tested restore drill (`deploy/backup/restore-drill.sh`: runs in CI on every push; first production drill passed 2026-09-30, logged in RUNBOOK.md)

## P1: needed for a good launch

### 9. Billing (if paid)
- [x] 🧑 Pricing and plans (approved 2026-10-02: free during the beta at a $1 monthly allowance; then Free with $1 of usage a month, Builder at $20 a month with $12 of usage, and $10 top-ups for $6 of usage; beta users get a discount)
- [ ] 🧑 Open and approve a Stripe account
- [ ] 🤖 Checkout, metering tied to account caps, invoices, billing page in Settings

### 10. Setting expectations about what Cavman can build
- [x] 🤖 Say in the UI which stacks get real tests (Python, Node/TypeScript) and which are reviewed code only
- [x] 🤖 Warn before mobile or unsupported-stack builds
- [x] 🤖 Run project build scripts (`npm run build`), not just tests (check `npm_build`: the build script runs in the network-denied jail on a scratch copy, without pre/post hooks)
- [x] 🤖 Cost estimate before a build starts (from what recent completed builds here actually cost, per model mode; plus the ceiling and the monthly allowance left)

### 11. Talking with a build
- [ ] 🤖 Clarifying questions as first-class "Cavman needs your input" requests
- [ ] 🤖 Instructions mid-run (workflow mode rejects messages today)
- [ ] 🤖 Email when a build finishes or needs an approval

### 12. GitHub
- [ ] 🤖 Publish flow end to end against github.com, including the scope upgrade (tested against a stand-in only)
- [x] 🤖 Import a real public repository (`tests/test_github_live.py` imports octocat/Hello-World from github.com through the API; an advisory CI job runs it on every push)
- [x] 🤖 Private repository import with the user's token (only the importing user's own token, with the `repo` scope, can import a private repository; the operator token never can; the token is used for the download only)
- [ ] 🤖 Push updates to an existing repository as a pull request

### 13. Safe previews of built web apps
- [ ] 🧑 Approve a separate preview domain and isolated preview cluster
- [ ] 🤖 Isolated, time-limited, credential-free previews in a sandboxed frame or link

### 14. Account
- [x] 🤖 Session management (see and revoke signed-in devices)
- [x] 🤖 Change password in Settings
- [x] 🤖 Change email in Settings (the current address approves the change, then the new one is verified)
- [x] 🤖 Optional two-factor authentication (authenticator app plus encrypted single-use backup codes; guards password sign-in, GitHub sign-in relies on GitHub's own)

### 15. Quality
- [x] 🤖 Accessibility audit to WCAG AA (axe-core, WCAG 2.1 A and AA, on every page type in every browser in CI: `web/e2e/quality.spec.ts`; fixed low-contrast green and grey text, a link told apart only by colour, and a scroll area keyboards could not reach. Not yet done: a manual pass with a screen reader)
- [x] 🤖 Mobile layout check (no page wider than the screen on Pixel 7 and iPhone 14 sizes, plus the core build journey on both)
- [x] 🤖 Firefox and Safari (the accessibility, layout and core build journeys run in Firefox and WebKit, Safari's engine, in CI; the full journey set stays on Chromium)
- [x] 🤖 Load test: concurrent builds and live-update connections (`scripts/load_test.py`, results in `docs/LOAD_TEST.md`: 100 builds and 500 live connections with no errors; worker slots, not the platform, limit cavman.dev)
- [x] 🤖 Pagination on run and project lists; streamed export for large accounts (keyset paging, 25 runs or 24 projects a page; the export streams one run at a time through the web server)

### 16. Onboarding and support
- [x] 🤖 First-run guidance, empty states, help/FAQ, pricing page (empty states and pricing were in place; the empty overview now shows how a build goes, and the docs have a Questions section)
- [x] 🧑 Support contact channel (email only for now: support@cavman.dev, owner's decision 2026-10-02)

## P2: soon after launch
- [x] 🤖 Keep or retire manager mode (retired 2026-10-02, owner's decision: every build runs the workflow driver, and `CAVMAN_ORCHESTRATION=manager` is refused at startup)
- [ ] 🤖 More sandbox stacks (Go, Rust, Java)
- [x] 🤖 Compare Budget / Balanced / Maximum Quality with real models (2026-10-02: deepseek-v4-flash too weak; suggest deepseek-v4-pro for Budget and Balanced, claude-sonnet-5.5 for Maximum Quality; set `CAVMAN_MODELS_*` to offer them)
- [x] 🤖 Dependency update automation (Dependabot weekly for pip, npm and GitHub Actions: `.github/dependabot.yml`)
- [x] 🤖 Plan the CI runner move from Ubuntu 24.04 deliberately (`docs/runbooks/ci-runner-move.md`; an advisory job already runs the suite on Ubuntu 26.04)
- [ ] 🧑 Privacy-respecting product analytics for the sign-up → first build funnel

## Suggested order
1. Live-model campaign (1)
2. Tenancy limits and ops basics (7, 8) and the 🤖 parts of 10 and 12
3. Isolation and deployment decisions (2, 3)
4. Email and legal (5, 6)
5. Security review (4)
6. Private beta
7. Billing and previews (9, 13)
8. Public launch
