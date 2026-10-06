"""Platform records: project ownership, run ownership, budgets and the durable job queue.

This store never duplicates orchestration truth. Tasks, artifacts, validation,
review, approvals and completion live only in the core's operational store; a
row here records *who owns* a run and *whether a worker is executing it*.

Jobs are leased. A worker that dies stops heartbeating, its lease expires, and
the next claimant turns the orphaned job into an explicit ``recover`` job, which
runs the core's offline interruption recovery before handing the run back to
the Manager. Browser connections play no part in execution.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from walter.pg import PostgresConnection, is_postgres_url
from walter.store import enable_wal

JOB_ACTIVE = ("queued", "running")

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects(
  id TEXT PRIMARY KEY,
  owner_id TEXT NOT NULL,
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  settings_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS projects_owner ON projects(owner_id, created_at);
CREATE TABLE IF NOT EXISTS runs(
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  owner_id TEXT NOT NULL,
  prompt TEXT NOT NULL,
  executor TEXT NOT NULL,
  budget_usd REAL NOT NULL,
  max_model_calls INTEGER NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS runs_owner ON runs(owner_id, created_at);
CREATE INDEX IF NOT EXISTS runs_project ON runs(project_id, created_at);
CREATE INDEX IF NOT EXISTS runs_created ON runs(created_at);
CREATE TABLE IF NOT EXISTS jobs(
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(id),
  kind TEXT NOT NULL,
  message TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL,
  outcome TEXT,
  detail TEXT,
  lease_owner TEXT,
  lease_expires REAL,
  cancel_requested INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT
);
CREATE INDEX IF NOT EXISTS jobs_run ON jobs(run_id, created_at);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, created_at);
CREATE TABLE IF NOT EXISTS publications(
  run_id TEXT PRIMARY KEY REFERENCES runs(id),
  owner_id TEXT NOT NULL,
  repo_full_name TEXT NOT NULL,
  html_url TEXT NOT NULL,
  commit_sha TEXT NOT NULL,
  private INTEGER NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS workflow_state(
  run_id TEXT PRIMARY KEY REFERENCES runs(id),
  state_json TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS project_leases(
  project_id TEXT PRIMARY KEY,
  holder TEXT NOT NULL,
  expires REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS maintenance(
  name TEXT PRIMARY KEY,
  last_run REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS retired_runs(
  run_id TEXT PRIMARY KEY,
  retired_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rate_events(
  owner_id TEXT NOT NULL,
  action TEXT NOT NULL,
  at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS rate_events_owner ON rate_events(owner_id, action, at);
CREATE TABLE IF NOT EXISTS job_notices(
  job_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  noticed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS job_notices_run ON job_notices(run_id);
CREATE TABLE IF NOT EXISTS run_instructions(
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(id),
  text TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS run_instructions_run ON run_instructions(run_id, created_at);
CREATE TABLE IF NOT EXISTS deliveries(
  run_id TEXT PRIMARY KEY REFERENCES runs(id),
  status TEXT NOT NULL,
  manifest_json TEXT NOT NULL,
  archive_name TEXT,
  created_at TEXT NOT NULL
);
"""


