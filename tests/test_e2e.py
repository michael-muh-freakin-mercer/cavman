"""Offline end-to-end proof of the durable Manager loop through finish_run."""
import asyncio
import json

import pytest
from agents import Runner, set_tracing_disabled
from agents.items import ToolCallOutputItem

from fakes import message_step, responder_step, scripted_model, tool_step
from caveman import runtime
from caveman.adapter import INITIAL_COMPLETION_CRITERION, DurableController
from caveman.contracts import TaskPacket
from caveman.models import CapabilityProfile, FailureClass, TaskNode, TaskStatus
from caveman.orchestration import Orchestrator
from caveman.store import SQLiteStore

CRITERION = "Demo artifact accepted with trusted validation and independent review evidence"

PACKET = {
    "task_id": "a",
    "role": "demo specialist",
    "objective": "Produce the demo deliverable",
    "deliverable": "A short demo deliverable document",
    "acceptance_criteria": ["Deliverable states the demo outcome"],
    "stop_condition": "Deliverable returned or genuinely blocked",
}

WORKER_RESULT = {
    "task_id": "a",
    "status": "completed",
    "summary": "Demo deliverable produced",
    "deliverable": "Demo deliverable: objective met.",
    "evidence": ["Deliverable drafted from the task packet"],
    "acceptance_check": [{
        "criterion": "Deliverable states the demo outcome",
        "passed": True,
        "note": "Stated in the deliverable",
    }],
}

REVIEW_RESULT = {
    "passed": True,
    "evidence": ["Deliverable matches the packet objective"],
    "reason": "Evidence satisfies the acceptance criteria",
}


def _offline_config():
    return runtime.RuntimeConfig(
        provider="openrouter", api_key="offline-fixture",
        base_url="https://openrouter.ai/api/v1",
        manager_model="fake-manager", worker_model="fake-worker", budget=None)


def test_durable_manager_loop_completes_offline(tmp_path, monkeypatch):
    set_tracing_disabled(True)
    store = SQLiteStore(tmp_path / "operations.db")
    core = Orchestrator(store)
    run = core.create_run("Offline demo objective", [INITIAL_COMPLETION_CRITERION])
    controller = DurableController(core, run.id)

    def finish_step(call):
        # Computed at call time: the artifact ID only exists after acceptance.
        state = controller.inspect()
        artifact_id = state.tasks["a"].artifact_ids[-1]
        return tool_step("finish_run", {
            "summary": "Demo run completed with an accepted artifact.",
            "criterion_evidence_json": json.dumps({CRITERION: [artifact_id]}),
        }, call_id="call-finish")

    manager = scripted_model([
        tool_step("set_completion_criteria", {"criteria": [CRITERION]}, call_id="call-criteria"),
        tool_step("plan_tasks", {"packets": [PACKET], "capabilities": ["model_only"],
                                 "checks": [["result_schema"]]}, call_id="call-plan"),
        tool_step("delegate_task", {"task_id": "a"}, call_id="call-delegate"),
        tool_step("validate_task", {"task_id": "a"}, call_id="call-validate"),
        tool_step("review_task", {"task_id": "a"}, call_id="call-review"),
        tool_step("accept_task", {"task_id": "a",
                                  "reason": "Trusted validation and independent review passed"},
                  call_id="call-accept"),
        responder_step(finish_step),
        message_step("Run completed; demo artifact accepted."),
    ])
    worker = scripted_model([
        message_step(json.dumps(WORKER_RESULT)),
        message_step(json.dumps(REVIEW_RESULT)),
    ])

    monkeypatch.setattr(runtime.RuntimeConfig, "from_env",
                        classmethod(lambda cls: _offline_config()))
    monkeypatch.setattr(runtime, "build_models", lambda config: (manager, worker))

    agent = runtime.build_caveman(controller)
    result = asyncio.run(Runner.run(agent, input="Run the offline demo", max_turns=20))

    assert result.final_output == "Run completed; demo artifact accepted."
    final = controller.inspect()
    assert final.status == "completed"
    task = final.tasks["a"]
    assert task.status == "ACCEPTED"
    artifact = final.artifacts[task.artifact_ids[-1]]
    assert artifact.status == "accepted"

    kinds = [event.kind for event in store.events(run.id)]
    for expected in ("run.created", "plan.criteria_defined", "task.created",
                     "assignment.created", "assignment.started", "artifact.submitted",
                     "artifact.validation_completed", "artifact.reviewed",
                     "artifact.accepted", "run.completed"):
        assert expected in kinds, expected

    assert final.usage_records, "durable usage accounting persisted no records"
    assert all(record.usage_known for record in final.usage_records)
    assert {record.role for record in final.usage_records} == {"manager", "worker", "reviewer"}
    assert all(record.input_tokens == 11 and record.output_tokens == 7
               for record in final.usage_records)

    manager.assert_complete()
    worker.assert_complete()
    controller.close()


