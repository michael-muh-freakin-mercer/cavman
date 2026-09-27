import json
import sqlite3
import threading
import pytest
from caveman.contracts import TaskPacket, WorkerResult
from caveman.models import (ApprovalStatus, BlockerReason, Event, ModelUsageRecord, TaskNode,
    TaskStatus)
from caveman.orchestration import GateError, Orchestrator
from caveman.store import ConcurrentUpdate, SQLiteStore, SnapshotIncompatible


def test_store_is_safe_across_threads(tmp_path):
    path = tmp_path / "threaded.db"
    store = SQLiteStore(path)
    run = Orchestrator(store).create_run("objective", ["criterion"])
    errors = []
    result = {}

    def worker():
        try:
            loaded = store.load(run.id)
            loaded.objective = "updated off-thread"
            result["saved"] = store.save(
                loaded, [Event(run_id=run.id, kind="test.threaded")], loaded.version)
            result["loaded"] = store.load(run.id)
            result["events"] = store.events(run.id)
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()
    assert not errors, errors
    assert result["saved"].version == run.version + 1
    assert result["loaded"].objective == "updated off-thread"
    assert result["events"][-1].kind == "test.threaded"
    assert [event.sequence for event in result["events"]] == list(
        range(1, result["loaded"].event_cursor + 1))
    store.close()


def test_durable_reload_and_events(tmp_path):
    path = tmp_path / "state.db"
    store = SQLiteStore(path)
    core = Orchestrator(store)
    run = core.create_run("objective", ["criterion"])
    core.register_input(run.id, "source", "reference")
    before = core.get_run(run.id)
    events = store.events(run.id)
    store.close()
    other = SQLiteStore(path)
    assert other.load(run.id) == before
    assert other.events(run.id) == events
    assert [e.sequence for e in events] == list(range(1,before.event_cursor+1))
    other.close()


def test_usage_records_persist_through_run_snapshot_round_trip(tmp_path):
    path = tmp_path / "usage.db"
    store = SQLiteStore(path)
    core = Orchestrator(store)
    run = core.create_run("objective", ["criterion"])
    record = ModelUsageRecord(
        run_id=run.id,
        task_id="task-1",
        assignment_id="assignment-1",
        worker_id="worker-1",
        provider="openrouter",
        model="deepseek/deepseek-v4.1-flash",
        role="worker",
        input_tokens=1024,
        output_tokens=512,
        total_tokens=1536,
        raw_usage={"prompt_tokens": 1024, "completion_tokens": 512, "total_tokens": 1536},
    )
    core.record_usage(run.id, record=record)
    reloaded = core.get_run(run.id)
    assert len(reloaded.usage_records) == 1
    assert reloaded.usage_records[0] == record
    saved = store.load(run.id)
    assert saved.usage_records[0] == record
    store.close()


def test_usage_records_explicitly_mark_unknown_usage(tmp_path):
    path = tmp_path / "unknown-usage.db"
    store = SQLiteStore(path)
    core = Orchestrator(store)
    run = core.create_run("objective", ["criterion"])
    core.record_usage(
        run.id,
        provider="openrouter",
        model="deepseek/deepseek-v4.1-flash",
        role="manager",
        task_id="task-1",
        raw_usage={},
    )
    reloaded = core.get_run(run.id)
    assert len(reloaded.usage_records) == 1
    usage = reloaded.usage_records[0]
    assert usage.usage_known is False
    assert usage.unknown_reason == "Provider did not return token usage data"
    assert usage.input_tokens is None
    assert usage.output_tokens is None
    assert usage.total_tokens is None
    store.close()


