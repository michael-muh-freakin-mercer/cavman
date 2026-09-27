from copy import deepcopy
import json

import pytest

from caveman.models import Run, TaskStatus, WorkPlan
from caveman.pulse import normalize_snapshot, render_digest, summarize_run


def snapshot():
    return {"id": "run-1", "status": "active", "version": 2, "event_cursor": 4,
            "tasks": {"done": {"status": "ACCEPTED"}, "draft": {"status": "SUBMITTED"}},
            "artifacts": {"a": {"status": "accepted", "task_id": "done", "content": "SECRET"},
                          "b": {"status": "candidate", "task_id": "draft"}},
            "accepted_artifacts": ["a"], "objective": "SECRET"}


def test_accepted_is_distinct_from_submitted():
    result = summarize_run(snapshot())
    assert result["task_counts"]["ACCEPTED"] == 1
    assert result["task_counts"]["SUBMITTED"] == 1
    assert list(result["task_counts"]) == [state.value for state in TaskStatus]
    assert result["recent_accepted_artifacts"] == [{"id": "a", "task_id": "done"}]
    assert result["status"] == "active"
    assert result["has_changes"] is None


def test_blocked_failed_and_pending_requests():
    run = snapshot()
    run["tasks"].update({"blocked": {"status": "BLOCKED", "blocker": "SECRET"},
                         "failed": {"status": "FAILED"}})
    run["approvals"] = {"p": {"status": "pending", "scope_json": "SECRET"},
                        "approved": {"status": "approved"}}
    run["capability_requests"] = {"c": {"status": "pending", "task_id": "blocked", "reason": "SECRET"},
                                  "denied": {"status": "denied"}}
    result = summarize_run(run)
    assert result["blocked_tasks"] == ["blocked"]
    assert result["failed_tasks"] == ["failed"]
    assert result["pending_approvals"] == [{"id": "p"}]
    assert result["pending_capability_requests"] == [{"id": "c", "task_id": "blocked"}]
    assert "SECRET" not in json.dumps(normalize_snapshot(run))


def test_no_changes_and_no_mutation():
    run = snapshot()
    before = deepcopy(run)
    result = summarize_run(run, previous=before)
    assert result["has_changes"] is False
    assert "No changes since previous snapshot." in render_digest(result)
    assert run == before
    run["tasks"]["draft"]["status"] = "REVIEWING"
    assert summarize_run(run, previous=before)["has_changes"] is True


def test_stable_rendering_and_safe_events():
    run = snapshot()
    events = [{"run_id": "run-1", "sequence": 4, "kind": "task.accepted", "data": {"key": "SECRET"}},
              {"run_id": "run-1", "sequence": 1, "kind": "run.created"},
              {"run_id": "other", "sequence": 8, "kind": "wrong.run"}]
    result = summarize_run(run, events=events)
    assert render_digest(result) == (
        "Project Pulse: run-1 (active)\nTasks: SUBMITTED 1, ACCEPTED 1\n"
        "Pending: 0 approvals, 0 capability requests\nRecent accepted artifacts: a\n"
        "Recent events: #4 task.accepted, #1 run.created")
    run["tasks"] = dict(reversed(list(run["tasks"].items())))
    assert summarize_run(run, events=reversed(events)) == result
    assert "SECRET" not in json.dumps(result)


def test_run_model_and_json_mapping_match():
    run = Run(id="run-1", objective="SECRET", plan=WorkPlan(objective="SECRET", completion_criteria=["SECRET"]))
    before = run.model_dump(mode="json")
    assert normalize_snapshot(run) == normalize_snapshot(before)
    assert run.model_dump(mode="json") == before
    assert "Tasks: none" in render_digest(summarize_run(run))


def test_acceptance_requires_both_records_and_list_membership():
    run = snapshot()
    run["accepted_artifacts"] = ["b", "missing", "a", "a"]
    run["artifacts"]["unlisted"] = {"status": "accepted", "task_id": "done"}
    assert summarize_run(run)["accepted_artifact_count"] == 1
    assert summarize_run(run, recent_limit=0)["recent_accepted_artifacts"] == []


def test_limits_unknown_states_and_wrong_baseline():
    run = snapshot()
    run["tasks"]["future"] = {"status": "NEW_STATE"}
    assert summarize_run(run)["unknown_tasks"] == ["future"]
    with pytest.raises(ValueError, match="different run"):
        summarize_run(run, previous={**run, "id": "other"})
    for limit in (-1, True, 1.5):
        with pytest.raises(ValueError, match="nonnegative integer"):
            summarize_run(run, recent_limit=limit)