def test_worker_provider_failure_preserves_durable_consistency(tmp_path, monkeypatch):
    set_tracing_disabled(True)
    store = SQLiteStore(tmp_path / "operations.db")
    core = Orchestrator(store)
    run = core.create_run("Offline demo objective", [INITIAL_COMPLETION_CRITERION])
    controller = DurableController(core, run.id)
    controller.set_criteria([CRITERION])
    core.add_tasks(run.id, [TaskNode(
        packet=TaskPacket(**PACKET),
        capability=CapabilityProfile.MODEL_ONLY,
        required_checks=["result_schema"],
        review_required=True,
    )])

    failing_worker = scripted_model([RuntimeError("provider exploded mid-request")])
    monkeypatch.setattr(runtime.RuntimeConfig, "from_env",
                        classmethod(lambda cls: _offline_config()))
    monkeypatch.setattr(runtime, "build_models",
                        lambda config: (scripted_model([]), failing_worker))

    with pytest.raises(Exception):
        asyncio.run(controller.delegate("a"))

    final = controller.inspect()
    task = final.tasks["a"]
    assert task.status == TaskStatus.FAILED
    assert final.failures, "no failure recorded for the provider error"
    failure = final.failures[-1]
    assert failure.classification == FailureClass.TOOL_FAILURE
    assert "provider exploded mid-request" in failure.evidence

    kinds = [event.kind for event in store.events(run.id)]
    assert "assignment.failed" in kinds
    assert "failure.classified" in kinds
    # The failed task stays gated until explicit recovery.
    with pytest.raises(ValueError, match="not eligible"):
        asyncio.run(controller.delegate("a"))
    controller.close()


def test_kernel_refuses_out_of_order_manager_calls_and_the_run_still_completes(tmp_path, monkeypatch):
    """The central design claim, exercised through the real SDK tool boundary.

    The model proposes and the kernel authorizes. Nothing tested that end to
    end: a scripted Manager that calls tools out of order must be refused by
    the kernel, must learn why from the tool result, must not corrupt durable
    state, and must still be able to complete the run afterwards. Weak workers
    and weak Managers do exactly this in real runs.
    """
    set_tracing_disabled(True)
    store = SQLiteStore(tmp_path / "operations.db")
    core = Orchestrator(store)
    run = core.create_run("Offline gate-order objective", [INITIAL_COMPLETION_CRITERION])
    controller = DurableController(core, run.id)

    def finish_step(call):
        state = controller.inspect()
        return tool_step("finish_run", {
            "summary": "Demo run completed after the kernel refused every premature call.",
            "criterion_evidence_json": json.dumps(
                {CRITERION: [state.tasks["a"].artifact_ids[-1]]}),
        }, call_id="call-finish-ok")

    manager = scripted_model([
        tool_step("set_completion_criteria", {"criteria": [CRITERION]}, call_id="c1"),
        tool_step("plan_tasks", {"packets": [PACKET], "capabilities": ["model_only"],
                                 "checks": [["result_schema"]]}, call_id="c2"),
        # Premature completion: no task has been accepted yet.
        tool_step("finish_run", {"summary": "done already",
                                 "criterion_evidence_json": json.dumps({CRITERION: []})},
                  call_id="c3"),
        tool_step("delegate_task", {"task_id": "a"}, call_id="c4"),
        # Premature acceptance: a candidate exists but carries no evidence.
        tool_step("accept_task", {"task_id": "a", "reason": "looks fine to me"},
                  call_id="c5"),
        tool_step("validate_task", {"task_id": "a"}, call_id="c6"),
        # Still premature: validated, but no independent review.
        tool_step("accept_task", {"task_id": "a", "reason": "validation passed"},
                  call_id="c7"),
        tool_step("review_task", {"task_id": "a"}, call_id="c8"),
        tool_step("accept_task", {"task_id": "a", "reason": "Validation and review passed"},
                  call_id="c9"),
        responder_step(finish_step),
        message_step("Run completed after three refusals."),
    ])
    worker = scripted_model([
        message_step(json.dumps(WORKER_RESULT)),
        message_step(json.dumps(REVIEW_RESULT)),
    ])

    monkeypatch.setattr(runtime.RuntimeConfig, "from_env",
                        classmethod(lambda cls: _offline_config()))
    monkeypatch.setattr(runtime, "build_models", lambda config: (manager, worker))

    agent = runtime.build_caveman(controller)
    result = asyncio.run(Runner.run(agent, input="Drive the gates out of order", max_turns=30))

    # Every scripted step ran, so no refusal aborted the run.
    manager.assert_complete()
    worker.assert_complete()
    assert result.final_output == "Run completed after three refusals."

    # The kernel's reason reached the model, not a generic failure.
    outputs = [str(item.output) for item in result.new_items
               if isinstance(item, ToolCallOutputItem)]
    refusals = [text for text in outputs if "An error occurred while running the tool" in text]
    assert len(refusals) == 3, outputs
    assert any("Required work remains unresolved" in text for text in refusals)
    assert any("Validation gates failed" in text and "result_schema" in text
               for text in refusals)
    assert any("Independent review required" in text for text in refusals)

    # Durable state records exactly one acceptance, and the run completed.
    final = controller.inspect()
    assert final.status == "completed"
    assert final.tasks["a"].status == "ACCEPTED"
    assert len(final.acceptances) == 1
    assert final.accepted_artifacts == [final.tasks["a"].artifact_ids[-1]]
    # A refused mutation must leave no trace beyond the model's own transcript.
    kinds = [event.kind for event in store.events(run.id)]
    assert kinds.count("artifact.accepted") == 1
    assert kinds.count("run.completed") == 1

    # Snapshot and append-only log still agree after the refusals.
    events = store.events(run.id)
    assert [event.sequence for event in events] == list(range(1, final.event_cursor + 1))
    controller.close()
