# Load test

`scripts/load_test.py` starts the real API (uvicorn over HTTP) and real worker
processes with the scripted executor. The models are scripted, but the kernel,
the Bubblewrap sandbox, the job queue and the live event stream are real. It
creates builds for many accounts at once, holds several live-update
connections open per build (reconnecting as a browser does), and polls the API
throughout. It spends no provider credit.

    .venv/bin/python scripts/load_test.py --users 50 --builds-per-user 2 --streams-per-run 5 \
        --workers 2 --worker-concurrency 4

## Results, 2026-10-02

Laptop with 8 cores, SQLite, Bubblewrap, 0.15 s per scripted model step.

| Builds (accounts × 2) | Live connections | Workers × slots | All complete | Build time p50 / p95 | API latency p50 / p95 / max | Stream errors |
|---|---|---|---|---|---|---|
| 6 (3) | 12 | 1 × 2 | yes | 10.5 s / 13.8 s | 7 ms / 24 ms / 68 ms | 0 |
| 40 (20) | 120 | 1 × 4 | yes | 32 s / 55 s | 6 ms / 17 ms / 0.54 s | 0 |
| 100 (50) | 500 | 2 × 4 | yes | 50 s / 89 s | 21 ms / 240 ms / 3.6 s | 0 |

Build times here are mostly queueing: a scripted build needs about 8 s of work.
With 100 builds in flight and 500 open streams, every build completed and no
stream was refused or dropped. The worst API call took 3.6 s, under the load of
500 streams each checking for changes every second against SQLite.

## What this says about cavman.dev

- **The platform is not the bottleneck.** The API, queue and streams handled far
  more than the beta will see.
- **Worker slots are.** A real build takes 5 to 30 minutes of model time, and
  cavman.dev runs one worker with `CAVMAN_WORKER_CONCURRENCY=1`. So builds run one
  at a time, and a second user waits for the first build to finish. Idle slots
  cost nothing, so raising concurrency to 3 or 4 on the one Droplet is the
  cheap fix (owner's decision). Check first that the E2B plan allows that many
  sandboxes at once; the checklist already notes the free plan's concurrency
  limit as a reason to upgrade before a public launch. Account caps (2 concurrent builds, monthly allowance) still apply.
- **Not covered here:** PostgreSQL (production's database; run with
  `--database-url`), E2B sandboxes, which add network round trips per check,
  real model latency, and Caddy in front of the streams.