class ActiveWork(RuntimeError):
    """The account still has work in flight."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _page(where: str, params: tuple, before: tuple[str, str] | None) -> tuple[str, tuple]:
    """Keyset pagination on (created_at, id), newest first: stable while new rows arrive."""
    if before is None:
        return where, params
    created_at, row_id = before
    return (f"{where} AND (created_at < ? OR (created_at = ? AND id < ?))",
            params + (created_at, created_at, row_id))


def new_id() -> str:
    return uuid4().hex


@dataclass(frozen=True)
class Project:
    id: str
    owner_id: str
    name: str
    description: str
    settings: dict
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class RunRecord:
    id: str
    project_id: str
    owner_id: str
    prompt: str
    executor: str
    budget_usd: float
    max_model_calls: int
    created_at: str
    model_mode: str = "automatic"


@dataclass(frozen=True)
class Job:
    id: str
    run_id: str
    kind: str
    message: str
    status: str
    outcome: str | None
    detail: str | None
    lease_owner: str | None
    lease_expires: float | None
    cancel_requested: bool
    created_at: str
    started_at: str | None
    finished_at: str | None


@dataclass(frozen=True)
class Delivery:
    run_id: str
    status: str
    manifest: dict
    archive_name: str | None
    created_at: str


def _postgres_schema() -> str:
    """The same tables for PostgreSQL: full-precision floats, the model_mode
    column created directly, and an insertion-order column for jobs."""
    schema = re.sub(r"\bREAL\b", "DOUBLE PRECISION", SCHEMA)
    schema = schema.replace("  max_model_calls INTEGER NOT NULL,\n  created_at TEXT NOT NULL\n);",
                            "  max_model_calls INTEGER NOT NULL,\n  created_at TEXT NOT NULL,\n"
                            "  model_mode TEXT NOT NULL DEFAULT 'automatic'\n);")
    schema = schema.replace("  started_at TEXT,\n  finished_at TEXT\n);",
                            "  started_at TEXT,\n  finished_at TEXT,\n  seq BIGINT GENERATED ALWAYS AS IDENTITY\n);")
    return schema


class PlatformStore:
    def __init__(self, path: str | Path, *, schema: str = "cavman_platform"):
        self._lock = threading.RLock()
        if is_postgres_url(path):
            self.connection = PostgresConnection(str(path), schema)
            try:
                with self._write() as db:
                    db.executescript(_postgres_schema())
            except BaseException:
                self.connection.close()
                raise
            return
        self.connection = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA busy_timeout=5000")
        if str(path) != ":memory:":
            enable_wal(self.connection)
        self.connection.executescript(SCHEMA)
        # Additive migrations for databases created by earlier versions, under the
        # write lock and re-checked, since API and workers start together.
        with self._write() as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(runs)")}
            if "model_mode" not in columns:
                db.execute("ALTER TABLE runs ADD COLUMN model_mode TEXT NOT NULL DEFAULT 'automatic'")

    def close(self) -> None:
        with self._lock:
            self.connection.close()

    @contextmanager
    def _write(self):
        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                yield self.connection
                self.connection.execute("COMMIT")
            except BaseException:
                self.connection.execute("ROLLBACK")
                raise

    def _query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.connection.execute(sql, params).fetchall()

    # Project leases and maintenance --------------------------------------

    def acquire_project(self, project_id: str, holder: str, seconds: float, now: float | None = None) -> bool:
        """Hold a project for work outside a job (maintenance, delivery retries).

        Refused while one of the project's jobs runs or another holder's lease
        is current; the job queue will not start the project's jobs meanwhile.
        """
        now = time.time() if now is None else now
        with self._write() as db:
            running = db.execute("SELECT COUNT(*) FROM jobs JOIN runs ON runs.id = jobs.run_id "
                                 "WHERE runs.project_id=? AND jobs.status='running'", (project_id,)).fetchone()[0]
            if running:
                return False
            lease = db.execute("SELECT holder, expires FROM project_leases WHERE project_id=?",
                               (project_id,)).fetchone()
            if lease is not None and lease[0] != holder and lease[1] >= now:
                return False
            db.execute("INSERT INTO project_leases VALUES(?,?,?) ON CONFLICT(project_id) DO UPDATE SET "
                       "holder=excluded.holder, expires=excluded.expires", (project_id, holder, now + seconds))
        return True

    def release_project(self, project_id: str, holder: str) -> None:
        with self._write() as db:
            db.execute("DELETE FROM project_leases WHERE project_id=? AND holder=?", (project_id, holder))

    def due(self, name: str, interval_seconds: float, now: float | None = None) -> bool:
        """True for exactly one caller per interval across all workers."""
        now = time.time() if now is None else now
        with self._write() as db:
            row = db.execute("SELECT last_run FROM maintenance WHERE name=?", (name,)).fetchone()
            if row is not None and row[0] > now - interval_seconds:
                return False
            db.execute("INSERT INTO maintenance VALUES(?,?) ON CONFLICT(name) DO UPDATE SET "
                       "last_run=excluded.last_run", (name, now))
        return True

    def unretired_runs(self, limit: int = 1000) -> list[RunRecord]:
        """Runs without queued or running work whose workspaces have not been retired."""
        return [self._run(row) for row in self._query(
            "SELECT * FROM runs WHERE id NOT IN (SELECT run_id FROM retired_runs) "
            "AND id NOT IN (SELECT run_id FROM jobs WHERE status IN ('queued','running')) "
            "ORDER BY created_at LIMIT ?", (limit,))]

    def mark_retired(self, run_id: str) -> None:
        with self._write() as db:
            db.execute("INSERT INTO retired_runs VALUES(?,?) ON CONFLICT(run_id) DO NOTHING", (run_id, _now()))

    def prune_rate_events(self, older_than: float) -> int:
        with self._write() as db:
            return db.execute("DELETE FROM rate_events WHERE at < ?", (older_than,)).rowcount

    # Limits -------------------------------------------------------------

    def consume_rate(self, owner_id: str, action: str, limit: int, window_seconds: float,
                     now: float | None = None) -> float | None:
        """Record one ``action`` unless ``limit`` were already used in the window.

        Returns None when allowed, otherwise the seconds until a slot frees up.
        Kept in the shared database so the limit holds across API hosts.
        """
        now = time.time() if now is None else now
        start = now - window_seconds
        with self._write() as db:
            db.execute("DELETE FROM rate_events WHERE owner_id=? AND action=? AND at < ?", (owner_id, action, start))
            recent = [row[0] for row in db.execute(
                "SELECT at FROM rate_events WHERE owner_id=? AND action=? ORDER BY at", (owner_id, action))]
            if len(recent) >= limit:
                return max(1.0, recent[len(recent) - limit] + window_seconds - now)
            db.execute("INSERT INTO rate_events VALUES(?,?,?)", (owner_id, action, now))
        return None

    def active_build_count(self, owner_id: str) -> int:
        """Runs of this owner with queued or running work."""
        return self._query("SELECT COUNT(DISTINCT jobs.run_id) FROM jobs JOIN runs ON runs.id = jobs.run_id "
                           "WHERE runs.owner_id=? AND jobs.status IN ('queued','running')", (owner_id,))[0][0]

    def project_count(self, owner_id: str) -> int:
        return self._query("SELECT COUNT(*) FROM projects WHERE owner_id=?", (owner_id,))[0][0]

    # Account erasure ----------------------------------------------------

    def delete_owner(self, owner_id: str) -> dict:
        """Remove every platform record owned by ``owner_id`` in one transaction.

        Refused while any of the owner's jobs is running; queued jobs go with
        their runs, so no worker can claim them afterwards.
        """
        owned = "SELECT id FROM runs WHERE owner_id=?"
        with self._write() as db:
            running = db.execute(f"SELECT COUNT(*) FROM jobs WHERE status='running' AND run_id IN ({owned})",
                                 (owner_id,)).fetchone()[0]
            if running:
                raise ActiveWork("A build is running. Stop it, or wait for it to finish, before deleting your account.")
            runs = [row[0] for row in db.execute(owned + " ORDER BY created_at", (owner_id,))]
            projects = [row[0] for row in db.execute("SELECT id FROM projects WHERE owner_id=?", (owner_id,))]
            for table in ("publications", "workflow_state", "deliveries", "job_notices", "run_instructions", "jobs",
                          "retired_runs"):
                db.execute(f"DELETE FROM {table} WHERE run_id IN ({owned})", (owner_id,))
            db.execute("DELETE FROM publications WHERE owner_id=?", (owner_id,))
            db.execute("DELETE FROM rate_events WHERE owner_id=?", (owner_id,))
            db.execute("DELETE FROM runs WHERE owner_id=?", (owner_id,))
            db.execute(f"DELETE FROM project_leases WHERE project_id IN "
                       f"(SELECT id FROM projects WHERE owner_id=?)", (owner_id,))
            db.execute("DELETE FROM projects WHERE owner_id=?", (owner_id,))
        return {"runs": runs, "projects": projects}

    def known_ids(self) -> tuple[set[str], set[str]]:
        """All run ids and project ids the platform still owns."""
        return ({row[0] for row in self._query("SELECT id FROM runs")},
                {row[0] for row in self._query("SELECT id FROM projects")})

    # Projects -----------------------------------------------------------

    @staticmethod
    def _project(row) -> Project:
        return Project(row["id"], row["owner_id"], row["name"], row["description"],
                       json.loads(row["settings_json"]), row["created_at"], row["updated_at"])

    def create_project(self, owner_id: str, name: str, description: str = "",
                       settings: dict | None = None, project_id: str | None = None) -> Project:
        if not owner_id.strip():
            raise ValueError("A project needs an owner")
        timestamp = _now()
        project_id = project_id or new_id()
        with self._write() as db:
            db.execute("INSERT INTO projects VALUES(?,?,?,?,?,?,?)",
                       (project_id, owner_id, name, description,
                        json.dumps(settings or {}, sort_keys=True), timestamp, timestamp))
        return self.get_project(owner_id, project_id)

    def get_project(self, owner_id: str, project_id: str) -> Project | None:
        rows = self._query("SELECT * FROM projects WHERE id=? AND owner_id=?", (project_id, owner_id))
        return self._project(rows[0]) if rows else None

    def project_by_id(self, project_id: str) -> Project | None:
        """Trusted worker-side lookup; never reachable from a user request path."""
        rows = self._query("SELECT * FROM projects WHERE id=?", (project_id,))
        return self._project(rows[0]) if rows else None

    def list_projects(self, owner_id: str, *, limit: int | None = None,
                      before: tuple[str, str] | None = None) -> list[Project]:
        """Newest first; ``before`` is the (created_at, id) of the last item already shown."""
        where, params = _page("owner_id=?", (owner_id,), before)
        return [self._project(row) for row in self._query(
            f"SELECT * FROM projects WHERE {where} ORDER BY created_at DESC, id DESC"
            + (" LIMIT ?" if limit else ""), params + ((limit,) if limit else ()))]

    def touch_project(self, project_id: str) -> None:
        with self._write() as db:
            db.execute("UPDATE projects SET updated_at=? WHERE id=?", (_now(), project_id))

    # Runs ---------------------------------------------------------------

    @staticmethod
    def _run(row) -> RunRecord:
        return RunRecord(row["id"], row["project_id"], row["owner_id"], row["prompt"],
                         row["executor"], row["budget_usd"], row["max_model_calls"], row["created_at"],
                         row["model_mode"])

    def create_run(self, run_id: str, project_id: str, owner_id: str, prompt: str, *,
                   executor: str, budget_usd: float, max_model_calls: int,
                   model_mode: str = "automatic") -> RunRecord:
        with self._write() as db:
            owner = db.execute("SELECT owner_id FROM projects WHERE id=?", (project_id,)).fetchone()
            if owner is None or owner[0] != owner_id:
                raise PermissionError("Project does not belong to this user")
            db.execute("INSERT INTO runs(id,project_id,owner_id,prompt,executor,budget_usd,max_model_calls,"
                       "created_at,model_mode) VALUES(?,?,?,?,?,?,?,?,?)",
                       (run_id, project_id, owner_id, prompt, executor, budget_usd,
                        max_model_calls, _now(), model_mode))
            db.execute("UPDATE projects SET updated_at=? WHERE id=?", (_now(), project_id))
        return self.run_by_id(run_id)

    def get_run(self, owner_id: str, run_id: str) -> RunRecord | None:
        rows = self._query("SELECT * FROM runs WHERE id=? AND owner_id=?", (run_id, owner_id))
        return self._run(rows[0]) if rows else None

    def run_by_id(self, run_id: str) -> RunRecord | None:
        """Trusted worker-side lookup; never reachable from a user request path."""
        rows = self._query("SELECT * FROM runs WHERE id=?", (run_id,))
        return self._run(rows[0]) if rows else None

    def list_runs(self, owner_id: str, project_id: str | None = None, *, limit: int | None = None,
                  before: tuple[str, str] | None = None) -> list[RunRecord]:
        """Newest first; ``before`` is the (created_at, id) of the last item already shown."""
        where, params = ("owner_id=?", (owner_id,)) if project_id is None else \
            ("owner_id=? AND project_id=?", (owner_id, project_id))
        where, params = _page(where, params, before)
        rows = self._query(f"SELECT * FROM runs WHERE {where} ORDER BY created_at DESC, id DESC"
                           + (" LIMIT ?" if limit else ""), params + ((limit,) if limit else ()))
        return [self._run(row) for row in rows]

    def count_runs(self, owner_id: str, project_id: str) -> int:
        return self._query("SELECT COUNT(*) FROM runs WHERE owner_id=? AND project_id=?",
                           (owner_id, project_id))[0][0]

    def set_budget(self, run_id: str, budget_usd: float) -> None:
        with self._write() as db:
            db.execute("UPDATE runs SET budget_usd=? WHERE id=?", (budget_usd, run_id))

    # Jobs ---------------------------------------------------------------

    @staticmethod
    def _job(row) -> Job:
        return Job(row["id"], row["run_id"], row["kind"], row["message"], row["status"],
                   row["outcome"], row["detail"], row["lease_owner"], row["lease_expires"],
                   bool(row["cancel_requested"]), row["created_at"], row["started_at"],
                   row["finished_at"])

    def enqueue(self, run_id: str, kind: str, message: str = "") -> tuple[Job, bool]:
        """Queue execution for a run unless it already has active work.

        Returns (job, created). At most one job per run is ever queued or
        running, so two browser tabs cannot start two Managers on one run.
        """
        if kind not in {"start", "continue", "recover"}:
            raise ValueError(f"Unknown job kind {kind}")
        with self._write() as db:
            active = db.execute(
                "SELECT * FROM jobs WHERE run_id=? AND status IN ('queued','running') "
                "ORDER BY created_at LIMIT 1", (run_id,)).fetchone()
            if active is not None:
                return self._job(active), False
            job_id = new_id()
            db.execute("INSERT INTO jobs(id,run_id,kind,message,status,created_at) VALUES(?,?,?,?,?,?)",
                       (job_id, run_id, kind, message, "queued", _now()))
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return self._job(row), True

    def jobs(self, run_id: str) -> list[Job]:
        return [self._job(row) for row in self._query(
            "SELECT * FROM jobs WHERE run_id=? ORDER BY created_at, rowid", (run_id,))]

    def latest_job(self, run_id: str) -> Job | None:
        rows = self._query("SELECT * FROM jobs WHERE run_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                           (run_id,))
        return self._job(rows[0]) if rows else None

    def active_job(self, run_id: str) -> Job | None:
        rows = self._query("SELECT * FROM jobs WHERE run_id=? AND status IN ('queued','running') "
                           "ORDER BY created_at LIMIT 1", (run_id,))
        return self._job(rows[0]) if rows else None

    def reap_expired(self, max_recoveries: int, now: float | None = None) -> list[Job]:
        """Turn jobs whose worker stopped heartbeating into explicit recovery work."""
        now = time.time() if now is None else now
        created = []
        with self._write() as db:
            expired = db.execute("SELECT * FROM jobs WHERE status='running' AND lease_expires < ?",
                                 (now,)).fetchall()
            for row in expired:
                db.execute("UPDATE jobs SET status='failed', outcome='interrupted', "
                           "detail='The worker executing this job stopped responding.', "
                           "finished_at=?, lease_owner=NULL WHERE id=?", (_now(), row["id"]))
                recoveries = db.execute("SELECT COUNT(*) FROM jobs WHERE run_id=? AND kind='recover'",
                                        (row["run_id"],)).fetchone()[0]
                if recoveries < max_recoveries and not row["cancel_requested"]:
                    job_id = new_id()
                    db.execute("INSERT INTO jobs(id,run_id,kind,message,status,created_at) "
                               "VALUES(?,?,?,?,?,?)",
                               (job_id, row["run_id"], "recover", "", "queued", _now()))
                    created.append(self._job(db.execute("SELECT * FROM jobs WHERE id=?",
                                                        (job_id,)).fetchone()))
        return created

    def claim(self, worker_id: str, lease_seconds: float, now: float | None = None) -> Job | None:
        """Lease the oldest queued job whose project is free.

        A project's sandbox state (candidate grants, integration branch) is
        owned by one job at a time, so a job waits while another job of the
        same project runs or while maintenance holds the project.
        """
        now = time.time() if now is None else now
        with self._write() as db:
            row = db.execute(
                "SELECT jobs.* FROM jobs JOIN runs ON runs.id = jobs.run_id WHERE jobs.status='queued' "
                "AND runs.project_id NOT IN (SELECT busy.project_id FROM jobs AS active "
                "JOIN runs AS busy ON busy.id = active.run_id WHERE active.status='running') "
                "AND runs.project_id NOT IN (SELECT project_id FROM project_leases WHERE expires >= ?) "
                "ORDER BY jobs.created_at, jobs.rowid LIMIT 1", (now,)).fetchone()
            if row is None:
                return None
            db.execute("UPDATE jobs SET status='running', lease_owner=?, lease_expires=?, started_at=? "
                       "WHERE id=? AND status='queued'",
                       (worker_id, now + lease_seconds, _now(), row["id"]))
            claimed = db.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()
        return self._job(claimed)

    def heartbeat(self, job_id: str, worker_id: str, lease_seconds: float) -> bool:
        """Extend the lease; returns True when cancellation was requested."""
        with self._write() as db:
            row = db.execute("SELECT lease_owner, cancel_requested, status FROM jobs WHERE id=?",
                             (job_id,)).fetchone()
            if row is None or row["lease_owner"] != worker_id or row["status"] != "running":
                # The lease was lost (reaped). Stop: another claimant owns recovery now.
                return True
            db.execute("UPDATE jobs SET lease_expires=? WHERE id=?", (time.time() + lease_seconds, job_id))
            return bool(row["cancel_requested"])

    def finish(self, job_id: str, worker_id: str, status: str, outcome: str, detail: str = "") -> None:
        if status not in {"succeeded", "failed", "cancelled"}:
            raise ValueError(f"Unknown terminal job status {status}")
        with self._write() as db:
            db.execute("UPDATE jobs SET status=?, outcome=?, detail=?, finished_at=?, lease_owner=NULL "
                       "WHERE id=? AND lease_owner=? AND status='running'",
                       (status, outcome, detail, _now(), job_id, worker_id))

    def unnoticed_jobs(self, since: str, limit: int = 50) -> list[Job]:
        """Jobs that ended at or after ``since`` and nobody has been told about yet.

        Cancelled jobs are left out: the user stopped them and already knows.
        """
        return [self._job(row) for row in self._query(
            "SELECT * FROM jobs WHERE status IN ('succeeded','failed') AND finished_at >= ? "
            "AND NOT EXISTS (SELECT 1 FROM job_notices WHERE job_notices.job_id = jobs.id) "
            "ORDER BY finished_at LIMIT ?", (since, limit))]

    def job_by_id(self, job_id: str) -> Job | None:
        rows = self._query("SELECT * FROM jobs WHERE id=?", (job_id,))
        return self._job(rows[0]) if rows else None

    def mark_noticed(self, job: Job) -> None:
        """Record that ``job``'s ending was told to its owner, or needs no telling."""
        with self._write() as db:
            db.execute("INSERT INTO job_notices VALUES(?,?,?) ON CONFLICT(job_id) DO NOTHING",
                       (job.id, job.run_id, _now()))

    # Owner instructions ---------------------------------------------------

    def add_instruction(self, run_id: str, text: str) -> dict:
        item = {"id": new_id(), "run_id": run_id, "text": text, "created_at": _now()}
        with self._write() as db:
            db.execute("INSERT INTO run_instructions(id, run_id, text, created_at) VALUES(?,?,?,?)",
                       (item["id"], run_id, text, item["created_at"]))
        return item

    def instructions(self, run_id: str) -> list[dict]:
        return [dict(row) for row in self._query(
            "SELECT id, text, created_at FROM run_instructions WHERE run_id=? ORDER BY created_at, id", (run_id,))]

    def request_cancel(self, run_id: str) -> Job | None:
        with self._write() as db:
            row = db.execute("SELECT * FROM jobs WHERE run_id=? AND status IN ('queued','running') "
                             "ORDER BY created_at LIMIT 1", (run_id,)).fetchone()
            if row is None:
                return None
            if row["status"] == "queued":
                db.execute("UPDATE jobs SET status='cancelled', outcome='cancelled', "
                           "detail='Stopped before a worker picked it up.', finished_at=? WHERE id=?",
                           (_now(), row["id"]))
            else:
                db.execute("UPDATE jobs SET cancel_requested=1 WHERE id=?", (row["id"],))
            return self._job(db.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone())

    # Operations -----------------------------------------------------------

    def job_counts(self) -> dict[tuple[str, str], int]:
        rows = self._query("SELECT status, COALESCE(outcome, '') AS outcome, COUNT(*) AS n FROM jobs "
                           "GROUP BY status, outcome")
        return {(row["status"], row["outcome"]): row["n"] for row in rows}

    def queue_stats(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        queued = self._query("SELECT created_at FROM jobs WHERE status='queued' ORDER BY created_at LIMIT 1")
        oldest = 0.0
        if queued:
            oldest = max(0.0, now - datetime.fromisoformat(queued[0]["created_at"]).timestamp())
        expired = self._query("SELECT COUNT(*) AS n FROM jobs WHERE status='running' AND lease_expires < ?", (now,))
        return {"oldest_queued_seconds": oldest, "expired_leases": expired[0]["n"]}

    def all_runs(self) -> list[RunRecord]:
        """Operator view across every owner; never reachable from a user request."""
        return [self._run(row) for row in self._query("SELECT * FROM runs ORDER BY created_at DESC")]

    def runs_created_since(self, since: str) -> list[RunRecord]:
        """Operator view of runs created at or after ``since`` (ISO-8601 UTC)."""
        return [self._run(row) for row in self._query(
            "SELECT * FROM runs WHERE created_at >= ? ORDER BY created_at DESC", (since,))]

    def runs_with_work_since(self, since: str) -> list[RunRecord]:
        """Runs that may have made model calls at or after ``since``.

        Model calls happen only inside a job, so a run whose every job finished
        before ``since`` is left out. Runs with no job at all are kept.
        """
        return [self._run(row) for row in self._query(
            "SELECT * FROM runs WHERE NOT EXISTS (SELECT 1 FROM jobs WHERE jobs.run_id = runs.id) "
            "OR EXISTS (SELECT 1 FROM jobs WHERE jobs.run_id = runs.id "
            "AND (jobs.finished_at IS NULL OR jobs.finished_at >= ?)) ORDER BY created_at DESC", (since,))]

    def delivery_counts(self) -> dict[str, int]:
        return {row["status"]: row["n"] for row in self._query(
            "SELECT status, COUNT(*) AS n FROM deliveries GROUP BY status")}

    # Publications ---------------------------------------------------------

    def publication(self, run_id: str) -> dict | None:
        rows = self._query("SELECT * FROM publications WHERE run_id=?", (run_id,))
        return dict(rows[0]) if rows else None

    def save_publication(self, run_id: str, owner_id: str, repo_full_name: str, html_url: str,
                         commit_sha: str, private: bool) -> dict:
        with self._write() as db:
            db.execute("INSERT INTO publications VALUES(?,?,?,?,?,?,?)",
                       (run_id, owner_id, repo_full_name, html_url, commit_sha, int(private), _now()))
        return self.publication(run_id)

    # Workflow state -------------------------------------------------------

    def workflow_state(self, run_id: str) -> dict | None:
        rows = self._query("SELECT state_json FROM workflow_state WHERE run_id=?", (run_id,))
        return json.loads(rows[0]["state_json"]) if rows else None

    def save_workflow_state(self, run_id: str, state: dict) -> None:
        with self._write() as db:
            db.execute("INSERT INTO workflow_state VALUES(?,?,?) ON CONFLICT(run_id) DO UPDATE SET "
                       "state_json=excluded.state_json, updated_at=excluded.updated_at",
                       (run_id, json.dumps(state, sort_keys=True), _now()))

    # Deliveries ---------------------------------------------------------

    def save_delivery(self, run_id: str, status: str, manifest: dict, archive_name: str | None) -> Delivery:
        with self._write() as db:
            db.execute("INSERT INTO deliveries VALUES(?,?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET "
                       "status=excluded.status, manifest_json=excluded.manifest_json, "
                       "archive_name=excluded.archive_name, created_at=excluded.created_at",
                       (run_id, status, json.dumps(manifest, sort_keys=True), archive_name, _now()))
        return self.delivery(run_id)

    def delivery(self, run_id: str) -> Delivery | None:
        rows = self._query("SELECT * FROM deliveries WHERE run_id=?", (run_id,))
        if not rows:
            return None
        row = rows[0]
        return Delivery(row["run_id"], row["status"], json.loads(row["manifest_json"]),
                        row["archive_name"], row["created_at"])