def test_final_usage_persists_after_completion_without_reopening_run(tmp_path):
    path = tmp_path / "completed-usage.db"
    store = SQLiteStore(path)
    core = Orchestrator(store)
    run = core.create_run("objective", ["criterion"])
    task = TaskNode(packet=TaskPacket(
        task_id="task-1", role="writer", objective="Write a report",
        deliverable="report", acceptance_criteria=["criterion"], stop_condition="deliver",
    ))
    core.add_tasks(run.id, [task])
    assignment = core.delegate(run.id, task.id, "worker-1")
    core.start(run.id, task.id)
    artifact = core.submit(run.id, task.id, assignment.id, "worker-1", WorkerResult(
        task_id=task.id, status="completed", summary="done", deliverable="report",
    ))
    core.review(run.id, artifact.id, "reviewer-1", True, "Criterion verified")
    core.accept(run.id, task.id, reason="Review passed")
    completed = core.complete(run.id, "Final report", criterion_evidence={"criterion": [artifact.id]})
    completed_events = store.events(run.id)

    record = core.record_usage(run.id, provider="openrouter", model="test-model",
                               role="manager", raw_usage={"input_tokens": 3, "output_tokens": 2})
    with pytest.raises(GateError, match="Run is terminal"):
        core.register_input(run.id, "late-source", "must not be registered")
    store.close()

    reopened = SQLiteStore(path)
    saved = reopened.load(run.id)
    assert saved.usage_records == [record]
    assert saved.model_dump(exclude={"usage_records", "version", "event_cursor", "updated_at"}) == completed.model_dump(
        exclude={"usage_records", "version", "event_cursor", "updated_at"})
    assert saved.version == completed.version + 1
    assert saved.event_cursor == completed.event_cursor + 1
    events = reopened.events(run.id)
    assert events[:-1] == completed_events
    assert events[-1].kind == "model.usage.recorded"
    assert events[-1].data["usage"] == record.model_dump(mode="json")
    reopened.close()


def test_optimistic_lock_and_atomic_rollback(tmp_path):
    path = tmp_path / "state.db"
    first, second = SQLiteStore(path), SQLiteStore(path)
    run = Orchestrator(first).create_run("objective", ["criterion"])
    stale = second.load(run.id)
    Orchestrator(first).register_input(run.id, "source", "ref")
    with pytest.raises(ConcurrentUpdate):
        second.save(stale, [Event(run_id=run.id, kind="test")], stale.version)
    fresh = first.load(run.id)
    fresh.objective = "not committed"
    with pytest.raises(ValueError):
        first.save(fresh, [Event(run_id=run.id, kind="test"), Event(run_id="wrong", kind="test")], fresh.version)
    assert first.load(run.id).objective == "objective"
    assert len(first.events(run.id)) == 2
    first.close()
    second.close()


def test_unknown_snapshot_fields_from_newer_code_shape_are_pruned(tmp_path, caplog):
    path = tmp_path / "drift.db"
    store = SQLiteStore(path)
    core = Orchestrator(store)
    run = core.create_run("objective", ["criterion"])
    core.add_tasks(run.id, [_task("readiness-candidate")])
    before = store.load(run.id)
    row = store.connection.execute("SELECT snapshot FROM runs WHERE id=?", (run.id,)).fetchone()
    document = json.loads(row[0])
    document["max_concurrent_specialists"] = 4
    document["specialist_timeout_seconds"] = 300.0
    document["reviews_in_flight"] = {}
    document["tasks"]["readiness-candidate"]["review_attempts"] = 1
    document["tasks"]["readiness-candidate"]["max_review_attempts"] = 3
    store.connection.execute("UPDATE runs SET snapshot=? WHERE id=?",
        (json.dumps(document, separators=(",", ":"), sort_keys=True), run.id))
    with caplog.at_level("WARNING", logger="caveman.store"):
        loaded = store.load(run.id)
    assert loaded == before
    assert loaded.objective == "objective"
    assert loaded.tasks["readiness-candidate"].packet.task_id == "readiness-candidate"
    warning = caplog.text
    for field in ("max_concurrent_specialists", "specialist_timeout_seconds", "reviews_in_flight",
            "tasks.readiness-candidate.review_attempts", "tasks.readiness-candidate.max_review_attempts"):
        assert field in warning
    store.close()


