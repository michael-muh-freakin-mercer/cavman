# Cavman operations runbook

For operators of a hosted Cavman. Commands run on an API or worker host with
the same environment as the services (`CAVMAN_DATA_DIR`, `CAVMAN_DATABASE_URL`,
`CAVMAN_API_TOKEN`, …). None of them spend model credits.

## Signals to watch

`GET /api/metrics` (bearer `CAVMAN_METRICS_TOKEN`), Prometheus text:

| Metric | Healthy | Page when |
| --- | --- | --- |
| `cavman_queue_oldest_seconds` | under a minute | over 10 minutes: workers are down, saturated, or every queued job's project is busy |
| `cavman_expired_leases` | 0 | above 0 for more than 2 × `CAVMAN_LEASE_SECONDS`: a worker died and nothing is reaping (no worker running) |
| `cavman_jobs{status="failed",outcome=...}` | slow growth | a sudden rise in one outcome (for example `interrupted`, `error`) |
| `cavman_sandbox_available` | 1 | 0: isolation is not usable on the API host (not alerted: workers probe their own sandbox at startup and exit if it fails, which shows as a queue backlog) |
| `cavman_deliveries{status="failed"}` | 0 or flat | any growth |
| `cavman_spend_month_usd` | within plan | above what you meant to spend this month (all accounts, provider-reported) |
| `cavman_model_calls_without_cost_month` | flat | any growth: the provider stopped reporting cost, so only call caps bound those calls |

`deploy/monitoring/` turns this table into alert rules (`alerts.yml`, tested
by `alerts_test.yml` in CI), a Prometheus scrape config and a Grafana
dashboard (`grafana-dashboard.json`, import it and pick the Prometheus data
source). Thresholds are beta starting points; the spend alert is set at $100.

Logs: set `CAVMAN_LOG_FORMAT=json` for one JSON object per line. Job failures
name the job id, and recovery failures name the run id.

## Stuck runs

1. List runs that need someone:

   ```bash
   cavman ops list --attention
   ```

   States: `approval_needed`, `waiting` (the user must continue), `blocked`,
   `paused`, `budget_reached`, `failed`, `recovering`.
2. Work out whether it is stuck on a person or on the system.
   - **A person.** Approvals, budget and continuing belong to the user; do nothing.
   - **A dead worker.** A job shows `running` but its lease expired. Any live
     worker reaps it on its next poll and queues a `recover` job, up to
     `CAVMAN_MAX_RECOVERIES`. If no worker is running, start one.
   - **A queued job that never starts.** Another job of the same project is
     running: jobs of one project run one at a time, by design. Otherwise
     every worker is busy (scale workers) or maintenance holds the project
     (it releases within 15 minutes).
3. If a run has no active job and should resume:

   ```bash
   cavman ops requeue RUN_ID
   ```

   This queues a `recover` job, which converts interrupted in-flight work to a
   recorded failure and hands the run back to the workflow.
4. If a run can never finish and has no in-flight work or pending approval,
   close it through the kernel's rules. The history is kept.

   ```bash
   cavman ops abandon RUN_ID --reason "why"
   ```

Never edit the databases by hand to "fix" a run. Every run state change goes
through the kernel so validation, review and acceptance cannot be forged.

## Worker outage

- Workers are stateless apart from leases. Restart them; running jobs whose
  worker died become `recover` jobs automatically.
- A worker refuses to start when Bubblewrap isolation is unusable (it logs why).
  Do not work around this by weakening isolation. See "Sandbox unavailable".
- Scale workers by `cavman_queue_oldest_seconds`. `CAVMAN_WORKER_CONCURRENCY`
  sets slots per process; more processes or hosts also work. The queue hands
  each job to exactly one worker across hosts.

## Sandbox unavailable

Symptoms: a worker exits at startup, `cavman_sandbox_available 0`, or
validations fail with an isolation error.

- Check the host allows unprivileged user namespaces
  (`kernel.apparmor_restrict_unprivileged_userns=0` on Ubuntu 24.04+).
- In containers, the verified options are in `deploy/README.md`.
- Cavman fails closed: while isolation is broken nothing is accepted. That is
  correct behaviour, not an outage to route around.

## API outage

- The API holds no state of its own. Restart it; with PostgreSQL, run more than
  one behind a load balancer.
- Builds keep running during an API outage; users reconnect and see current state.

## Database

