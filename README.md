<div align="center">

<img src="docs/assets/banner.png" alt="Cavman: we dug up a caveman who builds software. A pixel-art caveman stands in a museum display case labelled Specimen 001." width="100%">

<br>

[![CI](https://github.com/michael-muh-freakin-mercer/cavman/actions/workflows/ci.yml/badge.svg)](https://github.com/michael-muh-freakin-mercer/cavman/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-ff6a1f.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-2fd4ee.svg)](pyproject.toml)
[![Node 22+](https://img.shields.io/badge/node-22%2B-2fd4ee.svg)](web/package.json)
[![Status: pre-1.0](https://img.shields.io/badge/status-pre--1.0-8b95a4.svg)](docs/ROADMAP.md)
[![Powered by: receipts](https://img.shields.io/badge/powered%20by-receipts-ff6a1f.svg)](#why-cavman-or-trust-issues-productized)

**[Quick start](#build-it-yourself)** ·
**[How it works](#how-it-works)** ·
**[Security](#security-model-or-the-raccoon-policy)** ·
**[Docs](#documentation)** ·
**[Roadmap](docs/ROADMAP.md)** ·
**[Contributing](CONTRIBUTING.md)**

</div>

---

You describe the thing. Cavman plans it, hands the pieces to specialist agents,
makes them prove their work in a locked box with no internet, gets a second
opinion from a reviewer who has never met them, and gives you back a project.
When something breaks, it tries again. When something is scary, it asks you
first. Otherwise it leaves you alone.

Here's the catch with AI agents: they are *extremely* confident, and confidence
is not a test suite. So Cavman believes nothing an agent says. **The model
proposes; the kernel authorizes.** Nothing counts as done until trusted checks
and an independent reviewer sign off, and the kernel keeps the receipts.

It's open source, it runs on your own hardware, and it's pre-1.0, which means
it works, it's honest about what it can't do yet, and it will change under
your feet.

<p align="center">
  <img src="docs/assets/screenshot-run-complete.png" alt="A completed Cavman run: every stage done, 3 of 3 trusted checks passed, verified by the completion gate, with the delivered files and success criteria listed" width="100%">
  <br>
  <sub>A finished build. Every stage done, every check actually run, stamped by the completion gate. (Recorded with the scripted executor: fake brain, real everything else.)</sub>
</p>

## Why Cavman (or: trust issues, productized)

|  |  |
| --- | --- |
| **Receipts, not vibes** | Checks run in a Bubblewrap sandbox with no network and no access to your secrets. An agent saying "tests pass" is worth exactly nothing until the tests pass. |
| **A second opinion, always** | A fresh reviewer that never touched the work inspects every candidate before the kernel can accept it. No grading your own homework. |
| **Hard-to-kill runs** | Every task, attempt, check and decision is written down. Close the tab, restart the worker, trip over the power cord: the build picks up where it left off. (Automatic recovery is capped at 3 per run by default, `CAVMAN_MAX_RECOVERIES`, so a truly cursed run fails honestly instead of looping forever.) |
| **You're the boss of the scary stuff** | Consequential actions wait for you, bound to the exact scope you were shown. If Cavman changes the ask, it asks again. No bait-and-switch. |
| **Your wallet has a seatbelt** | Every run has a USD and model-call ceiling. Cost is tracked per call, and in the live campaign it matched OpenRouter's own counter to within half a cent. |
| **Bring your own brain** | Any tool-calling model on OpenRouter. No single vendor holding the keys to your cave. |

### We put real money where our README is

The [first live-model campaign](docs/live-campaign/README.md) threw 10 real
requests (6 Python, 3 TypeScript, 1 spec-plus-code) at
`deepseek/deepseek-v4-pro`:

| Builds completed | Total cost | Median per build | Median time |
| :---: | :---: | :---: | :---: |
| **10 / 10** | **$0.87** | **$0.09** | **5.5 min** |

Less than a coffee for the lot. Two builds got a garbage model response
halfway through and recovered on their own through a kernel-routed revision.
Every accepted change passed sandboxed checks and an independent review. (No
human has code-reviewed the output yet. We said we'd be honest.)

## How it works

```text
  You ─── "Build me a booking app for a tattoo studio"
   │
   ▼
 Understand ─▶ Plan ─▶ Build ─▶ Review ─▶ Deliver
               │        │         │          │
               │        │         │          └─ integrated head + build report, verified against kernel records
               │        │         └─ independent reviewer + trusted sandbox checks gate acceptance
               │        └─ one specialist per task, isolated workspace, built on accepted upstream work
               └─ dependency-aware tasks with declared checks and acceptance criteria
```

<p align="center">
  <img src="docs/assets/screenshot-approval.png" alt="An approval card asking the user to grant a sandboxed-development capability, showing why, the risk, what changes and the exact scope digest, with Approve and Reject buttons" width="100%">
  <br>
  <sub>Cavman asking permission like a well-raised agent: why, what's risky, what changes, and a digest of the exact thing you're approving.</sub>
</p>

### Architecture

```text
Browser
   ↓
Cavman Web            web/            Next.js · auth (Better Auth) · same-origin proxy
   ↓  service token + verified user id
Cavman API            src/cavman/    FastAPI · ownership · approvals · SSE · delivery
   ↓  reads kernel state; queues durable jobs
Orchestration Core     src/walter/     runs · tasks · gates · approvals · recovery · events
   ↓  executed by
Workers / Sandboxes    cavman worker  leased jobs · planner + specialists · Bubblewrap
   ↓
Generated Project      per-project git repo → verified delivery archive
```

- **Core (`src/walter/`)** is the single source of truth for runs, plans, tasks,
  dependencies, artifacts, validation, review, acceptance, approvals, recovery,
  replanning and completion. The deep dive is [docs/ENGINE.md](docs/ENGINE.md).
- **API (`src/cavman/`)** never keeps its own copy of that truth. It tracks who
  owns what, queues work as leased jobs, turns kernel state into honest
  user-facing views, and binds each approval to the exact scope digest you saw.
  It has *no endpoint* that can record a check, a review, an acceptance or a
  completion. Not a locked one. None.
- **Worker** claims jobs, runs them outside any HTTP request, heartbeats, and
  recovers orphaned jobs through the core's own interruption recovery. By
  default it runs the **workflow driver** (`src/cavman/workflow.py`): boring,
  deterministic code drives plan → delegate → validate → review →
  accept/integrate → recover, and models only do the parts that need a brain
  (planning, building, reviewing). No tokens burned on bookkeeping. Every step
  still goes through the kernel. (The original manager mode, where a Manager
  model drove each step through tool calls, was retired on 2026-10-02.)
- **Web (`web/`)** renders real state over server-sent events. The browser only
  watches and decides; closing it never hurts a run.

> **A note on Walter.** Cavman used to be called Walter. Walter still lives in
> `src/walter/` (and the `walter` operator CLI), quietly doing the actual
> thinking. He doesn't need the credit. Everything a user sees is Cavman.

## Build it yourself

This is a DIY project; you run the whole thing.

**What you need:** Linux, Python 3.11+, Node 22+, `git`, and the isolation
backend (`bubblewrap`, `libseccomp2`, `util-linux` for `prlimit`) with
unprivileged user namespaces allowed. Yes, Linux specifically: the sandbox is
Bubblewrap, and Bubblewrap is a Linux thing. On a Mac, use a Linux VM or
container. On Ubuntu 24.04:

```bash
sudo apt install bubblewrap libseccomp2 util-linux python3-venv
sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0
```

**1. Clone and install**

```bash
git clone https://github.com/michael-muh-freakin-mercer/cavman.git
cd cavman
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
(cd web && npm ci)
```

Use your distro's `python3`, not a toolcache build. The sandbox binds `/usr`
into an environment-cleared namespace, so the interpreter has to live under
`/usr` or it can't find itself in there.

**2. Configure**

```bash
cp .env.example .env                 # API + worker
cp web/.env.example web/.env.local   # web app
```

Put the same random `CAVMAN_API_TOKEN` (`openssl rand -hex 32`) in both files,
a `BETTER_AUTH_SECRET` in `web/.env.local`, and then either an
`OPENROUTER_API_KEY` (real models, real bills) or `CAVMAN_EXECUTOR=scripted`
(free, fake models; see [Testing](#testing-we-have-opinions)). Every variable is
explained in the example files.

**3. Light the fire** (three terminals)

```bash
.venv/bin/cavman api --port 8000    # private API; binds to localhost
.venv/bin/cavman worker             # does the builds; refuses to start without working isolation
cd web && npm run dev                # or: npm run build && npm start
```

Open http://localhost:3000, make an account, and tell it what to build. Want
more builds at once? Start more workers. Jobs are leased, so they never step on
each other.

## Testing (we have opinions)

```bash
.venv/bin/python -m pytest -q                         # backend: core + API + worker (offline)
.venv/bin/python evals/runner.py                      # orchestration scenarios (offline)
cd web
npm run typecheck && npm run lint && npm test         # frontend checks, unit + component tests
npm run build                                         # production build
npx playwright test                                   # end-to-end journeys (starts API, worker, web)
```

The E2E suite boots the real API, a real worker and the production web build.
The worker uses the **scripted test executor** (`CAVMAN_EXECUTOR=scripted`):
the *model* is a script, but every state change still goes through the kernel
and every check really runs in the sandbox. Tags in the prompt pick the
scenario: none (the happy path), `#approval`, `#fail-validation`, `#dependent`
(a task building on another task's merged code) and `#parallel` (a stale-base
rebuild with carry-over). Scripted runs are labelled in the UI, and the
scripted executor flat-out refuses to run when `CAVMAN_ENV=production`. The
browser Playwright uses must match the pinned `@playwright/test` version
(`npx playwright install chromium`).

Want to spend real money on purpose?
`WALTER_LIVE_SMOKE=1 .venv/bin/python -m pytest -q -m live` runs the opt-in
provider smoke test, and [`scripts/live_campaign.py`](scripts/live_campaign.py)
runs a capped batch of real builds (`--executor scripted` for a free dry run).

[CI](.github/workflows/ci.yml) runs all of it on every push and pull request,
plus the API suite against PostgreSQL, a secret scan against an audited
baseline, and dependency audits. Green or it didn't happen.

## What it does today

<details>
<summary><b>Accounts, requests and runs</b></summary>

- Accounts with email and password (plus optional GitHub sign-in), password
  reset by emailed link, optional required email verification, signed-in
  device management and password change.
- Plain-English build requests with optional stack, constraints, deployment
  target and budget; the prompt survives sign-up.
- Durable runs executed by workers, recoverable after worker loss, observable
  live; stop, continue (optionally with an instruction), and close.
- A run dashboard showing only real state: stage, tasks and specialists,
  attempts, dependencies, trusted check results (passed / failed / running /
  not run), independent reviews, artifacts with digests and workspace
  fingerprints, failures with classified recovery, replans, approvals, events,
  per-call model usage, provider-reported cost and budget.
- Exact-scope approvals: approve or reject a specific request; a changed request
  must be shown again.

</details>

<details>
<summary><b>Builds, integration and delivery</b></summary>

- Integrated builds: each accepted code change is fast-forwarded onto the
  project's internal integration branch with exactly its validated bytes, and
  later tasks start from that branch, so dependent work builds and is tested on
  top of accepted work. A task built on an outdated base is rebuilt on the new
  one with its previous attempt carried over.
- Existing code: a new project can start from a public GitHub repository.
  Trusted API code checks GitHub's metadata (public, within the size limit),
  makes a shallow HTTPS-only clone without hooks, submodules or credentials,
  refuses symlinks and submodules, leaves out secret-looking files, and starts
  the project from one local commit of the kept files (no upstream history or
  remote). The planner of every run is shown the project's current files, so
  follow-up runs and imports build on what is there.
- Follow-ups: "Ask for changes" on a completed build starts a new run in the
  same project from its integrated code; the new run's delivery contains the
  earlier work plus the change.
- Delivery: when the kernel completes a run, Cavman archives the integration
  head (verified against the kernel's recorded commits) plus a build report.
- Publish to GitHub (when GitHub sign-in is configured): on explicit request,
  Cavman asks for repository scope at that moment, creates a new repository
  with the exact name and visibility you confirm, and pushes only the verified
  integration commit to `main`. The token is encrypted at rest in the auth
  store, used once per request, and never stored by the API. Nothing existing is
  overwritten, and nothing is deployed.

</details>

<details>
<summary><b>Limits, budgets and your data</b></summary>

- Abuse limits per account, enforced by the API across hosts: builds and
  imports per hour, actions per minute, concurrent builds, projects and disk
  (all configurable, see `.env.example`). Over a rate limit the API answers
  429 with `Retry-After`.
- Budgets on by default: a USD ceiling on provider-reported cost plus a
  model-call ceiling per run and per account per month, with a warning at 80%
  and a safe pause at the limit. OpenRouter calls explicitly request cost
  reporting.
- Your data is yours: download everything Cavman holds for your account as
  JSON (account, sign-in methods without tokens, projects, runs, decisions,
  checks, artifacts and events), or delete the account. Deletion needs your
  password (or a recent sign-in for GitHub-only accounts), is refused while a
  build is running, and removes projects, runs, kernel history, conversation
  sessions, repositories and delivery archives before the sign-in itself.

</details>

## Security model (or: the raccoon policy)

Generated code is treated like a raccoon in your kitchen: probably harmless,
absolutely not getting the car keys.

- Generated code is untrusted. It runs only in Bubblewrap with unshared
  namespaces, no network (seccomp), a cleared environment, resource limits and a
  read-only snapshot; credential-shaped files are excluded and unwritable.
  Missing isolation is a hard failure. There is no "just run it on the host"
  fallback, and there never will be.
- npm dependencies are installed in a separate jail that has network access but
  runs with install scripts disabled, a cleared environment and only the
  manifest visible. The result is cached by manifest digest and mounted
  read-only into the network-denied jail where candidate code runs.
- The API is private and authenticates the web server with a shared token; the
  web server verifies the user session and forwards only the user id. Every
  project and run lookup is owner-scoped. State-changing browser requests must
  be same-origin JSON, and every page is served with a nonce-based CSP.
- Provider keys live only with workers; the service token only with web and
  API; the auth secret only with the web app. Host paths and credential-shaped
  strings are redacted from everything shown to users.
- Generated apps are never rendered on the Cavman origin. Live previews don't
  exist yet because we'd rather ship nothing than ship them unsafely.

The full paranoia is written up in [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md).
Found a hole? Please tell us privately first: [SECURITY.md](SECURITY.md).

## Deployment

See [deploy/README.md](deploy/README.md): web on Vercel or any Node host, API and
workers on Linux with a shared volume, managed Postgres for authentication, and
the exact container options Bubblewrap needs (verified, with their trade-offs).
If you run it for other people, read the [runbook](docs/RUNBOOK.md) too.
Nothing in this repository deploys anything on its own.

## Things it can't do yet

The honesty section. Everything here is a known gap, not a surprise.

- Sandboxed execution covers Python (`compile`, `pytest`, `pytest_regression`)
  and Node/TypeScript (`node_test` via Node's test runner, `tsc`, and
  `npm_build`, the project's own `npm run build` without its pre/post hooks).
  Dev servers are not run, and other stacks get reviewed source and documents
  without executable checks.
- Isolation is Bubblewrap on a shared kernel by default. For a public
  multi-tenant service, use the E2B backend (`CAVMAN_SANDBOX_BACKEND=e2b`),
  which runs every check in its own throwaway microVM; see
  [deploy/README.md](deploy/README.md#e2b-instead-of-bubblewrap).
- OpenRouter is the only configured model provider (any tool-calling model on it,
  e.g. Kimi, DeepSeek, Qwen). Model modes (Budget, Balanced, Maximum Quality)
  appear only when an operator configures them (`CAVMAN_MODELS_*`).
- Operational and platform state can live in PostgreSQL (`CAVMAN_DATABASE_URL`),
  which the whole API suite runs against in CI; without it they are SQLite files.
  Project repositories and delivery archives are still files, so API and
  workers share a volume either way. Manager-mode conversation sessions stay
  in SQLite on that volume (the default workflow mode does not use them).
- GitHub publishing is tested against a local stand-in for GitHub; the
  OAuth scope upgrade and token retrieval path has not been exercised against
  github.com from this environment. Repository import is tested against a local
  upstream and supports public repositories only. No live previews yet.
- Some providers don't report cost for every call. When that happens Cavman
  shows the cost as incomplete instead of making up a number.

## Where things live

| Path | What's in there |
| --- | --- |
| [`src/walter/`](src/walter/) | The brain: kernel, sandbox, durable store, Agents SDK adapter, operator CLI |
| [`src/cavman/`](src/cavman/) | The body: FastAPI API, leased-job worker, workflow driver, delivery |
| [`web/`](web/) | The face: marketing site, auth, live run dashboard (Next.js) |
| [`doctrine/`](doctrine/) | The rules the brain follows; `SYSTEM_PROMPT.md` is loaded at runtime |
| [`docs/`](docs/) | Engine guide, threat model, runbooks, roadmap, implementation state, campaign reports |
| [`tests/`](tests/), [`evals/`](evals/) | Offline backend suite and orchestration scenarios |
| [`deploy/`](deploy/) | Container images, compose file and deployment guide |
| [`scripts/`](scripts/) | Secret scan and live-campaign harness |

## Documentation

| Document | What you'll learn |
| --- | --- |
| [docs/ENGINE.md](docs/ENGINE.md) | How the orchestration core really works: lifecycle, gates, recovery, sandbox, operator CLI |
| [doctrine/](doctrine/README.md) | The rulebook: operating model, permissions, QA, failure recovery |
| [deploy/README.md](deploy/README.md) | Deployment topology, isolation requirements, durability |
| [docs/RUNBOOK.md](docs/RUNBOOK.md) | Keeping a hosted Cavman alive |
| [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md) | Assets, trust boundaries, threats and mitigations |
| [docs/ROADMAP.md](docs/ROADMAP.md), [CHANGELOG.md](CHANGELOG.md) | Where we're going, and every decision we made on the way |

## Come hang out in the cave

Bug reports with reproductions are the best gift you can give right now.
[CONTRIBUTING.md](CONTRIBUTING.md) has the setup, the checks, and the house
rules (which the code enforces, so arguing with them is mostly a waste of
everyone's afternoon). Be decent to each other: [Code of Conduct](CODE_OF_CONDUCT.md).

## License

[MIT](LICENSE). Take it, fork it, carve it into a cave wall.