def test_drifted_run_can_be_mutated_and_resaved_with_pruned_fields(tmp_path):
    path = tmp_path / "drifted-save.db"
    store = SQLiteStore(path)
    core = Orchestrator(store)
    run = core.create_run("objective", ["criterion"])
    core.add_tasks(run.id, [_task("drifted-task")])
    current = store.load(run.id)
    row = store.connection.execute("SELECT snapshot FROM runs WHERE id=?", (run.id,)).fetchone()
    document = json.loads(row[0])
    document["max_concurrent_specialists"] = 4
    document["tasks"]["drifted-task"]["review_attempts"] = 1
    store.connection.execute("UPDATE runs SET snapshot=? WHERE id=?",
        (json.dumps(document, separators=(",", ":"), sort_keys=True), run.id))

    updated = store.save(current, [Event(run_id=run.id, kind="test")], current.version)

    snapshot = json.loads(store.connection.execute(
        "SELECT snapshot FROM runs WHERE id=?", (run.id,)).fetchone()[0])
    assert "max_concurrent_specialists" not in snapshot
    assert "review_attempts" not in snapshot["tasks"]["drifted-task"]
    reloaded = store.load(run.id)
    assert updated.version == current.version + 1
    assert reloaded.event_cursor == current.event_cursor + 1
    assert reloaded.tasks["drifted-task"].packet.task_id == "drifted-task"
    store.close()


def test_snapshot_with_wrong_schema_version_is_actionable(tmp_path):
    path = tmp_path / "version.db"
    store = SQLiteStore(path)
    run = Orchestrator(store).create_run("objective", ["criterion"])
    row = store.connection.execute("SELECT snapshot FROM runs WHERE id=?", (run.id,)).fetchone()
    document = json.loads(row[0])
    document["schema_version"] = 99
    store.connection.execute("UPDATE runs SET snapshot=? WHERE id=?",
        (json.dumps(document, separators=(",", ":"), sort_keys=True), run.id))
    with pytest.raises(SnapshotIncompatible) as excinfo:
        store.load(run.id)
    message = str(excinfo.value)
    assert isinstance(excinfo.value, ValueError)
    assert run.id in message
    assert "99" in message
    assert str(store.SCHEMA_VERSION) in message
    assert "migrate" in message.lower()
    store.close()


def test_genuinely_corrupt_snapshot_is_not_silently_accepted(tmp_path):
    path = tmp_path / "corrupt.db"
    store = SQLiteStore(path)
    run = Orchestrator(store).create_run("objective", ["criterion"])
    row = store.connection.execute("SELECT snapshot FROM runs WHERE id=?", (run.id,)).fetchone()
    document = json.loads(row[0])
    del document["plan"]
    store.connection.execute("UPDATE runs SET snapshot=? WHERE id=?",
        (json.dumps(document, separators=(",", ":"), sort_keys=True), run.id))
    with pytest.raises(SnapshotIncompatible) as excinfo:
        store.load(run.id)
    assert run.id in str(excinfo.value)
    assert "plan" in str(excinfo.value)
    store.close()


def test_future_schema_and_event_corruption_rejected(tmp_path):
    path = tmp_path / "state.db"
    store = SQLiteStore(path)
    run = Orchestrator(store).create_run("objective", ["criterion"])
    store.connection.execute("DELETE FROM events")
    with pytest.raises(ValueError, match="cursor"):
        store.load(run.id)
    store.close()
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA user_version=999")
    connection.close()
    with pytest.raises(ValueError, match="schema"):
        SQLiteStore(path)


@pytest.mark.parametrize("field,value", [("run_id", "wrong-run"), ("sequence", 999)])
def test_event_payload_identity_corruption_rejected(tmp_path, field, value):
    path = tmp_path / "state.db"
    store = SQLiteStore(path)
    run = Orchestrator(store).create_run("objective", ["criterion"])
    sequence, payload = store.connection.execute(
        "SELECT sequence,payload FROM events WHERE run_id=?", (run.id,)).fetchone()
    document = json.loads(payload)
    document[field] = value
    store.connection.execute("UPDATE events SET payload=? WHERE run_id=? AND sequence=?",
        (json.dumps(document), run.id, sequence))
    with pytest.raises(ValueError, match="payload identity"):
        store.load(run.id)
    with pytest.raises(ValueError, match="payload identity"):
        store.events(run.id)
    store.close()