- **PostgreSQL** (`CAVMAN_DATABASE_URL`). Kernel runs and events are in schema
  `<CAVMAN_DATABASE_SCHEMA>_ops`; ownership, jobs, deliveries and limits are in
  `_platform`. Back both up together, with the managed provider's point-in-time
  recovery.
- **SQLite** (single host). `cavman-operations.db` and `cavman-platform.db` in
  `CAVMAN_DATA_DIR`, in WAL mode. Back them up with `sqlite3 FILE ".backup DEST"`,
  not by copying the files while services run.
- **Files.** `CAVMAN_DATA_DIR/projects` holds the project git repositories;
  `CAVMAN_DATA_DIR/deliveries` holds the download archives. Back them up with
  the database, since run records point at them.

### Restore

1. Stop workers, then the API.
2. Restore the database (both schemas, or both SQLite files) and the data
   directory from the same point in time. On the DigitalOcean deployment the
   nightly set in Spaces holds both (`deploy/backup/`): decrypt with the age
   key, `pg_restore --no-owner --no-privileges` each `db-*.dump` into an empty
   database, and extract `data.tar` into the volume. `restore-drill.sh` shows
   the exact commands. For a point in time after the last nightly set, use the
   managed cluster's point-in-time recovery for the database, knowing files
   written after that set are not in it.
3. Start the API, then the workers. Jobs that were running at backup time have
   expired leases and are recovered automatically.
4. Check `cavman ops list --attention` and the metrics above.

Restore drills are on the launch checklist; record each drill's date and result here.
`deploy/backup/restore-drill.sh` runs one without touching production, and CI
runs it on every push against Postgres and an S3 stand-in.

| Date | Backup set | Result | By |
| --- | --- | --- | --- |
| 2026-09-30 | `20260930T044321Z` (first nightly set, 844 KB) | Passed on cavman-1; key copy and scratch data removed afterwards | Owner |

### Moving from SQLite to PostgreSQL

`cavman ops migrate-to-postgres` copies both SQLite stores into the schemas
`CAVMAN_DATABASE_URL` names, and optionally the web app's Better Auth file.

1. Back up the SQLite files and the data directory (above).
2. Stop the web app, workers and API. The copy refuses while any job holds a
   live lease.
3. Create the auth tables in the new database: start the web app once with
   the new `AUTH_DATABASE_URL` (tables are created on first use), or run
   `npx auth migrate`, then stop it again.
4. With `CAVMAN_DATABASE_URL` set, rehearse, then copy:

   ```bash
   cavman ops migrate-to-postgres --dry-run \
     --auth-sqlite web/.local/cavman-auth.db --auth-url "$AUTH_DATABASE_URL"
   cavman ops migrate-to-postgres \
     --auth-sqlite web/.local/cavman-auth.db --auth-url "$AUTH_DATABASE_URL"
   ```

   Every store is copied in a transaction that is rolled back first, so a
   refusal (a non-empty target table, a column PostgreSQL lacks, a conversion
   error) leaves nothing behind. Row counts are printed and checked.
5. Keep `CAVMAN_DATABASE_URL` and the new `AUTH_DATABASE_URL` set, start the
   API, workers and web app, and sign in to check a few runs. Sign-in sessions
   carry over while `BETTER_AUTH_SECRET` is unchanged. Keep the SQLite files until you are satisfied.

If the connection drops between the stores' commits, drop the target schemas
(`<schema>_ops`, `<schema>_platform`, and the auth tables) and run it again.
Project repositories and archives stay where they are on the data volume.
Conversation sessions from the retired manager mode stay in SQLite; nothing
writes to them any more.

## Accounts and data

- A user deletes their own account in Settings. That removes their projects,
  runs, history, sessions, repositories and archives, then their sign-in.
- If a deletion was interrupted, remove what no account owns (anything younger
  than an hour is kept):

  ```bash
  cavman ops purge-orphans
  ```

## Limits

Per-account limits are environment settings on the API (`.env.example`):
builds and imports per hour, actions per minute, concurrent builds, projects,
disk, and the monthly spend and model-call caps. Raising one takes effect when
the API restarts.

## Maintenance

One worker per `CAVMAN_MAINTENANCE_INTERVAL_SECONDS` (default hourly):
- retires the candidate worktrees of finished runs;
- prunes npm install caches unused for `CAVMAN_NODE_DEPS_MAX_AGE_DAYS`;
- prunes old rate-limit records.

It holds each project while cleaning it and skips busy projects. Look for
"Maintenance pass" in the logs.
