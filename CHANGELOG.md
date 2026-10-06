# Changelog

## 2026-10-01 — The finished project is checked as a whole

- Each task's reviewer rules only on the success criteria that task alone covers. A criterion several tasks share was never checked anywhere. Now, once every task is accepted and integrated, and before the run completes, the project's own checks (pytest, compile, node:test, tsc, whichever its tasks used) run together on the integrated code, and a fresh reviewer reads that code and rules on each shared criterion. A failed check, an unmet or unruled criterion, a high or critical finding, or a reviewer who read no file fails the review, whatever the reviewer claimed.
- When it fails, Cavman proposes one more task, written from the reviewer's suggested fix, to make the project meet the missed criteria. Adding a task changes the plan, so the kernel asks the owner to approve it: the Change the plan card says what the review found. If approved, the task runs and the project is reviewed again. If declined, the run completes and its result says which criteria the review found unmet. If no plan revisions are left, the result says so too.
- Runs with no shared criterion are unchanged. A passing review is noted in the run's result.

## 2026-10-02 — Email when a build needs you

- When a build is ready, waits for a decision, reaches its budget or stops early, its owner gets an email with a link to the run. Builds can take half an hour, so people no longer have to keep the tab open. A build the user stopped themselves is not emailed.
- The API lists finished jobs nobody has been told about at `GET /api/notices` and records each one at `POST /api/notices/{job_id}/sent`; both need the service token and are about no single user. Jobs that ended more than six hours earlier are never offered, so a server's first start does not mail old builds. Account deletion removes the records.
- The web server, which holds the email provider and the addresses, checks every `CAVMAN_NOTICE_POLL_SECONDS` (default 60) and emails only verified addresses. A failed send is retried on the next pass. `CAVMAN_BUILD_EMAILS=0` turns it off for the server.
- Settings has a new Email panel with "Email me about my builds", on by default.
- A build that stops to ask its owner questions before planning is emailed too ("Cavman has questions about your build").

## 2026-10-02 — Cavman can ask before it plans

- When a request is ambiguous in a way that changes what gets built, the planner may return up to three short questions instead of a plan. It may do this once per run. The run then shows **Needs your input** with the questions, and appears under "Waiting for your decision" on the overview. The answer, given on the run page, is stored as a run instruction and continues the build. The planner then gets the questions and answers and must plan, using sensible defaults for anything still open. A second round of questions is refused and the planner retries.
- The API reports the state `input_needed` and the run detail lists `questions`. Nothing is spent on specialists before the answer.

## 2026-10-01 — Give a build instructions while it runs

- The run page has *Add an instruction* while a build is executing, and the *Continue* dialog's instruction box now works in workflow mode too (it used to be refused). Instructions are stored beside the run (`run_instructions`, at most 20 a run), listed under the request on the run page, and included in the export through the run detail.
- The workflow driver reads them at the start of every round. Specialists starting work from then on get them in their instructions, newest winning over the task packet where they conflict. Reviewers get them as `owner_instructions` and fail work that ignores one that applies. Work already accepted is not redone; a follow-up run is the way to change it.
- New `POST /api/runs/{id}/instructions`. Manager mode keeps its own path (the message goes to the Manager on Continue).

## 2026-10-06 — Paid plans through Stripe

- Settings has a Billing panel when Stripe is configured (`STRIPE_SECRET_KEY` and `STRIPE_WEBHOOK_SECRET`; hidden otherwise, so the free beta is unchanged). Builder ($20 a month, $12 of usage, a free first 30 days) and $10 top-ups ($6 of usage that does not expire) go through Stripe-hosted Checkout with Managed Payments: Stripe is the merchant of record and handles tax, fraud, disputes and receipts. "Invoices and plan" opens Stripe's customer portal.
- The account's monthly limit is now its plan's allowance plus top-up credit left; the call cap grows with it. Credit pays only for spend beyond a month's allowance, settled once per month.
- Stripe's events reach `/api/stripe/webhook` on the web server, which relays them to the API to verify. Subscription state is read back from Stripe, each payment grants credit once, a refund takes back its share, and deleting an account cancels its subscription.
- New dependency: `stripe` (Python, 16.x). The privacy policy lists Stripe. Setup and the plan rules are in `docs/BILLING.md`.

## 2026-10-02 — The site speaks to developers

- Link previews and the browser tab now say "Cavman — AI builds that have to prove they work", with a description naming who it is for (developers) and what it does best (Python and TypeScript libraries, CLIs and API cores, handed over after real tests and a second review). The dig-site joke stays in the landing page's headline.
- The last web-app examples are gone: the How it works flow and the landing page's example run now show a Python CLI that renames photos by date, with Python, TypeScript, API, test and docs specialists, and the new-build placeholder models a good, testable request.

## 2026-10-06 — Sentry 11

- `@sentry/nextjs` 10 → 11. Sentry 11 drops `sendDefaultPii` and collects every kind of data by default, now including local variable values in stack frames. The web server turns each category off in `dataCollection` (user, cookies, bodies, query strings, response headers, stack-frame variables and the rest) and keeps only the request headers `scrubEvent` already kept, so reports carry no more than before.

## 2026-10-02 — Manager mode is retired

- Every build now runs the workflow driver, where plain code drives plan, delegate, validate, review and accept, and models only plan, build and review. The original mode, where a Manager model drove each step through tool calls, used far more model calls and was never used on cavman.dev. It is gone, along with its scripted test models, the `--orchestration` flag of the live campaign, `CAVMAN_MANAGER_MAX_TURNS`, and the per-run conversation sessions it kept in local SQLite.
- `CAVMAN_ORCHESTRATION=manager` now stops the API and worker at startup with a message saying so, instead of quietly running something different. `workflow` is still accepted.
- Runs and the system view no longer report an `orchestration` field. The Continue dialog no longer has an instruction box. It only appeared in manager mode.
- Erasing an account still deletes any old manager-mode sessions for its runs.

## 2026-10-06 — source-map-js 1.2.2

- `source-map-js` 1.2.1 → 1.2.2 in the web lockfile for GHSA-68fv-2mgg-jv7q (high: event-loop denial of service from crafted source maps). The advisory appeared after 2026-10-05 and failed `npm audit` in CI on every branch.

## 2026-10-02 — Start from a private GitHub repository

- A new project can start from a private GitHub repository the user can read. The web server reads the user's own GitHub token from the encrypted auth store, only when the account has granted the `repo` scope and the request names a repository. It sends the token to the API in an `X-Cavman-GitHub-Token` header the browser cannot set. The importer uses it for GitHub's metadata and the download, and never stores or logs it.
- The operator's import token (`CAVMAN_GITHUB_IMPORT_TOKEN`) still only raises rate limits: a private repository is refused unless the importing user's own token is present.
- When a repository is private, or not found without a token, the API answers with `needs_scope: "repo"`. The New build form then offers "Give Cavman access to your GitHub repositories" and, after GitHub, returns to the form with the request and repository filled in. The project records whether its source was private.

## 2026-10-01 — Accessibility, phones, Firefox and Safari