def test_inspection_does_not_mutate_persisted_state():
    store = SQLiteStore()
    core = Orchestrator(store)
    run = core.create_run("objective", ["criterion"])
    copy = core.inspect(run.id)
    copy.objective = "unaudited change"
    assert core.inspect(run.id).objective == "objective"
    store.close()


def test_prior_v1_approval_snapshot_migrates_transactionally_and_reloads(tmp_path):
    path = tmp_path / "state.db"
    store = SQLiteStore(path)
    core = Orchestrator(store)
    run = core.create_run("objective", ["criterion"])
    request = core.request_approval(run.id, "promote", {"artifact_id": "artifact-v1"}, "Legacy reason")
    core.decide_approval(run.id, request.id, True, "human", "Legacy approval")
    events_before = [event.model_dump(mode="json") for event in store.events(run.id)]

    row = store.connection.execute("SELECT snapshot FROM runs WHERE id=?", (run.id,)).fetchone()
    prior = json.loads(row[0])
    prior["schema_version"] = 1
    for task_state in prior["tasks"].values():
        task_state.pop("approval_gates", None)
    for approval in prior["approvals"].values():
        rationale = approval["rationale"]
        for field in ["run_id", "category", "target", "rationale", "risk", "artifact_refs",
                "requested_capability", "required", "status", "updated_at", "decided_at",
                "superseded_at", "replacement_id"]:
            approval.pop(field, None)
        approval["reason"] = rationale
    for decision in prior["approval_decisions"].values():
        decision.pop("status", None)
    prior_payload = json.dumps(prior, separators=(",", ":"), sort_keys=True)
    store.connection.execute("UPDATE runs SET snapshot=? WHERE id=?", (prior_payload, run.id))
    store.connection.execute("PRAGMA user_version=1")
    store.close()

    migrated_store = SQLiteStore(path)
    migrated = migrated_store.load(run.id)
    migrated_request = migrated.approvals[request.id]
    assert migrated.schema_version == SQLiteStore.SCHEMA_VERSION == 2
    assert migrated_request.run_id == run.id
    assert migrated_request.rationale == "Legacy reason"
    assert migrated_request.status == "approved"
    assert migrated.approval_decisions[request.id].status == "approved"
    assert [event.model_dump(mode="json") for event in migrated_store.events(run.id)] == events_before
    backup = migrated_store.connection.execute(
        "SELECT snapshot FROM schema_migration_backups WHERE run_id=? AND from_version=1 AND to_version=2",
        (run.id,)).fetchone()
    assert backup == (prior_payload,)
    migrated_store.close()

    reloaded_store = SQLiteStore(path)
    assert reloaded_store.load(run.id) == migrated
    assert reloaded_store.connection.execute("PRAGMA user_version").fetchone()[0] == 2
    reloaded_store.close()


def _task(task_id):
    return TaskNode(packet=TaskPacket(task_id=task_id, role="writer", objective="Write",
        deliverable="report", acceptance_criteria=["accurate"], stop_condition="deliver"))


def _downgrade_approval_snapshot(store, run_id, *, missing_task_id=None):
    row = store.connection.execute("SELECT snapshot FROM runs WHERE id=?", (run_id,)).fetchone()
    prior = json.loads(row[0])
    prior["schema_version"] = 1
    for task_id, task_state in prior["tasks"].items():
        task_state["approval_ids"] = [gate["request_id"] for gate in task_state.pop("approval_gates", [])]
        if task_id == missing_task_id:
            task_state["approval_ids"] = ["missing-legacy-request"]
    for approval in prior["approvals"].values():
        rationale = approval["rationale"]
        for field in ["run_id", "category", "target", "rationale", "risk", "artifact_refs",
                "requested_capability", "required", "status", "updated_at", "decided_at",
                "superseded_at", "replacement_id"]:
            approval.pop(field, None)
        approval["reason"] = rationale
    for decision in prior["approval_decisions"].values():
        decision.pop("status", None)
    payload = json.dumps(prior, separators=(",", ":"), sort_keys=True)
    store.connection.execute("UPDATE runs SET snapshot=? WHERE id=?", (payload, run_id))
    store.connection.execute("PRAGMA user_version=1")
    return payload


