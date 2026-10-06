#!/usr/bin/env python3
"""Load test: many concurrent builds and live-update connections against a real stack.

Starts the API (uvicorn, real HTTP) and worker processes with the scripted
executor: scripted models, but the real kernel, sandbox, queue and event
stream. It spends no provider credit.

    .venv/bin/python scripts/load_test.py --users 10 --builds-per-user 2 --streams-per-run 3 \
        --workers 2 --worker-concurrency 2

Reports build completion times, API latency while under load, and how the live
event streams behaved. State is SQLite unless --database-url names PostgreSQL.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[1]
TOKEN = "load-test-" + "x" * 40
PROMPTS = ["Build me a booking app for a tattoo studio", "Booking core #dependent", "Booking core #parallel"]


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--users", type=int, default=10)
    parser.add_argument("--builds-per-user", type=int, default=2,
                        help="At most the per-account concurrent build cap (2 by default)")
    parser.add_argument("--streams-per-run", type=int, default=2, help="Live-update connections per build")
    parser.add_argument("--workers", type=int, default=2, help="Worker processes")
    parser.add_argument("--worker-concurrency", type=int, default=2, help="Execution slots per worker")
    parser.add_argument("--step-delay", type=float, default=0.15, help="Seconds per scripted model step")
    parser.add_argument("--timeout", type=float, default=900, help="Give up after this many seconds")
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--out", type=Path, default=None, help="Write the JSON report here too")
    return parser.parse_args(argv)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(q * len(ordered)))], 3)


async def stream(client: httpx.AsyncClient, run_id: str, headers: dict, stats: dict, stop: asyncio.Event):
    """Hold a live-update connection open, reconnecting as a browser would, until the run ends."""
    while not stop.is_set():
        stats["opened"] += 1
        try:
            async with client.stream("GET", f"/api/runs/{run_id}/stream", headers=headers, timeout=None) as response:
                if response.status_code != 200:
                    stats["refused"] += 1
                    await asyncio.sleep(1)
                    continue
                async for line in response.aiter_lines():
                    if line.startswith("data:"):
                        stats["events"] += 1
                    if stop.is_set():
                        break
        except (httpx.HTTPError, OSError):
            stats["errors"] += 1
            await asyncio.sleep(1)


async def drive(args, base: str) -> dict:
    limits = httpx.Limits(max_connections=None, max_keepalive_connections=None)
    async with httpx.AsyncClient(base_url=base, limits=limits, timeout=30) as client:
        latencies: list[float] = []
        failures: list[str] = []
        runs: list[tuple[str, dict, float]] = []
        started = time.monotonic()
        for user in range(args.users):
            headers = {"Authorization": f"Bearer {TOKEN}", "X-Cavman-User": f"load-user-{user}"}
            for index in range(args.builds_per_user):
                prompt = PROMPTS[(user + index) % len(PROMPTS)]
                response = await client.post("/api/builds", json={"prompt": prompt}, headers=headers)
                if response.status_code != 201:
                    failures.append(f"create {response.status_code}: {response.text[:200]}")
                    continue
                runs.append((response.json()["run_id"], headers, time.monotonic()))
        stop = asyncio.Event()
        stream_stats = {"opened": 0, "refused": 0, "errors": 0, "events": 0}
        streams = [asyncio.create_task(stream(client, run_id, headers, stream_stats, stop))
                   for run_id, headers, _ in runs for _ in range(args.streams_per_run)]
        done: dict[str, float] = {}
        states: dict[str, str] = {}
        deadline = started + args.timeout
        while len(done) < len(runs) and time.monotonic() < deadline:
            for run_id, headers, created in runs:
                if run_id in done:
                    continue
                began = time.monotonic()
                response = await client.get(f"/api/runs/{run_id}", headers=headers)
                latencies.append(time.monotonic() - began)
                if response.status_code != 200:
                    failures.append(f"detail {response.status_code}")
                    continue
                state = response.json()["state"]
                states[run_id] = state
                if state in {"complete", "failed", "approval_needed", "blocked", "waiting", "budget_reached"}:
                    done[run_id] = time.monotonic() - created
            began = time.monotonic()
            listing = await client.get("/api/runs", headers=runs[0][1]) if runs else None
            if listing is not None:
                latencies.append(time.monotonic() - began)
            await asyncio.sleep(0.5)
        stop.set()
        await asyncio.sleep(0.2)
        for task in streams:
            task.cancel()
        await asyncio.gather(*streams, return_exceptions=True)
        elapsed = time.monotonic() - started
        completed = [run_id for run_id, state in states.items() if state == "complete"]
        return {
            "builds": len(runs), "completed": len(completed),
            "final_states": {state: list(states.values()).count(state) for state in set(states.values())},
            "timed_out": len(runs) - len(done), "elapsed_s": round(elapsed, 1),
            "build_seconds": {"p50": pct(list(done.values()), 0.5), "p95": pct(list(done.values()), 0.95),
                              "max": pct(list(done.values()), 1.0)},
            "api_latency_seconds": {"samples": len(latencies), "p50": pct(latencies, 0.5),
                                    "p95": pct(latencies, 0.95), "max": pct(latencies, 1.0)},
            "streams": {"concurrent": len(streams), **stream_stats},
            "failures": failures[:20],
        }


def main(argv=None) -> int:
    args = parse_args(argv)
    data = Path(tempfile.mkdtemp(prefix="cavman-load-"))
    port = free_port()
    env = {**os.environ, "CAVMAN_API_TOKEN": TOKEN, "CAVMAN_EXECUTOR": "scripted",
           "CAVMAN_DATA_DIR": str(data), "CAVMAN_SCRIPTED_STEP_DELAY": str(args.step_delay),
           "CAVMAN_HEARTBEAT_SECONDS": "0.5", "CAVMAN_WORKER_CONCURRENCY": str(args.worker_concurrency),
           "CAVMAN_BUILDS_PER_HOUR": "1000"}
    if args.database_url:
        env["CAVMAN_DATABASE_URL"] = args.database_url
    bin_dir = Path(sys.executable).parent
    processes = [subprocess.Popen([str(bin_dir / "cavman"), "api", "--port", str(port)], env=env,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)]
    try:
        base = f"http://127.0.0.1:{port}"
        for _ in range(120):
            try:
                if httpx.get(base + "/api/health", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.5)
        else:
            print("The API did not start", file=sys.stderr)
            return 2
        processes += [subprocess.Popen([str(bin_dir / "cavman"), "worker"], env=env, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL) for _ in range(args.workers)]
        report = asyncio.run(drive(args, base))
        report["setup"] = {"users": args.users, "builds_per_user": args.builds_per_user,
                           "streams_per_run": args.streams_per_run, "workers": args.workers,
                           "worker_concurrency": args.worker_concurrency, "step_delay_s": args.step_delay,
                           "database": "postgres" if args.database_url else "sqlite"}
        print(json.dumps(report, indent=2))
        if args.out:
            args.out.write_text(json.dumps(report, indent=2))
        return 0 if report["completed"] == report["builds"] and not report["failures"] else 1
    finally:
        for process in processes:
            process.send_signal(signal.SIGTERM)
        for process in processes:
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()


if __name__ == "__main__":
    raise SystemExit(main())