- New end-to-end checks (`web/e2e/quality.spec.ts`): every public page, every signed-in page, a run waiting for approval and a completed run are scanned with axe-core for WCAG 2.1 A and AA, and must be no wider than the screen.
- The same checks and the core journeys (`web/e2e/cross-browser.spec.ts`: sign up, build, follow it live, decide an approval, download) run in Chromium, Firefox, WebKit (Safari's engine) and at Pixel 7 and iPhone 14 sizes. The full journey set stays on Chromium; the Compose deployment job runs Chromium only.
- WebKit (Safari's engine) could not sign in on the plain-HTTP test stack: the production CSP's `upgrade-insecure-requests` made it send the page's own requests to `https://localhost`, which Chromium and Firefox exempt but WebKit does not. The directive is now sent only when the page came over HTTPS, directly or through Caddy's `X-Forwarded-Proto`, which is always the case on cavman.dev.
- Fixed what the scan found: the "ok" green (`#16803f` to `#126b34`) and the faint grey (`#6f7369` to `#5f625a`) now reach 4.5:1 on their backgrounds, the docs link to the setup guide is underlined, not just coloured, and the run's activity list can be scrolled from the keyboard.

## 2026-10-02 — Reviewers must read the work, and are asked again if they do not

- Live smokes showed the same request costing $0.03 in one build and $0.65 in another. In one case both reviewers ruled every item met, and the work was sent back anyway, because they had judged from the diff in their input without opening a file. Trusted code fails such a review, but it said so only in the evidence. The specialist got a reason saying everything was fine and spent 79 calls redoing working code.
- Reviewers are now told to open the candidate's files before ruling. One that reads nothing is asked once more. If it still reads nothing, the review fails with a reason that says the verdict was discarded for that reason.
- Findings and the model comparison are in `docs/live-campaign/README.md`.
- The real-github.com import check sends the workflow's own token, so parallel CI runs no longer share GitHub's anonymous rate limit.

## 2026-10-01 — Change your email, and optional two-factor sign-in

- Settings > Account can change the account's email. A verified address must approve the change by a link sent to it first, then the new address confirms by its own link, so a stolen session cannot quietly move the account. An unverified address gets only the second link.
- Settings > Two-factor sign-in turns on a code from an authenticator app (Better Auth's two-factor plugin): scan a QR code or type the key, confirm with a code, and save ten single-use backup codes. Signing in with a password then asks for a code or a backup code, with an option to trust the device for 30 days. New backup codes and turning it off need the password. The TOTP secret and the backup codes are stored encrypted. GitHub sign-in is not gated; it relies on GitHub's own two-factor.
- The plugin adds a `twoFactor` table and a `twoFactorEnabled` user column through the automatic auth migration. The privacy policy mentions the encrypted key and codes.
- New dependency: `uqr` (MIT, no dependencies) draws the QR code as an SVG in the browser.

## 2026-10-01 — Paged run and project lists, streamed export

- `GET /api/runs`, `GET /api/projects` and `GET /api/projects/{id}` return one page, newest first (`limit`, 1 to 100), with a `next` cursor for the page after it (`before=`). Paging is by (created, id), so builds started while someone pages do not shift or repeat items. A project still reports its full `run_count` and its latest run on every page.
- The Runs, Projects and project pages show 25 runs or 24 projects with Older and Newest links. The overview reads the newest 50 runs instead of all of them.
- Projects no longer load every run to count them: one count query and the latest run.
- The account export streams one run at a time from the API through the web server, so a large account is never built in memory. The file's content is unchanged.

## 2026-10-01 — First-run guidance and a FAQ

- A new account's overview shows how a build goes in four steps (describe it, watch it work, decide when asked, take it away), with links to the docs and the questions.
- The docs page has a Questions section: what Cavman builds well, cost, the monthly allowance, why a build waits, running the result, privacy and self-hosting, plus the support address.
- The docs now cover starting from a public GitHub repository, asking for changes, and publishing to GitHub, and no longer say publishing "is not wired yet".

## 2026-10-02 — Load test

- `scripts/load_test.py` drives the real API and worker processes with many accounts' builds and live-update connections at once, using scripted models, so it costs nothing. 100 concurrent builds with 500 open streams all completed, with no stream errors and an API p95 of 240 ms. Results and what they mean for cavman.dev are in `docs/LOAD_TEST.md`: the single worker slot, not the platform, is the limit.

## 2026-10-02 — Repository import proven against github.com

- `tests/test_github_live.py` (opt-in with `CAVMAN_LIVE_GITHUB=1`) imports GitHub's public example repository through the API, the way a user's build does, and checks the result: one local commit, no upstream remote or shallow state. It also checks that a missing or private repository is refused by the real API. An advisory CI job runs it on every push. It cannot fail the build, so a GitHub outage does not block merges.

## 2026-10-01 — Rehearse the move to Ubuntu 26.04 runners

- An advisory CI job runs the backend suite on `ubuntu-26.04`, which has Python 3.14 as its system interpreter, the one the sandbox uses. The job cannot fail the build and is not a required check. `docs/runbooks/ci-runner-move.md` lists what depends on the runner image and the steps to move.

## 2026-10-01 — Builds prove the project's own build script

- New trusted check `npm_build` runs the project's `npm run build` in the same network-denied jail as the Node tests, with dependencies from the isolated installer. The workspace stays read-only: the sources are copied to scratch, `node_modules` is linked in read-only, and the output is thrown away. Pre- and post-build hooks are not run.
- The planner is told to require it when `package.json` has a `build` script, so a bundler or framework build that fails no longer passes because the tests did. Specialists can run it with `run_check("npm_build")`. A task with no `build` script fails the check with a plain reason.
- Limits: two minutes, the usual Node memory cap and 32 MB of scratch. Dev servers are still not run.

## 2026-10-01 — Campaign reports say why work was sent back

- A live campaign report now lists every failed check and every review that asked for changes, in order, with the reviewer's reason, plus the number of candidates and the cost by role (planner, specialist, reviewer) for each build. The 2026-09-30 smoke showed review had made builds dearer but recorded nothing about why revisions were requested.
- The report is rewritten after every build, marked partial until the campaign ends, and `--deadline-minutes` stops new builds from starting late. The live smoke uses 70 minutes against its 90-minute job timeout. A smoke on 2026-10-02 hit that timeout and left no report, although it had spent money.

## 2026-09-30 — Worker containers keep Docker's seccomp filter

- `deploy/compose.yaml` ran Bubblewrap workers with `seccomp=unconfined`, which switched off the container's whole system-call filter to let Bubblewrap create a user namespace. They now run under `deploy/seccomp-worker.json`: Docker's default profile plus the six calls Bubblewrap needs (`clone`, `unshare`, `mount`, `umount2`, `pivot_root`, `sethostname`). Everything else the default denies stays denied.
- `apparmor=unconfined` and `systempaths=unconfined` are unchanged; Bubblewrap cannot mount `/proc` without them.
- `scripts/worker_seccomp.py` rebuilds the profile from Docker's default. `deploy/e2e.sh` fails if the worker container is not seccomp-filtered.
- cavman.dev is not affected: its workers use E2B and `compose.prod.yaml` already drops all three options.

## 2026-09-30 — Review checks the build against its plan

- The first live build on cavman.dev passed every review while a planned launcher shortcut was never delivered, the vault folder was not hidden, secrets were taken as command-line arguments and saves were not atomic. Reviewers returned one overall verdict, and green tests were enough to earn it.
- A reviewer now gets a numbered list of plan items built from the task packet (the planned deliverable, each acceptance criterion, each constraint, and in workflow mode each run success criterion only that task covers) together with the user's request, and must return a verdict on each. A review is recorded as failed when an item has no verdict, is ruled unmet, or is ruled met without evidence, or when the reviewer reports a high or critical finding, whatever it put in `passed`. The failure names the items, so the specialist's revision knows what is missing.
- Reviewers are told to look for secrets on the command line or in logs, in-place saves of user data, injection, path traversal, loose file permissions and silent data loss.
- Not covered yet: a success criterion shared between several tasks is not checked as a whole when the run finishes. A three-build live smoke completed 3 of 3 for $1.00, but builds cost more than before ($0.14 to $0.67 against a $0.09 median) and one took 33 minutes; see `docs/live-campaign/README.md`.

## 2026-09-30 — Live campaign survives more than one build

- A three-build live smoke stopped at its second build with "Event loop is closed". The campaign started a new event loop for every job while the provider client, which is cached, kept a connection from the previous loop. It now runs every job on one loop, as a worker does. Production workers were never affected.

## 2026-10-01 — Log shipper starts under its locked-down settings

- The `alloy` service failed on its first production start with `mkdir /var/lib/alloy/data: permission denied`: that directory belongs to the image's own user, and the service runs as root with every capability dropped, so it could not enter it. Its state now lives in a volume of its own at `/alloy-state`. The unused `alloy-data` volume can be removed (`docker volume ls | grep alloy-data`).
- New CI job "log shipper in the production overlay" (`deploy/logs/ci-test.sh`) starts the service from `compose.prod.yaml` next to a stand-in for Loki and checks that it stays up, that a marked container's lines arrive with the right labels and credentials, and that an unmarked container is left out.

## 2026-09-30 — Pricing says the hosted beta is free

- The pricing page now leads with the hosted beta at cavman.dev: free during the beta within a monthly model allowance shown in the account, with a "Start building free" button to sign-up. It says paid plans are coming, that nothing is charged without notice first, and that beta users get a discount. Self-hosting is still listed at $0 plus provider usage.
- The hosted card promises sandboxed tests only for Python and TypeScript builds (other stacks and documents get review only), downloads only for delivered projects, and cost as the model provider reports it.

## 2026-09-30 — The landing page promises only what builds have proven

- The example prompts under the landing page's build box were a web app, a SaaS and a mobile app, which no live build has shown Cavman delivering. They are now the kinds of work the live campaign completed: a Python CLI, a Python library, an API core, a TypeScript rate limiter, a CSV parser and a tattoo studio's booking availability engine, each with tests. The placeholder follows.
- The line under the headline says Cavman hands work over only after a second reviewer signs off, and names what it is best at today: Python and TypeScript libraries, CLIs and API cores, where real tests must pass too. Other stacks get review only, so the page no longer promises tests for them.
- The "Hands it over" step no longer offers publishing, which is not wired up yet (see Docs, "Current limitations"); it says the reviewed build is yours to download.

## 2026-09-30 — A new look: the dig site

- The web app has a new light design: concrete background with a survey grid, ink outlines, duck-yellow for the one main action per screen, and pink and sky-blue stickers for small jokes. Headings use Rubik Mono One, text uses Archivo and code uses JetBrains Mono.
- The landing page is now a dig site: "We dug up a caveman who builds software.", with the prompt as the largest element and Cavman in a museum display case. He blinks, and looks up when you type.
- The pixel Cavman is the logo and shows how each run is going: digging while it works, hiding when it needs you, asleep when paused and cheering when it is done.
- The favicon, share image, README banner and README screenshots are redrawn to match. Navigation and button labels are unchanged.

## 2026-09-30 — Caveman is now Cavman

- The product, the Python package (`cavman`, CLI `cavman`), the web app's API routes, settings (`CAVMAN_*`), metrics (`cavman_*`), the E2B template (`cavman-sandbox`), Docker and systemd names and all docs use the new name, matching the domain cavman.dev.
- Existing servers keep working without changes: `CAVEMAN_*` settings still apply (a `CAVMAN_*` setting of the same name wins), and an install still configured that way keeps its database schema (`caveman_*`), E2B template and SQLite file names. `scripts/migrate-to-cavman.sh` rewrites a server's `.env` files to the new names and pins those values; see "Upgrading a server set up before the rename to Cavman" in `docs/DEPLOY_DIGITALOCEAN.md`. The web app sends the user identity under both the old and the new header name and the API accepts either, so the two can be upgraded one after the other.
- Local development: the compose volume is now `cavman-data`, so a fresh `docker compose up` starts with empty local state; `.local/caveman` and `.local/caveman-auth.db` are still used when they are the only ones present.

## 2026-09-30 — Server logs can go to Grafana Cloud

- `compose.prod.yaml` gains an optional `alloy` service (Grafana Alloy) that ships the API, worker, web and Caddy container logs to Grafana Cloud Logs or any Loki. It starts only when `.env` has `COMPOSE_PROFILES=logs` and the three `GRAFANA_LOKI_*` values; setup is in `docs/DEPLOY_DIGITALOCEAN.md`, "Logs".
- The shipper reads Docker's log files through a read-only mount and is not given the Docker socket. Production containers are labelled `dev.cavman.logs=ship` and their log lines record it; only those lines are shipped.
- The privacy policy lists Grafana Labs as a processor and says its copy of the logs is deleted after at most 30 days.

## 2026-09-30 — Error reporting to Sentry

- With `SENTRY_DSN` set, the API and workers (`pip install '.[sentry]'`, now in the API image) and the web server report errors to Sentry: unhandled exceptions, ERROR log records (so logged job failures), and failed Next.js requests. Unset, nothing changes.
- Reports carry the error, stack and route only. The Python side sends no PII, request bodies or local variables; the web side strips bodies, cookies, query strings (including the copy in the Next.js context) and all but a few harmless headers, and drops the user. There is no browser SDK, so the CSP is unchanged.
- The privacy policy lists Sentry as a processor.

## 2026-09-30 — Nightly off-server backups and a restore drill

- `deploy/backup/backup.sh` stops the API and workers while it captures both stores, so the database and files match (they restart before the upload, and on any failure), dumps PostgreSQL (the auth database too when it is separate) and archives the data volume, encrypts both with age to a key the server never holds, uploads the set to a private DigitalOcean Spaces bucket under a UTC timestamp, and prunes sets older than `BACKUP_RETENTION_DAYS` (1 to 30, default 14). `cavman-backup.timer` runs it nightly; `BACKUP_PING_URL` can report each success to a cron monitor.
- `deploy/backup/restore-drill.sh` restores a set into a throwaway Postgres container and a scratch directory, verifies checksums, row counts and `git fsck` on project repositories, and runs `cavman ops list` against the copy. Nothing in production is touched.
- CI job "backup and restore drill" runs both against Postgres and an S3 stand-in on every push, including pruning and a check that no plaintext reaches the bucket.
- Setup steps: `docs/DEPLOY_DIGITALOCEAN.md`, "Backups".

## 2026-09-30 — Terms, Privacy and Acceptable Use pages

- New public pages at `/terms`, `/privacy` and `/acceptable-use`, linked from the site footer and from a line under the sign-up button. The Acceptable Use Policy lists what Cavman refuses to build. The privacy policy names every third party that sees user data (OpenRouter and model providers, E2B, DigitalOcean, Cloudflare, Resend, GitHub).
- Operator name, contact address, governing law and minimum age live in `web/lib/legal.ts`.
- `deploy/compose.prod.yaml` rotates container logs by size (5 × 10 MB per service), so server logs no longer grow without limit.

## 2026-09-29 — Invite-only sign-up and HSTS

- `CAVMAN_SIGNUP_ALLOWLIST` (web) limits who can create an account to listed emails and `@domain` entries, for password and GitHub sign-up alike. Others see "Cavman is invite-only for now". Setting it also requires email verification (so someone cannot claim an allowlisted address they do not own), which needs `RESEND_API_KEY`. Unset keeps sign-up open; existing accounts are unaffected.
- Caddy now sends `Strict-Transport-Security` (one year), so browsers stop trying plain HTTP.

## 2026-09-29 — DigitalOcean deployment guide

- `docs/DEPLOY_DIGITALOCEAN.md` walks through the first deploy: SSH key, an 8 GB / 4 vCPU Droplet, a block storage volume, managed PostgreSQL restricted to the Droplet, a cloud firewall, DNS, and which secret goes in which server file.
- `deploy/compose.prod.yaml` layers production settings over `compose.yaml`: Caddy with automatic HTTPS as the only public listener, restart policies, workers on the E2B backend without relaxed container security options, the data volume on block storage, and Postgres connections verified against the provider's CA (`sslmode=verify-full`).
- `web/.dockerignore` keeps `web/.env.local` and other local files out of the web image; secrets reach the app only through the runtime `env_file`.
## 2026-09-29 — E2B microVM sandbox

- New isolation backend: with `CAVMAN_SANDBOX_BACKEND=e2b`, every candidate check and npm install runs in a fresh E2B microVM that is killed afterwards, so generated code never runs on the worker host. Bubblewrap stays the default.
- The VM gets the same contract as Bubblewrap: a read-only candidate snapshot owned by root, a dedicated `sandbox` account entered with `setpriv --no-new-privs` (E2B's default `user` has passwordless sudo and is never used), a cleared environment, `prlimit` limits and a wall-time budget. Offline checks load Bubblewrap's network-deny seccomp filter inside the VM, because the first live run showed E2B's `allow_internet_access=False` alone did not stop outbound connections. Writable outputs come back through `tarfile`'s `data` filter. Any E2B failure is `SandboxUnavailable`; there is no host fallback.
- `scripts/e2b_template.py` builds the `cavman-sandbox` template (Ubuntu 24.04, pytest at `/opt/walter-env`, Node 22 at `/opt/node`). Install the SDK with `pip install '.[e2b]'`; the API image now includes it.
- A worker starting with E2B runs the isolation probe in a real VM and refuses to start if it fails. The health check reports the configured backend without starting a VM.
- `src/walter/sandbox_e2b.py` joins `SAFETY_PATHS`.
- The "E2B sandbox" workflow builds the template and runs the live tests (`-m e2b`) when the `E2B_API_KEY` secret is set.

## 2026-09-28 — GitHub account rename

- The owner's GitHub account is now `michael-muh-freakin-mercer`; `CODEOWNERS`, the package URLs, issue forms, docs and the web app's GitHub links point at `michael-muh-freakin-mercer/cavman`.

## 2026-09-28 — Sign-up CAPTCHA

- With `TURNSTILE_SITE_KEY` and `TURNSTILE_SECRET_KEY` set, Cloudflare Turnstile guards sign-up and password-reset requests (Better Auth's captcha plugin; tokens are checked server-side against the site's own hostname, single-use, and refreshed after every attempt). The submit button waits for the check. A rejected check on a reset request keeps the form open with an error instead of claiming an email was sent, and if the Turnstile script cannot load the form says so and offers a retry. Sign-in is unchanged; it is rate limited. Without the keys nothing changes; one key without the other is an error.
- The CSP adds `frame-src` and `connect-src` for `https://challenges.cloudflare.com` only when the CAPTCHA is on, and otherwise now states `frame-src 'none'`.

## 2026-09-28 — SQLite to PostgreSQL migration

- `cavman ops migrate-to-postgres` copies the operational and platform stores from SQLite into `CAVMAN_DATABASE_URL`, and with `--auth-sqlite`/`--auth-url` the web app's Better Auth tables (booleans and timestamps converted). It refuses while a job holds a live lease, when only one of the two core SQLite stores exists, when the auth file lacks any Better Auth table, when any target table has rows, or when SQLite has a column PostgreSQL lacks. Every store is rehearsed in a rolled-back transaction before any is committed, and row counts are checked. `--dry-run` stops after the rehearsal. Steps are in `docs/RUNBOOK.md`.

## 2026-09-28 — Opt-in live smoke in CI

- A new workflow, **Live smoke (real models, spends credits)**, runs only when started by hand from the Actions tab. It runs 1–10 of the live campaign's standard builds with real models, capped per build and in total (defaults $1 and $2; hard limits $5 and $20 whatever is typed); every model call goes through the campaign, so the cap covers the whole workflow. It fails unless every build completes and the provider reported the cost of every call, and the report lands in the job summary and as an artifact. It needs the `OPENROUTER_API_KEY` repository secret.
- `scripts/live_campaign.py --min-completion RATE` exits 1 when fewer requests complete (compared unrounded) or, with the provider executor, when some calls reported no cost, so automation can't pass on a bad report.

## 2026-09-28 — Dashboards and alerts

- `GET /api/metrics` adds `cavman_spend_month_usd`, `cavman_model_calls_month` and `cavman_model_calls_without_cost_month`: provider-reported spend and calls this calendar month across all accounts, counted by when each call was made (runs whose jobs all finished before the month are skipped). `cavman_deliveries{status="failed"}` is now always exported, at 0 until the first failure.
- `deploy/monitoring/`: Prometheus alert rules (scrape down, queue backlog and stall, expired leases, job failures, delivery failures, monthly spend, uncosted calls; none on `cavman_sandbox_available`, which is probed on the API host, not the workers) with `promtool` unit tests run in CI, an example scrape config, and a Grafana dashboard.

## 2026-09-28 — Cost estimate before a build

- The new-build form shows what recent builds on this server actually cost in the chosen model mode (median and the 10th–90th percentile range of completed runs whose every call reported a cost, last 30 days), the build's ceiling, and the account's dollars and model calls left this month ("at most", with the count of calls that reported no cost, when some did), with a warning when either allowance is below the build's limits and a clear notice when it is used up. With fewer than 5 such builds it says there is no history instead of guessing.
- `GET /api/estimate` serves these figures (aggregates only, cached for five minutes); it reads only runs created in the window.

## 2026-09-28 — Compose deployment verified on PostgreSQL

- `deploy/e2e.sh` builds the API and web images, starts `compose.yaml` with `deploy/compose.e2e.yaml` (Postgres for Better Auth and operational state, two workers sharing the job queue, scripted executor) and runs the 15 web journeys against it. A new CI job, `deploy`, runs it on every push.
- The Playwright config takes `AUTH_DATABASE_URL` from the environment, so the journeys can run against Better Auth on Postgres locally, and `CAVMAN_E2E_BASE_URL` to drive an already running stack.

## 2026-09-28 — Repository restructure and project presentation

- The specifications moved from the repository root into `doctrine/` (with the former `prompts/`, `protocols/` and `templates/` under it); `runbooks/` moved to `docs/runbooks/` and `ROADMAP.md` to `docs/ROADMAP.md`. Content is unchanged and history follows the moves.
- The Manager's instructions now load from `doctrine/SYSTEM_PROMPT.md`, and the API image copies `doctrine/`.
- `SAFETY_PATHS` protects the same doctrine files at their new `doctrine/` paths; the set is otherwise unchanged. Protection is an exact path match, so a new test fails if any protected path names a file that does not exist, which would otherwise leave a moved file silently unprotected.
- The README was rebuilt around real product screenshots, the first live-campaign results and a repository map; `SECURITY.md`, `CODE_OF_CONDUCT.md`, issue forms, a pull request template, `CODEOWNERS`, Dependabot and `.editorconfig` were added. Links point at the repository's new name, `michael-muh-freakin-mercer/cavman`.
- The tagline "So easy a cavman could do it" was retired as too close to an existing slogan; it is now "You describe. Cavman delivers." The web app gained a favicon, Apple touch icon and link-preview image drawn from the flint mark.

## 2026-09-28 — Step budget and salvage (first live completion)

- With specialists able to run their checks, the fourth live smoke build completed: the specialist reached all-green checks at its 24th and final step, ran out before reporting, and its revision was accepted after trusted validation and independent review ($0.27, 33 model calls, 11 minutes).
- Cavman gives specialists 40 steps per attempt (`CAVMAN_SPECIALIST_MAX_TURNS`).
- `DurableController(salvage_exhausted=True)` (Cavman only): when a developer specialist runs out of steps after changing its workspace, the platform submits the workspace as a candidate instead of discarding the attempt. The submission says in its deliverable that the platform submitted it and the specialist did not self-report; it must still pass every trusted check and independent review. Without changes, or with salvage off (operator CLI), exhaustion fails as before.

## 2026-09-28 — Planner robustness (from live probes)

- Probing the planner on the smoke prompt: 2 of 3 plans were rejected because `required_inputs` held free text ("Specification from design-parser-spec", "DATA_MODEL.md for …") the kernel cannot resolve. The driver now maps entries naming an earlier task to that task id (and a dependency) and moves anything else into the task's context; the planner is told what the field accepts. After the change 3 of 3 probes planned successfully.
- Several plans put `pytest_regression` on the first code task of a new project, a check with nothing to run that the kernel records as a failure. The driver drops it where there are no pre-existing tests and no upstream Python code task; the planner is told when to use it and to keep code and its tests in one task.
- Planning gets three attempts (was two), retries unparseable planner output too, and a final failure names the last validation problem.

## 2026-09-28 — Specialists can run their own checks

- Found by the first live-model smoke build: specialists' `run_check(category, argv)` calls were all refused by the sandbox's strict templates ("Command category denied", "Only the immutable Python environment … are executable"), which the model was never told. Unable to test, the specialist rewrote 8–11 KB files blind until it ran out of turns, twice, and a syntax error went unnoticed.
- `run_check` now takes a check name (`pytest`, `compile`, `node_test`, `tsc`) and optional paths and builds the exact template itself; the sandbox still validates every command. Output is trimmed to its last 3,000 characters so test logs do not bloat every later turn. Refusals come back as a readable result instead of a tool error.
- Developer-sandbox specialists are told which trusted checks will verify their work, how many steps they have, and a short working loop: write implementation and focused tests, run the checks, fix failures, return as soon as they pass.

## 2026-09-28 — Specialist tool trace

- `WALTER_TOOL_TRACE=<file>` (operator-only, opt-in) appends one JSON line per specialist model call and tool call: tool name, argument and result sizes, and the first 200 characters of check results and errors. File contents are never recorded. Used to tune turn budgets in the live campaign.

## 2026-09-28 — Security settings

- Settings has a Security panel: signed-in devices (browser and system from the user agent), signing out one device or all others, and changing the password (which signs out other devices). Revocation goes through `POST /api/account/sessions`, which addresses sessions by id and keeps their tokens on the server, so page scripts never see another device's session token.

## 2026-09-28 — Stack expectations

- The New build form states that Python and Node/TypeScript code is tested in the sandbox and other stacks are delivered as reviewed source, and warns (without blocking) when the request or preferred stack names something Cavman cannot run: iOS/Swift, Android/Kotlin, Flutter, React Native, native mobile apps, Go, Rust, Java, .NET, PHP, Ruby, C++, game engines.

## 2026-09-28 — Secret scanning

- CI runs `scripts/check_secrets.py`: detect-secrets rescans the tracked tree against `.secrets.baseline` and fails on any finding not audited as a false positive. The baseline's current entries are placeholders and test fixtures (`your_openrouter_key_here`, CI-only Postgres and auth values, fake tokens in tests).

## 2026-09-28 — Campaign spend guard

- `scripts/live_campaign.py` starts a run only if its whole per-run ceiling still fits under `--total-budget-usd`, and stops the campaign after any run whose provider did not report cost for every call, since spend could then not be capped (`--allow-unknown-cost` overrides).

## 2026-09-28 — One job per project; scheduled maintenance

- Fix: a project's sandbox state (candidate grants in `.local/sandboxes/grants.json`) is loaded once per `WorkspaceManager` and rewritten whole on save, and every job builds its own manager. Two runs of one project executing at once (allowed since users may have two builds running) could drop each other's grants or corrupt the file. The queue now never claims a job whose project already has a running job, across all workers and hosts.
- Project leases (`project_leases`) let work outside a job hold a project: the job queue skips a leased project, and a lease is refused while a job of the project runs. The manual delivery retry endpoint takes one.
- Maintenance: once per `CAVMAN_MAINTENANCE_INTERVAL_SECONDS` (default 3600, one worker across the fleet) the worker retires candidate worktrees, branches and grants of finished runs (completed with a ready delivery, or closed otherwise) through the sandbox's `retire_run`, prunes cached npm installs unused for `CAVMAN_NODE_DEPS_MAX_AGE_DAYS` (default 7), and prunes rate-limit records older than a day. Each project is leased while it is cleaned. Deliveries and the integration branch are untouched; a follow-up build after cleanup is tested.

## 2026-09-28 — Abuse and tenancy limits

- Per-user rate limits kept in the platform database (so they hold across API hosts): new builds and continuations per hour, repository imports per hour, and other mutating actions per minute; over a limit the API answers 429 with `Retry-After`. Per-account caps on concurrent builds, projects and disk (project repositories plus archives). All configurable (`CAVMAN_BUILDS_PER_HOUR`, `CAVMAN_IMPORTS_PER_HOUR`, `CAVMAN_ACTIONS_PER_MINUTE`, `CAVMAN_MAX_CONCURRENT_BUILDS`, `CAVMAN_MAX_PROJECTS`, `CAVMAN_ACCOUNT_DISK_MB`). Rate records are erased with the account.
- `CAVMAN_GITHUB_IMPORT_TOKEN`: optional operator token for import metadata and clones (raises GitHub's anonymous rate limit); it reaches git only through environment config for the download and is never written to the project repository.
- Fix: `PRAGMA journal_mode=WAL` fails immediately with "database is locked" while another process holds a new database file (SQLite applies no busy timeout to it), another way a concurrent first start could crash. `walter.store.enable_wal` skips the change when WAL is already on and retries briefly otherwise.

## 2026-09-28 — Concurrent first start

- Fix: `SQLiteStore` read `user_version == 0` and then created its tables without holding the write lock, so when the API and a worker opened a fresh database at the same moment one of them crashed with `table runs already exists` (a worker crash left every E2E build unexecuted in CI). Schema creation, the v1→v2 migration and the platform store's additive column migration now take the write lock and re-check before acting. A regression test opens both stores from eight processes released by a barrier; it reproduces the crash on the old code.

## 2026-09-28 — PostgreSQL for operational state

- `CAVMAN_DATABASE_URL` puts the kernel's run store and the platform store in PostgreSQL (schemas `<CAVMAN_DATABASE_SCHEMA>_ops` / `_platform`). `walter.pg` presents a psycopg connection with the SQLite calls the stores make: `BEGIN IMMEDIATE` becomes a transaction holding a per-schema advisory lock (single-writer semantics preserved, so the optimistic version check and the job queue behave identically across hosts), `?` placeholders and `rowid` are translated, and rows read by index or name. `PostgresStore` subclasses `SQLiteStore`; `walter.store.open_store` and `Settings.open_operations_store/open_platform_store` select the backend.
- Fix (both backends): loading a run read its row and its events in separate statements, so a concurrent commit from another process could make a reader see a snapshot that did not match its events. Reads now happen inside one read transaction (repeatable read on PostgreSQL). `Engine.version` uses the store instead of opening the SQLite file.
- The platform store's upserts use portable `ON CONFLICT ... DO UPDATE`.
- CI runs the API suite and new store/queue concurrency tests against a PostgreSQL 16 service; locally the tests start a throwaway cluster when PostgreSQL is installed.

## 2026-09-28 — Follow-up requests

- "Ask for changes" on a completed build opens a new run in the same project; the planner sees the project's files and specialists start from its integration head. A scripted `#follow-up` scenario (its tests import the earlier run's code) and an API test plus an E2E journey prove the second delivery contains both runs' work.

## 2026-09-28 — Forms never submit natively

- Fix: a click on a form's submit button before the page hydrated fell through to a native GET submission, which reloads the page and would put field values (including the sign-in password) in the URL. It surfaced as an intermittent CI failure of the password-reset journey. Sign-in, sign-up, password reset and build forms now use `method="post"` and keep their submit buttons disabled until hydration (`useHydrated`); an E2E journey with JavaScript disabled pins this.

## 2026-09-28 — Data export and account deletion

- Decision: honouring a user's deletion request is the one case where durable run history is removed. `SQLiteStore.delete_run` erases a run's snapshot, events and migration backups; ordinary operation still never removes history.
- `GET /api/account/export` returns the account's projects and runs (full event history and artifact contents); the web route `/api/account/export` adds the account record and sign-in methods (never tokens) and serves it as a download.
- `DELETE /api/account` removes platform records in one transaction (refused with 409 while a job is running; queued jobs go with their runs), then kernel runs, agent sessions, project repositories and delivery archives. It is called only from Better Auth's `beforeDelete` hook, after the password (or session freshness) check, so the sign-in is deleted only after the data; the browser proxy does not expose it. `cavman ops purge-orphans` removes data left by an interrupted erasure (items younger than an hour are kept).
- Settings has a "Your data" panel with the download and a typed-confirmation delete dialog.

## 2026-09-28 — Existing code

- New projects can start from a public GitHub repository (`repository_url` on `POST /api/builds` and `POST /api/projects`; "Start from a public GitHub repository" in the New build form). `src/cavman/importer.py` accepts only `https://github.com/<owner>/<repo>`, checks public visibility and size through GitHub's API before downloading, clones shallow over HTTPS only (`GIT_ALLOW_PROTOCOL=https`) with no hooks, templates, tags, submodules or credentials, inspects the tree before checkout, refuses symlinks, submodules and other special entries, drops paths the sandbox treats as state or secrets, and commits the kept files as the project's single first commit, discarding upstream history, refs and remote. Cavman's `.local/` is excluded through `.git/info/exclude`, leaving the project's own `.gitignore` untouched. The source (URL, upstream commit, branch, counts) is recorded in the project settings and shown on the project page.
- The workflow planner now receives a listing of the project's current files (integration head, else `HEAD`; policy-visible paths only, capped at 200), so follow-up runs and imported projects are planned against existing code. `WorkspaceManager.tracked_files()` provides it.

## 2026-09-28 — Publish to GitHub

- Decision: publishing is a promotion performed only on the user's explicit request for one exact target (new repository name, visibility, verified integration commit). The user's click on the confirmation dialog is the human authorization; it is recorded in the platform `publications` table (the kernel refuses mutations on completed runs). Repository scope is requested incrementally at that moment; OAuth tokens are encrypted at rest (Better Auth `encryptOAuthTokens`), read server-side by a dedicated route, used once by the API, passed to git through environment config (never argv or repo config), and redacted from errors. Only newly created repositories are pushed to, so nothing is overwritten.

## 2026-09-28 — Node/TypeScript toolchain and execution backend seam

- New trusted checks `node_test` and `tsc` (see TOOLS.md). Node runs under the existing network-deny seccomp filter with in-process test isolation, because per-file test processes need `socketpair`, which the filter denies. Node gets a 4 GB address-space cap (V8 reserves far more than it uses) with a 768 MB heap limit, and remains bound by the 1 GB aggregate-RSS monitor; Python keeps 1 GB. Node 22+ is required.
- npm dependencies install in a separate jail: network allowed, `--ignore-scripts`, cleared environment, manifest-only view, cached by manifest digest, mounted read-only.
- Execution is now behind `ExecutionBackend` / `ExecutionSpec`; `BubblewrapBackend` is the implementation. A microVM or managed sandbox can be added without touching workspaces, checks or the kernel.
- The worker image ships Node 22.

## 2026-09-28 — Workflow driver

- Decision: Cavman runs default to a deterministic workflow driver (`src/cavman/workflow.py`) instead of the Manager model's tool loop. Code drives plan → delegate → validate → review → accept/integrate → recover; a planner model produces criteria and a task graph that is validated (uniqueness, dependency order, criterion coverage, kernel task rules) before anything is recorded, with one corrected retry. Failures are classified by trusted code and routed by the kernel's recovery table; replan-blocked work is reopened only when the kernel judges the reopen non-material; capability requests become exact human approvals. The default scripted build dropped from 19 model calls to 8. `CAVMAN_ORCHESTRATION=manager` keeps the original mode.
- Adapter: worker errors are classified (budget interruption → TIMEOUT, malformed structured output or exhausted turns → BAD_OUTPUT, provider transport errors → PROVIDER_FAILURE); anything unrecognised stays TOOL_FAILURE. `_invoke` can call the manager model (used by the planner).

## 2026-09-28 — Acceptance integration

- Decision: for Cavman project repositories, accepting a developer candidate fast-forwards an internal `walter-integration` staging ref to exactly its validated bytes (fingerprint re-verified). New candidates start from that ref, so dependent tasks build on accepted upstream code. Integration is fast-forward only; a candidate on a stale base fails as the new `STALE_BASE` class (routed to RETRY) and the retry replays the previous attempt onto the new head. finish_run requires all accepted code to be integrated. Merging into user branches, pushing and deploying remain human-approved promotion. Off for the operator CLI.
- Kernel: `Artifact.integrated_commit`, `Orchestrator.record_integration`, `FailureClass.STALE_BASE`.
- Sandbox: `integration_head`, `integrate` (compare-and-swap ref update), `carry_over`, `create_candidate(base_revision=...)`.
- Delivery archives the integration head after verifying it against the kernel's recorded commits.

## 2026-09-27 — Cavman product layer

- Product identity is now Cavman; `walter` remains the internal core package and a compatibility CLI.
- Added `src/cavman/`: private FastAPI control plane, owner-scoped platform store, leased durable job queue, worker with interruption recovery, exact-scope approval decisions (bound to the displayed scope digest), SSE streaming, honest projections, verified delivery archives, and a scripted test executor refused in production.
- Core (additive): provider-reported USD spend ceiling in `UsageBudget` (`WALTER_MAX_COST_USD`), a provider registry seam in `runtime.build_models`, and `build_walter` honoring a per-run controller configuration.
- Added `web/`: Next.js marketing site, Better Auth sign-in, authenticated same-origin API proxy, and the live run dashboard; Vitest and Playwright journeys; CI jobs for both.
- Workers refuse to start when Bubblewrap isolation is unusable.

## Unreleased — 2026-09-27

### Fixed
- **Sandboxed commands now work on Debian/Ubuntu hosts.** The sandbox root
  hardcoded `--symlink usr/lib /lib64`, which is Arch's layout; on Debian and
  Ubuntu the dynamic loader lives in `/usr/lib64` (itself a symlink into
  `/usr/lib/<multiarch>`), so every sandboxed command failed with
  `bwrap: execvp /usr/bin/prlimit: No such file or directory` — an error that
  names the binary rather than the unresolvable interpreter. The `/lib64` target
  is now derived from the host's own loader directory (`_loader_dir_target`), with
  the previous value as the fallback. Found by running the suite on an Ubuntu CI
  runner. Note that `src/walter/sandbox.py` is a safety-path module, so this
  change deserves the same review attention as any other control-plane edit.

### Added
- **Continuous integration** (`.github/workflows/ci.yml`): the offline suite and
  the offline eval scenarios on Python 3.11 and 3.14, with the isolation backend
  installed and verified before the tests run. The workflow is green on the
  default branch, so the README now publishes its status badge.
- **Bounded Manager drill-down tools.** `inspect_task` returns one task's packet,
  required checks, per-artifact validation and review verdicts, approval gates
  and failure evidence as bounded excerpts. `inspect_artifact` returns one
  candidate's content, truncated with the omitted length stated; the digest
  still covers the whole artifact.
- **`CONTRIBUTING.md`** covering only workflows this repository enforces: the
  editable install, the four verification commands, `pytest` as the sole
  configured gate, the `importorskip` caveat that makes a green run mean less
  than it looks, and the doctrine/safety-path rules.

### Changed
- **Replan materiality is decided by the kernel, not asserted by the model.**
  Every model-authored replan previously waited for human approval, which
  contradicted the promise that Walter keeps going on its own and in practice
  cost the operator a round-trip just to unstick a run whose attempt budget was
  exhausted — exactly what happened in run `a892546081ce`.
  `Orchestrator.replan_materiality` now derives the answer from the proposal's
  structure and current durable state: a proposal that only reopens tasks which
  never reached `ACCEPTED` discards nothing the human was shown and applies
  autonomously, while adding or removing tasks, rewiring dependencies,
  superseding accepted work, or changing nothing at all stays gated. The model's
  own trigger, risk and evidence text is never consulted, so a model cannot talk
  its way past the gate — nor accidentally gate a harmless proposal by
  describing it dramatically.

  `DurableController.apply_replan` re-derives the assessment before applying,
  because a proposal authored as autonomous can become material while it waits —
  an upstream task reaching `ACCEPTED` is enough, and that does not make the
  proposal stale. The kernel continues to honor the `requires_approval` flag for
  trusted programmatic callers, whose authority is established independently;
  that facility is documented in `OPERATING_MODEL.md` and is not delegated to
  the model.

- **A developer lane that changed nothing is refused before submission.** A
  worker claiming `completed` with an empty candidate diff produced nothing to
  inspect. Trusted validation caught it, but only after a sandbox execution and
  the further Manager turns spent discovering why. The claim is now checked
  against the trusted diff at the adapter boundary, classified `BAD_OUTPUT` with
  the worker's own summary preserved in the failure evidence, and routed to a
  bounded revision. Read-only `repo_reader` lanes are exempt: their diff is
  empty by construction. Note this saves the sandbox execution and the
  diagnosis, not the attempt — `attempts` increments in `Orchestrator.delegate`,
  before the worker runs.

- **The model-facing read path is bounded by construction.** `inspect_run`
  returned the entire run snapshot — candidate bodies, task packets, approval
  scope documents, pytest logs and workspace diffs — so the Manager's context
  grew with exactly the material Walter exists to keep out of a context window.
  It now returns a projection: plan, completion criteria verbatim, per-task
  status/blocker/attempt budget, artifact status, accepted artifacts, open
  approval and capability gates, recent event kinds, and usage totals. Operators
  keep the unbounded snapshot through `walter run inspect`, and
  `DurableController.inspect()` stays full because trusted internals
  (`_receipt`, `delegate`, `candidate_scope`, readiness checks) depend on it.

  Measured on synthetic runs: 1,025 bytes versus 6,954 for one task with a
  2k-character candidate (6.8x), 1,497 versus 55,275 at three tasks (37x), and
  2,202 versus 253,755 at six tasks with 20k-character candidates (115x). The
  bounded read grows sub-linearly in candidate size; the snapshot does not.

  This partially reverses the 2026-09-20 resolution of gap-report finding 5,
  which introduced compact receipts and designated `inspect_run` "the full-truth
  read". Those receipts fixed the mutation path and left the read path
  unbounded, which is where the cost actually was: run
  `a892546081ce4ab1bf62c4778d832988` spent 19 Manager calls against 2 worker
  calls — 90% of the spend on orchestration overhead — for a single-task
  objective that accepted nothing.
- **Manager doctrine names the new read path.** `DURABLE_INSTRUCTIONS` tells the
  Manager that `inspect_run` is bounded, to drill down only when a decision
  needs it, and not to re-read after a successful mutation because every
  mutating tool already returns current status. `TOOLS.md` records the boundary.
- **Live evidence is no longer presented as current capability.** Every
  live-model datapoint in this repository was recorded on 2026-09-20 and
  predates both the 2026-09-22 worker tool-calling fix and the 2026-09-21
  `pytest` scope split; one stale run declared a check named `unittest`, which no
  longer exists. `ROADMAP.md` and `docs/IMPLEMENTATION_STATE.md` mark those
  numbers as history, and a fresh measured baseline is tracked as outstanding
  work.

### Operational
- The three remaining `active` runs from 2026-09-20 were closed offline. Each
  required its pending gate to be denied first — a `promote_candidate` gate on a
  readiness fixture, and a stale `replan` proposal written against a check name
  the current kernel would reject. Five candidate worktrees and branches were
  retired with `walter run cleanup`. The ledger holds 12 abandoned runs and 1
  completed run, with no orphan worktrees.

### Verified
- **First measured live run on the current worker path** (run
  `8ab604c242e946b7b06ee76d1ebf4388`, external throwaway repository): completed
  on the first attempt in 27 model calls and 156,219 tokens — Manager 12,
  worker 12, reviewer 3. Both predeclared checks and an independent review
  passed, the live checkout was never modified, and the Manager stopped at the
  promotion boundary on its own. Full record in
  `docs/baseline-2026-09-27.md`.

  The worker made 12 calls, which is direct evidence that it used its granted
  tools — a worker cannot write a file, inspect its diff and run a check in one
  call. This settles a planned change: two-phase worker finalization was
  conditional on this measurement showing single-call workers or turn-budget
  losses, and it showed neither, so that work is dropped rather than built.

  Orchestration overhead fell from 90% of calls to 44%, but this is not a
  controlled comparison and is not claimed as one: the Manager model, the
  objective, and the outcome all differ from run `a892546081ce`.

## Unreleased — 2026-09-22 rehearsal follow-ups

### Added
- **Offline run abandonment:** `walter run abandon <run_id> --reason "<why>"` (kernel `Orchestrator.abandon`) closes an active run that holds no in-flight task, no pending approval, and no pending capability request, without any provider call. Every other terminal route needs the Manager model or demands accepted artifacts, so such runs previously stayed `active` forever. The transition records `run.abandoned` with the reason and local operator principal, preserves all durable history, and is refused with the exact blocking task or gate named.

### Changed
- **Replan proposals are validated before they bind an approval gate (follow-up 1):** `Orchestrator.validate_replan` runs the same structural rules application enforces — unknown reopen/remove/dependency references, non-fresh or id-colliding additions, capability/executable-check violations, a dependency map that breaks the resulting plan graph, and an exhausted replan budget — and reports every defect in one message. `DurableController.propose_replan` validates before `request_approval`, so a kernel-invalid proposal no longer consumes a human review round-trip, and `apply_replan` re-validates against current state because upstream facts can move while the gate is open.
- **`finish_run` rejection names the accepted evidence shape (follow-up 3):** the completion gate now states that `criterion_evidence` maps each completion criterion verbatim to nonempty arrays of accepted artifact IDs, and echoes the required keys, the available accepted artifact IDs, and an example instead of failing once per mis-formatted attempt.
- **Read-only lanes are reviewable (follow-up 2):** `repo_reader` candidates cannot change files, so their diff is empty by construction and the "reviewer inspected no candidate file" rule failed every scout lane. That rule now applies only to lanes that can change files; a read-only lane is reviewed on its reported content, verified against the repository with the same read-only tools, and the reviewer's instructions say so.

## Unreleased — 2026-09-21 decisions

### Changed
- **Strict validation evidence (Decision 2):** a recorded check failure on the current candidate bytes permanently blocks that candidate; re-running to green no longer supersedes it. Correction requires a revised candidate.
- **Split pytest scopes (Decision 1):** new `pytest_candidate` (candidate's changed tests; `pytest` remains as its legacy alias) and `pytest_regression` (the pre-existing suite the candidate did not touch), so breaking existing tests can no longer hide behind new green ones. Regression records an honest failure when there is nothing to run or the suite exceeds the sandbox budget.
- **Dirty-workspace retries (Decision 3):** grants now carry a `used` marker set when an attempt begins; the escalation workspace-reuse branch fires only for never-executed candidates, so a retry after a failed attempt gets a fresh isolated workspace as `replace_workspace` contracts.
- **Interactive mode continuity (Decision 4):** follow-up messages continue the open run instead of spawning an orphan run per input line; `:new` explicitly starts a fresh run.
- **Sandbox inventory limits raised (Decision 5):** 2 MB / 10k files → 50 MB / 100k files (256 MB snapshot cap); `list_files`, `status`, and `changed_paths` no longer read every file's bytes. Symlinks remain forbidden pending a dedicated security design.
- **Operator-collision UX (Decision 7):** a cross-process `ConcurrentUpdate` reaching a Manager tool is retried once for atomic mutations and otherwise translated into a plain-language `concurrent_update` result instead of an opaque tool error.

### Fixed
- **Nested agents never called their tools (found by the 2026-09-22 live rehearsal):** with the SDK's `output_type` structured output, the OpenRouter chat-completions path suppresses tool calling entirely, so every worker/reviewer answered directly without invoking its granted tools. `_invoke` now requests schema-shaped JSON in prose and validates it client-side; verified live that workers then use their tools.

### Reserved
- **Safety-boundary grant path (Decision 6):** `create_safety_candidate` is formally a reserved facility — no Manager tool, CLI command, or construction site injects an approval verifier, so the shipped runtime can only deny. Wiring it requires a fresh security design review (see TOOLS.md).

## Unreleased

### Added

- Durable orchestration models, atomic SQLite operational state/events, artifact lineage, scoped approvals, bounded recovery/replanning, and guarded run inspection/resume/approval commands.
- Enforceable capability profiles, isolated candidate worktrees, fail-closed Bubblewrap checks, trusted validation, independent review, and an offline self-build-readiness fixture that stops at human promotion approval.
- Schema-v2 approval lifecycle/gates with transactional v1 backups and explicit legacy-gate recovery; assignment-bound submissions; audited revision workspaces; trusted current-candidate approval scope; and one-use, exactly scoped safety-boundary grants.
- Assignment-bound typed capability requests with partial-result preservation, exact Manager-created repository workspaces, atomic/idempotent restart-safe application, cleanup on rejection, profile-correct redelegation, and mandatory executable checks for developer plans/replans/changes.
- Stale workspace-grant reconciliation that closes grants whose signed manifest no longer matches the host environment.
- Forward-compatible durable snapshot loading that prunes unknown fields and emits clear diagnostics instead of failing.
- Env-configurable per-run usage budget (`WALTER_MAX_MODEL_CALLS`, `WALTER_MAX_INPUT_TOKENS`, `WALTER_MAX_OUTPUT_TOKENS`, `WALTER_MAX_TOTAL_TOKENS`) with clean exhaustion reporting.
- Live end-to-end verification on 2026-09-20: a model-driven run planned, delegated, validated, independently reviewed, accepted, and completed within its token budget.

### Changed

- Scoped developer validation to the candidate's changed/added test files: `pytest` now runs only those files inside the isolated sandbox and requires the candidate to add or modify at least one test file; removed the non-functional `unittest` check.
- Adopted the Master Blueprint's canonical task lifecycle and acceptance-only dependency semantics throughout executable doctrine and templates.
- Separated Agents SDK conversation sessions from authoritative operational state and documented the current OpenRouter hosted-web limitation.
- Guarded new CLI runs with a completion-criteria sentinel, provider preflight, deterministic resource closure, and OS-derived local operator audit identity.
- Kept the legacy trace-sensitive option as an honest no-op: provider trace export and sensitive payloads remain disabled, and displayed workflow identifiers are explicitly local.
- Made the durable store safe across Agents SDK tool-dispatch threads.
- Fixed CLI session cleanup and Manager output surfacing.

### Safety boundary

- Self-build readiness does not authorize real autonomous self-development, merge, push, deploy, or candidate promotion.

## 1.0.1 — 2026-09-14

### Changed

- Renamed the Manager identity from `Agent Manager` to **Walter**.
- Updated Codex entry instructions, charter, system prompt, and README to use Walter as the agent/product name while retaining `Manager` as the functional role.
- Standardized the recommended local installation path as `~/Projects/Walter`.

## 1.0.0 — 2026-09-14

Initial v1 specification.

### Includes

- general-purpose Manager charter;
- high-autonomy exception-based escalation;
- centralized worker creation;
- one-agent-one-lane delegation;
- dependency-aware execution graph;
- minimum-sufficient context and least-privilege tools;
- risk-based independent QA;
- failure classification and recovery;
- canonical state/memory promotion rules;
- worker, reviewer, scoping, and adjudication prompts;
- task/result/state/decision/failure templates;
- initial orchestration evaluation suite.