def test_v1_task_approval_ids_migrate_to_exact_typed_gates_for_all_decisions(tmp_path):
    path = tmp_path / "gates.db"
    store = SQLiteStore(path)
    core = Orchestrator(store)
    rid = core.create_run("objective", ["criterion"]).id
    core.add_tasks(rid, [_task("pending"), _task("approved"), _task("rejected")])
    requests = {}
    for task_id in ("pending", "approved", "rejected"):
        scope = {"task_id": task_id, "operation": "publish"}
        requests[task_id] = core.request_approval(rid, "publish", scope, f"Gate {task_id}")
        core.gate_task(rid, task_id, requests[task_id].id, "publish", scope)
    core.decide_approval(rid, requests["approved"].id, True, "human", "Approved")
    core.decide_approval(rid, requests["rejected"].id, False, "human", "Rejected")
    events_before = [event.model_dump(mode="json") for event in store.events(rid)]
    _downgrade_approval_snapshot(store, rid)
    store.close()

    migrated_store = SQLiteStore(path)
    migrated = migrated_store.load(rid)
    for task_id, expected in (("pending", ApprovalStatus.PENDING),
            ("approved", ApprovalStatus.APPROVED), ("rejected", ApprovalStatus.REJECTED)):
        node = migrated.tasks[task_id]
        request = migrated.approvals[requests[task_id].id]
        assert node.approval_ids == []
        assert len(node.approval_gates) == 1
        assert node.approval_gates[0].request_id == request.id
        assert node.approval_gates[0].action == request.action
        assert node.approval_gates[0].scope_json == request.scope_json
        assert node.approval_gates[0].scope_digest == request.scope_digest
        assert request.status == expected
    assert migrated.tasks["approved"].status == TaskStatus.READY
    assert migrated.tasks["pending"].status == TaskStatus.BLOCKED
    assert migrated.tasks["rejected"].status == TaskStatus.BLOCKED
    assert [event.model_dump(mode="json") for event in migrated_store.events(rid)] == events_before
    Orchestrator(migrated_store).delegate(rid, "approved", "worker")
    with pytest.raises(GateError):
        Orchestrator(migrated_store).delegate(rid, "pending", "worker")
    migrated_store.close()


def test_missing_v1_approval_is_explicitly_blocked_and_manager_can_regate(tmp_path):
    path = tmp_path / "missing-gate.db"
    store = SQLiteStore(path)
    core = Orchestrator(store)
    rid = core.create_run("objective", ["criterion"]).id
    core.add_tasks(rid, [_task("legacy")])
    original_events = [event.model_dump(mode="json") for event in store.events(rid)]
    source = _downgrade_approval_snapshot(store, rid, missing_task_id="legacy")
    store.close()

    migrated_store = SQLiteStore(path)
    core = Orchestrator(migrated_store)
    migrated = core.get_run(rid)
    node = migrated.tasks["legacy"]
    assert node.status == TaskStatus.BLOCKED
    assert node.approval_ids == ["missing-legacy-request"]
    assert node.blocker.startswith(BlockerReason.LEGACY_APPROVAL_REGATE)
    with pytest.raises(GateError):
        core.delegate(rid, "legacy", "worker")
    assert [event.model_dump(mode="json") for event in migrated_store.events(rid)] == original_events
    backup = migrated_store.connection.execute(
        "SELECT snapshot FROM schema_migration_backups WHERE run_id=? AND from_version=1 AND to_version=2",
        (rid,)).fetchone()
    assert backup == (source,)

    scope = {"task_id": "legacy", "operation": "publish"}
    replacement = core.request_approval(rid, "publish", scope, "Recovered exact scope")
    core.decide_approval(rid, replacement.id, True, "human", "Approved recovered scope")
    current = core.request_approval(rid, "publish", scope, "Current recovered scope",
        supersedes=replacement.id)
    with pytest.raises(GateError, match="exact approval request"):
        core.recover_legacy_approval_gate(rid, "legacy", "missing-legacy-request",
            replacement.id, "publish", scope)
    assert core.get_run(rid).tasks["legacy"].approval_ids == ["missing-legacy-request"]
    with pytest.raises(GateError, match="exact approval request"):
        core.recover_legacy_approval_gate(rid, "legacy", "missing-legacy-request",
            current.id, "publish", {**scope, "operation": "different"})
    core.decide_approval(rid, current.id, True, "human", "Approved current recovered scope")
    core.recover_legacy_approval_gate(rid, "legacy", "missing-legacy-request",
        current.id, "publish", scope)
    recovered = core.reload(rid)
    assert recovered.tasks["legacy"].approval_ids == []
    assert recovered.tasks["legacy"].status == TaskStatus.READY
    assert recovered.tasks["legacy"].approval_gates[0].request_id == current.id
    core.delegate(rid, "legacy", "worker")
    migrated_store.close()

    reloaded = SQLiteStore(path)
    assert reloaded.load(rid).tasks["legacy"].status == TaskStatus.DELEGATED
    assert reloaded.connection.execute("PRAGMA user_version").fetchone()[0] == 2
    reloaded.close()


