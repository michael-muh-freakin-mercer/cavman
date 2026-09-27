"""Atomic SQLite snapshots and append-only audit events with optimistic concurrency."""
from __future__ import annotations
import json
import hashlib
import logging
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from pydantic import ValidationError
from .models import BlockerReason, Event, Run, now

logger = logging.getLogger(__name__)


class ConcurrentUpdate(RuntimeError):
    pass


class SnapshotIncompatible(ValueError):
    """A persisted snapshot cannot be loaded by this build; remediation is required."""


def _format_location(loc: tuple) -> str:
    return ".".join(str(part) for part in loc) if loc else "<root>"


def _drop_path(document: object, loc: tuple) -> None:
    """Remove an extra field addressed by a Pydantic error loc from the raw payload."""
    target = document
    for part in loc[:-1]:
        target = target[part] if not isinstance(target, list) else target[int(part)]
    if isinstance(target, list):
        del target[int(loc[-1])]
    else:
        target.pop(loc[-1], None)


class SQLiteStore:
    SCHEMA_VERSION = 2

    def __init__(self, path: str | Path = ":memory:"):
        self._lock = threading.RLock()
        self.connection = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA busy_timeout=5000")
        version = self.connection.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, self.SCHEMA_VERSION):
            self.connection.close()
            raise ValueError(f"Unsupported operational schema {version}")
        try:
            if version == 0:
                self.connection.executescript("""
                CREATE TABLE runs(id TEXT PRIMARY KEY, version INTEGER NOT NULL, snapshot TEXT NOT NULL);
                CREATE TABLE events(run_id TEXT NOT NULL REFERENCES runs(id), sequence INTEGER NOT NULL,
                  payload TEXT NOT NULL, PRIMARY KEY(run_id,sequence));
                CREATE TABLE schema_migration_backups(
                  run_id TEXT NOT NULL, from_version INTEGER NOT NULL, to_version INTEGER NOT NULL,
                  snapshot TEXT NOT NULL, migrated_at TEXT NOT NULL,
                  PRIMARY KEY(run_id,from_version,to_version));
                PRAGMA user_version=2;
                """)
            elif version == 1:
                self._migrate_v1_to_v2()
            else:
                self.connection.execute("""CREATE TABLE IF NOT EXISTS schema_migration_backups(
                  run_id TEXT NOT NULL, from_version INTEGER NOT NULL, to_version INTEGER NOT NULL,
                  snapshot TEXT NOT NULL, migrated_at TEXT NOT NULL,
                  PRIMARY KEY(run_id,from_version,to_version))""")
        except BaseException:
            self.connection.close()
            raise

    @staticmethod
    def _v2_snapshot(payload: str) -> str:
        """Transform the documented schema-v1 approval shape without discarding audit data."""
        document = json.loads(payload)
        run_id = document["id"]
        decisions = document.get("approval_decisions", {})
        for approval_id, approval in document.get("approvals", {}).items():
            decision = decisions.get(approval_id)
            scope = json.loads(approval["scope_json"])
            created_at = approval.get("created_at", now())
            if decision:
                status = "approved" if decision.get("approved") else "rejected"
                decided_at = decision.get("created_at", created_at)
                decision.setdefault("status", status)
            else:
                status, decided_at = "pending", None
            rationale = approval.pop("reason", None) or approval.get("rationale") or "Legacy approval request"
            approval.setdefault("run_id", run_id)
            approval.setdefault("category", approval.get("action", "legacy"))
            approval.setdefault("target", str(scope.get("target") or scope.get("task_id") or "run"))
            approval.setdefault("rationale", rationale)
            approval.setdefault("risk", "Unspecified legacy approval risk")
            approval.setdefault("artifact_refs", [scope["artifact_id"]] if scope.get("artifact_id") else [])
            approval.setdefault("requested_capability", scope.get("capability") if approval.get("action") == "change_capability" else None)
            approval.setdefault("required", True)
            approval.setdefault("status", status)
            approval.setdefault("updated_at", decided_at or created_at)
            approval.setdefault("decided_at", decided_at)
            approval.setdefault("superseded_at", None)
            approval.setdefault("replacement_id", None)
        approvals = document.get("approvals", {})
        decisions = document.get("approval_decisions", {})
        for task in document.get("tasks", {}).values():
            task.setdefault("approval_gates", [])
            unresolved = []
            for approval_id in task.get("approval_ids", []):
                approval = approvals.get(approval_id)
                try:
                    # A dictionary key/object identity mismatch or malformed scope is
                    # ambiguous and must be repaired explicitly by the Manager.
                    if not approval or approval.get("id") != approval_id or not approval.get("action"):
                        raise ValueError("missing or ambiguous approval request")
                    scope = json.loads(approval["scope_json"])
                    if not isinstance(scope, dict):
                        raise ValueError("approval scope is not an object")
                    scope_json = json.dumps(scope, sort_keys=True, separators=(",", ":"), allow_nan=False)
                    scope_digest = hashlib.sha256(scope_json.encode()).hexdigest()
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    unresolved.append(approval_id)
                    continue
                approval["scope_json"] = scope_json
                approval["scope_digest"] = scope_digest
                task["approval_gates"].append({
                    "request_id": approval_id,
                    "action": approval["action"],
                    "scope_json": scope_json,
                    "scope_digest": scope_digest,
                })
            task["approval_ids"] = unresolved
            if unresolved:
                # Raw-JSON context: use .value so the payload stays plain strings.
                task["blocker"] = (BlockerReason.LEGACY_APPROVAL_REGATE.value + ": "
                                   + ", ".join(unresolved))
                if task.get("status") in {"READY", "DELEGATED", "RUNNING", "SUBMITTED", "REVIEWING"}:
                    task["status"] = "BLOCKED"
            elif task["approval_gates"]:
                unsatisfied = []
                for gate in task["approval_gates"]:
                    request = approvals[gate["request_id"]]
                    decision = decisions.get(gate["request_id"])
                    if request["status"] != "approved" or not decision or not decision.get("approved"):
                        unsatisfied.append(request["status"])
                if unsatisfied and task.get("status") == "READY":
                    task["status"] = "BLOCKED"
                    task["blocker"] = (BlockerReason.APPROVAL_REJECTED.value
                                       if "rejected" in unsatisfied
                                       else BlockerReason.APPROVAL_PENDING.value)
            if not unresolved:
                task["approval_ids"] = []
        document["schema_version"] = 2
        return json.dumps(document, separators=(",", ":"), sort_keys=True)

    def _migrate_v1_to_v2(self):
        """Transactionally migrate snapshots, retaining exact source rows for recovery."""
        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                self.connection.execute("""CREATE TABLE IF NOT EXISTS schema_migration_backups(
                  run_id TEXT NOT NULL, from_version INTEGER NOT NULL, to_version INTEGER NOT NULL,
                  snapshot TEXT NOT NULL, migrated_at TEXT NOT NULL,
                  PRIMARY KEY(run_id,from_version,to_version))""")
                rows = self.connection.execute("SELECT id,snapshot FROM runs ORDER BY rowid").fetchall()
                timestamp = now()
                for run_id, snapshot in rows:
                    self.connection.execute("INSERT INTO schema_migration_backups VALUES(?,?,?,?,?)",
                        (run_id, 1, 2, snapshot, timestamp))
                    migrated = self._v2_snapshot(snapshot)
                    self.connection.execute("UPDATE runs SET snapshot=? WHERE id=?", (migrated, run_id))
                self.connection.execute("PRAGMA user_version=2")
                self.connection.execute("COMMIT")
            except BaseException:
                self.connection.execute("ROLLBACK")
                raise

    def close(self):
        with self._lock:
            self.connection.close()

    @contextmanager
    def transaction(self):
        """Serialize a whole read-modify-write against other in-process callers.

        ``load`` and ``save`` are individually thread-safe, but a caller that
        loads a snapshot, mutates it and saves it is only protected by the
        optimistic version check.  The Agents SDK executes several tool calls
        from one model turn concurrently (async tools on the event loop, sync
        tools on ``asyncio.to_thread`` worker threads), so that window is
        routinely contended and produced spurious ``ConcurrentUpdate`` errors.
        Holding this reentrant lock across the whole sequence removes the
        in-process race while leaving the version check to protect against
        other processes.
        """
        with self._lock:
            yield

    def _load_snapshot(self, run_id: str, payload: str) -> Run:
        """Validate a snapshot, pruning only unknown fields written by newer code shapes.

        Strict integrity is preserved: any defect other than an extra field (missing
        required value, wrong type, malformed JSON, version drift) fails loudly with a
        SnapshotIncompatible naming the run and the outstanding problems.
        """
        try:
            document = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise SnapshotIncompatible(
                f"Run {run_id}: snapshot is not valid JSON ({exc}).") from exc
        if not isinstance(document, dict):
            raise SnapshotIncompatible(
                f"Run {run_id}: snapshot root must be a JSON object, found {type(document).__name__}.")
        found_version = document.get("schema_version", self.SCHEMA_VERSION)
        if found_version != self.SCHEMA_VERSION:
            raise SnapshotIncompatible(
                f"Run {run_id}: snapshot schema_version={found_version!r} but this build expects "
                f"{self.SCHEMA_VERSION}. Migrate the operational store with a compatible Caveman "
                "version, or restore the run from a supported backup, before loading it.")
        dropped: list[str] = []
        for _ in range(1000):
            try:
                run = Run.model_validate(document)
                break
            except ValidationError as exc:
                extras = [error["loc"] for error in exc.errors()
                          if error["type"] == "extra_forbidden"]
                if not extras:
                    problems = "; ".join(
                        f"{_format_location(error['loc'])}: {error['msg']}" for error in exc.errors())
                    raise SnapshotIncompatible(
                        f"Run {run_id}: snapshot failed validation and cannot be silently repaired: "
                        f"{problems}") from exc
                for loc in extras:
                    _drop_path(document, loc)
                    dropped.append(_format_location(loc))
        else:
            raise SnapshotIncompatible(
                f"Run {run_id}: snapshot still failed validation after pruning unknown fields.")
        if dropped:
            logger.warning(
                "Run %s: dropped %d unknown snapshot field(s) written by a newer code shape: %s",
                run_id, len(dropped), ", ".join(sorted(dropped)))
        return run

    def load(self, run_id: str) -> Run:
        with self._lock:
            row = self.connection.execute("SELECT version,snapshot FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(run_id)
            run = self._load_snapshot(run_id, row[1])
            if run.id != run_id:
                raise SnapshotIncompatible(
                    f"Run {run_id}: snapshot identity mismatch (embedded run id {run.id!r}).")
            if run.version != row[0]:
                raise SnapshotIncompatible(
                    f"Run {run_id}: snapshot version {run.version} does not match stored version {row[0]}.")
            event_rows = self.connection.execute(
                "SELECT sequence,payload FROM events WHERE run_id=? ORDER BY sequence", (run_id,)).fetchall()
            count = len(event_rows)
            cursor = event_rows[-1][0] if event_rows else 0
            if count != cursor or cursor != run.event_cursor:
                raise ValueError("Snapshot/event cursor mismatch")
            for expected, (sequence, payload) in enumerate(event_rows, 1):
                event = Event.model_validate_json(payload)
                if sequence != expected or event.sequence != sequence or event.run_id != run_id:
                    raise ValueError("Corrupt event payload identity or sequence")
            return run

    def list_runs(self) -> list[Run]:
        with self._lock:
            return [self.load(row[0]) for row in self.connection.execute("SELECT id FROM runs ORDER BY rowid")]

    def events(self, run_id: str) -> list[Event]:
        with self._lock:
            # An unknown run must not read as an existing run with no history:
            # `caveman run events <typo>` printed an empty list and exited 0.
            if self.connection.execute(
                    "SELECT 1 FROM runs WHERE id=?", (run_id,)).fetchone() is None:
                raise KeyError(run_id)
            events = []
            for expected, (sequence, payload) in enumerate(self.connection.execute(
                    "SELECT sequence,payload FROM events WHERE run_id=? ORDER BY sequence", (run_id,)), 1):
                event = Event.model_validate_json(payload)
                if sequence != expected or event.sequence != sequence or event.run_id != run_id:
                    raise ValueError("Corrupt event payload identity or sequence")
                events.append(event)
            return events

    def save(self, run: Run, events: list[Event], expected_version: int | None) -> Run:
        if not events:
            raise ValueError("Every mutation requires an event")
        candidate = run.model_copy(deep=True)
        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                row = self.connection.execute("SELECT version,snapshot FROM runs WHERE id=?", (run.id,)).fetchone()
                if (row is None and expected_version is not None) or (row is not None and row[0] != expected_version):
                    raise ConcurrentUpdate("Run changed; reload before applying this operation")
                cursor = self._load_snapshot(run.id, row[1]).event_cursor if row else 0
                candidate.version = (expected_version or 0) + 1
                candidate.updated_at = now()
                candidate.event_cursor = cursor + len(events)
                payload = candidate.model_dump_json()
                if row:
                    self.connection.execute("UPDATE runs SET version=?,snapshot=? WHERE id=?", (candidate.version,payload,run.id))
                else:
                    self.connection.execute("INSERT INTO runs VALUES(?,?,?)", (run.id,candidate.version,payload))
                for index, event in enumerate(events, cursor+1):
                    if event.run_id != run.id:
                        raise ValueError("Event belongs to another run")
                    event = event.model_copy(update={"sequence": index})
                    self.connection.execute("INSERT INTO events VALUES(?,?,?)", (run.id,index,event.model_dump_json()))
                self.connection.execute("COMMIT")
            except BaseException:
                self.connection.execute("ROLLBACK")
                raise
            return candidate