def test_concurrent_tool_dispatch_does_not_lose_durable_mutations(tmp_path):
    """The SDK dispatches several tool calls from one model turn concurrently.

    Async tools share the event loop while sync tools run on worker threads, so
    every durable read-modify-write is contended. Neither the operational
    mutations nor the append-only usage accounting may be dropped.
    """
    store = SQLiteStore(tmp_path / "contended.db")
    core = Orchestrator(store)
    rid = core.create_run("objective", ["criterion"]).id
    core.add_tasks(rid, [TaskNode(packet=TaskPacket(
        task_id=f"t{index}", role="writer", objective="write", deliverable="report",
        acceptance_criteria=["accurate"], stop_condition="deliver")) for index in range(8)])

    errors = []
    barrier = threading.Barrier(9)

    def delegator(index):
        barrier.wait()
        try:
            core.delegate(rid, f"t{index}", f"worker-{index}")
            core.start(rid, f"t{index}")
        except BaseException as exc:  # pragma: no cover - failure detail
            errors.append(("delegate", exc))

    def accountant():
        barrier.wait()
        for _ in range(16):
            try:
                core.record_usage(rid, raw_usage={"prompt_tokens": 7, "completion_tokens": 3})
            except BaseException as exc:  # pragma: no cover - failure detail
                errors.append(("usage", exc))

    threads = [threading.Thread(target=delegator, args=(index,)) for index in range(8)]
    threads.append(threading.Thread(target=accountant))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors, errors
    run = store.load(rid)
    assert all(task.status == TaskStatus.RUNNING for task in run.tasks.values())
    assert len(run.usage_records) == 16
    assert sum(record.total_tokens or 0 for record in run.usage_records) == 16 * 10
    # The snapshot and the append-only event log must agree after contention.
    assert [event.sequence for event in store.events(rid)] == list(
        range(1, run.event_cursor + 1))
    store.close()


def test_usage_accounting_survives_a_lost_version_race(tmp_path):
    """A billed provider call must not be discarded by an out-of-process commit."""
    store = SQLiteStore(tmp_path / "raced.db")
    core = Orchestrator(store)
    rid = core.create_run("objective", ["criterion"]).id

    original_save = store.save
    interfered = []

    def save(run, events, expected_version):
        if not interfered and events and events[0].kind == "model.usage.recorded":
            interfered.append(True)
            # Simulate another process committing between this load and save.
            other = original_save(
                store.load(rid), [Event(run_id=rid, kind="other.process")],
                store.load(rid).version)
            assert other.version == expected_version + 1
        return original_save(run, events, expected_version)

    store.save = save
    record = core.record_usage(rid, raw_usage={"prompt_tokens": 5, "completion_tokens": 2})
    store.save = original_save
    run = store.load(rid)
    assert interfered == [True]
    assert [item.id for item in run.usage_records] == [record.id]
    assert run.usage_records[0].total_tokens == 7
    store.close()


def test_events_for_an_unknown_run_is_not_an_empty_history(tmp_path):
    store = SQLiteStore(tmp_path / "missing.db")
    run = Orchestrator(store).create_run("objective", ["criterion"])
    assert store.events(run.id)
    with pytest.raises(KeyError):
        store.events("not-a-run")
    store.close()
