import asyncio
import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("agents")

import fakes
from walter.adapter import (DurableController, INITIAL_COMPLETION_CRITERION,
                            ReviewResult)
from walter.contracts import TaskPacket, WorkerResult
from walter.models import (CapabilityProfile, CapabilityRequestStatus, FailureClass,
                           ReplanProposal, TaskNode)
from walter.orchestration import GateError, Orchestrator
from walter.sandbox import CommandResult, WorkspaceManager
from walter.store import SQLiteStore
from walter.adapter import workspace_tools
from walter.adapter import load_system_prompt
from walter.adapter import DURABLE_INSTRUCTIONS


def packet(task_id="task"):
    return TaskPacket(
        task_id=task_id,
        role="fixture specialist",
        objective="Produce a bounded fixture result",
        deliverable="One inspectable result",
        acceptance_criteria=["result is present"],
        stop_condition="result submitted",
    )


async def _call_tool(function_tool, **arguments):
    """Invoke a Manager tool through the real SDK FunctionTool boundary."""
    from agents.tool_context import ToolContext

    payload = json.dumps(arguments)
    context = ToolContext(None, tool_name=function_tool.name, tool_call_id="call-1",
                          tool_arguments=payload)
    return await function_tool.on_invoke_tool(context, payload)


def controller_for(task, tmp_path=None):
    core = Orchestrator(SQLiteStore())
    run = core.create_run("fixture", ["accepted fixture"])
    core.add_tasks(run.id, [task])
    return DurableController(core, run.id)


class RecordingWorkspaceManager(WorkspaceManager):
    """Exercise the real candidate identity while stubbing the isolation backend."""

    def __init__(self, repository):
        super().__init__(repository)
        self.executions = []

    def run_command(self, workspace_id, category, argv, *, worker_id=None, timeout=30):
        self.inspect_grant(workspace_id, worker_id=worker_id)
        self.executions.append((workspace_id, category, list(argv), worker_id))
        return CommandResult(0, "1 passed\n", "")


def _write_candidate(controller, task_id="task", path="test_candidate.py",
                     content="def test_candidate():\n    assert True\n"):
    """Make a stubbed worker produce a real candidate change in its own workspace.

    A `developer_sandbox` lane that returns completed without touching a file is
    refused before submission, so a fixture standing in for a working developer
    has to actually write something. Resolved at call time because a redelegated
    attempt gets a fresh workspace and worker identity. The default path is a
    root-level ``test_*.py`` so it satisfies both the ``compile`` and the
    candidate-scoped ``pytest`` checks without needing a tests directory.
    """
    workspace_id = controller.inspect().tasks[task_id].workspace_id
    grant = controller.workspaces.inspect_grant(workspace_id)
    controller.workspaces.write_file(workspace_id, path, content,
                                     worker_id=grant.worker_id)


def _git_fixture(repository):
    """Initialize the fixture repository and commit its current contents."""
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repository), "-c", "user.name=Fixture", "-c",
                    "user.email=fixture@example.invalid", "commit", "-qm", "fixture"], check=True)


def _developer_pytest_controller(tmp_path):
    repository = tmp_path / "fixture"
    repository.mkdir()
    (repository / "README.md").write_text("# Fixture\n")
    (repository / "tests").mkdir()
    _git_fixture(repository)
    core = Orchestrator(SQLiteStore())
    run = core.create_run("scoped pytest", ["A candidate test file passes"])
    core.add_tasks(run.id, [TaskNode(
        packet=packet(), capability=CapabilityProfile.DEVELOPER_SANDBOX,
        required_checks=["pytest"],
    )])
    manager = RecordingWorkspaceManager(repository)
    return DurableController(core, run.id, manager), manager


def test_no_op_developer_candidate_is_refused_before_submission(tmp_path, monkeypatch):
    """A developer lane that changed nothing never becomes a candidate.

    Trusted validation would catch it, but only after a sandbox execution and
    the Manager turns spent discovering why. Refuse it against the trusted diff
    instead, and say so precisely.
    """
    controller, manager = _developer_pytest_controller(tmp_path)

    async def claims_success_without_writing(**kwargs):
        return WorkerResult(task_id="task", status="completed",
                            summary="added the function", deliverable="trust me")

    monkeypatch.setattr(controller, "_invoke", claims_success_without_writing)
    returned = asyncio.run(controller.delegate("task"))

    assert isinstance(returned, WorkerResult)
    state = controller.inspect()
    task = state.tasks["task"]
    # No candidate exists, so nothing can be validated, reviewed or accepted.
    assert task.artifact_ids == []
    assert state.artifacts == {}
    # The lane was classified and routed to a bounded revision.
    assert task.status == "REVISION_REQUIRED"
    assert state.failures[-1].classification == FailureClass.BAD_OUTPUT
    assert "unchanged candidate workspace" in state.failures[-1].evidence
    # The worker's own claim is preserved in the evidence rather than discarded.
    assert "added the function" in state.failures[-1].evidence
    # No sandbox execution was spent on a candidate that does not exist.
    assert manager.executions == []


def test_read_only_lane_is_exempt_from_the_no_op_refusal(tmp_path, monkeypatch):
    """A repo_reader diff is empty by construction and must still submit."""
    repository = tmp_path / "fixture"
    repository.mkdir()
    (repository / "README.md").write_text("# Fixture\n")
    _git_fixture(repository)
    core = Orchestrator(SQLiteStore())
    run = core.create_run("scout", ["A written report cites repository facts"])
    core.add_tasks(run.id, [TaskNode(
        packet=packet(), capability=CapabilityProfile.REPO_READER,
        required_checks=["result_schema"],
    )])
    controller = DurableController(core, run.id, RecordingWorkspaceManager(repository))

    async def report(**kwargs):
        return WorkerResult(task_id="task", status="completed", summary="scouted",
                            deliverable="README.md declares the fixture")

    monkeypatch.setattr(controller, "_invoke", report)
    artifact = asyncio.run(controller.delegate("task"))
    assert controller.inspect().tasks["task"].status == "SUBMITTED"
    assert artifact.content


def test_autonomous_replan_needs_no_gate_and_material_one_still_does():
    """Materiality is structural, and the model's own wording cannot move it."""
    controller = controller_for(TaskNode(packet=packet(), required_checks=["result_schema"]))
    core = controller.core
    core.delegate(controller.run_id, "task", "worker")
    core.start(controller.run_id, "task")
    core.fail(controller.run_id, "task", FailureClass.TIMEOUT, "interrupted")
    core.recover(controller.run_id, controller.inspect().failures[-1].id, "retry")

    # Reopen-only against work that never reached ACCEPTED: autonomous, however
    # alarming the model's trigger and risk text happens to sound.
    reasons = core.replan_materiality(controller.run_id, ReplanProposal(
        base_revision=0, trigger="CATASTROPHIC IRREVERSIBLE REWRITE",
        evidence=["e"], reopen=["task"], risks=["destroys everything"]))
    assert reasons == []

    # Each structural change is material on its own.
    assert core.replan_materiality(controller.run_id, ReplanProposal(
        base_revision=0, trigger="t", evidence=["e"], remove=["task"])) == [
            "removes 1 task(s)"]
    assert core.replan_materiality(controller.run_id, ReplanProposal(
        base_revision=0, trigger="t", evidence=["e"])) == ["changes nothing"]
    rewire = core.replan_materiality(controller.run_id, ReplanProposal(
        base_revision=0, trigger="t", evidence=["e"], reopen=["task"],
        dependencies={"task": []}))
    assert rewire == ["rewires task dependencies"]


def test_reopening_accepted_work_stays_gated():
    """Superseding an accepted artifact is the human's call, not the model's."""
    controller = controller_for(TaskNode(packet=packet(), required_checks=["result_schema"]))
    core = controller.core
    assignment = core.delegate(controller.run_id, "task", "worker")
    core.start(controller.run_id, "task")
    core.submit(controller.run_id, "task", assignment.id, "worker",
        WorkerResult(task_id="task", status="completed", summary="s", deliverable="d"))
    artifact_id = controller.inspect().tasks["task"].artifact_ids[-1]
    core.validate(controller.run_id, artifact_id, "result_schema", True, "checked",
                  validator_id="executor")
    core.review(controller.run_id, artifact_id, "reviewer", True, "reviewed")
    core.accept(controller.run_id, "task", reason="gates passed")
    assert controller.inspect().tasks["task"].status == "ACCEPTED"

    reasons = core.replan_materiality(controller.run_id, ReplanProposal(
        base_revision=0, trigger="t", evidence=["e"], reopen=["task"]))
    assert reasons == ["would supersede accepted work: task"]
    proposal, approval = controller.propose_replan(
        trigger="redo accepted work", evidence=["new constraint"],
        add=[], remove=[], reopen=["task"])
    assert approval is not None and proposal.requires_approval is True
    with pytest.raises(GateError, match="Exact scoped human approval required"):
        controller.apply_replan(proposal.id)


def test_autonomous_replan_that_becomes_material_is_refused_at_apply():
    """The assessment is re-derived at apply time, not trusted from the flag."""
    upstream = TaskNode(packet=packet("upstream"), required_checks=["result_schema"])
    controller = controller_for(upstream)
    core = controller.core
    core.delegate(controller.run_id, "upstream", "worker")
    core.start(controller.run_id, "upstream")
    core.fail(controller.run_id, "upstream", FailureClass.TIMEOUT, "interrupted")
    core.recover(controller.run_id, controller.inspect().failures[-1].id, "retry")

    proposal, approval = controller.propose_replan(
        trigger="reopen the failed lane", evidence=["transient provider fault"],
        add=[], remove=[], reopen=["upstream"])
    assert approval is None

    # The lane succeeds before the Manager gets around to applying the proposal,
    # so applying it would now supersede accepted work.
    assignment = core.delegate(controller.run_id, "upstream", "worker-2")
    core.start(controller.run_id, "upstream")
    core.submit(controller.run_id, "upstream", assignment.id, "worker-2",
        WorkerResult(task_id="upstream", status="completed", summary="s", deliverable="d"))
    artifact_id = controller.inspect().tasks["upstream"].artifact_ids[-1]
    core.validate(controller.run_id, artifact_id, "result_schema", True, "checked",
                  validator_id="executor")
    core.review(controller.run_id, artifact_id, "reviewer", True, "reviewed")
    core.accept(controller.run_id, "upstream", reason="gates passed")

    with pytest.raises(ValueError, match="no longer autonomous"):
        controller.apply_replan(proposal.id)
    assert controller.inspect().plan.revision == 0


def test_unknown_replan_proposal_names_the_known_ones():
    controller = controller_for(TaskNode(packet=packet(), required_checks=["result_schema"]))
    with pytest.raises(ValueError, match="Unknown replan proposal nope"):
        controller.apply_replan("nope")


def test_manager_tool_surface_has_no_trust_forging_tools():
    controller = controller_for(TaskNode(packet=packet(), required_checks=["result_schema"]))
    tools = {item.name: item for item in controller.tools()}
    assert set(tools) == {
        "inspect_run", "inspect_task", "inspect_artifact",
        "set_completion_criteria", "register_repository_inputs",
        "plan_tasks", "delegate_task",
        "validate_task", "review_task", "accept_task", "recover_task", "replan_tasks",
        "apply_replan",
        "request_capability_change", "apply_capability_change",
        "request_approval", "request_candidate_approval", "authorize_candidate_action",
        "finish_run",
    }
    assert "passed" not in tools["validate_task"].params_json_schema["properties"]
    assert "evidence" not in tools["validate_task"].params_json_schema["properties"]
    assert "approved" not in tools["request_approval"].params_json_schema["properties"]
    assert "decide_approval" not in tools
    assert "submit_artifact" not in tools


def test_placeholder_criteria_block_planning_and_delegation():
    core = Orchestrator(SQLiteStore())
    run = core.create_run("broad goal", [INITIAL_COMPLETION_CRITERION])
    controller = DurableController(core, run.id)
    with pytest.raises(ValueError, match="completion criteria"):
        asyncio.run(controller.delegate("missing"))
    controller.set_criteria(["A persisted artifact passes the declared result schema check"])
    assert controller.inspect().plan.completion_criteria == [
        "A persisted artifact passes the declared result schema check"
    ]


def _repository_controller(tmp_path):
    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (repository / "test_calc.py").write_text("def test_add():\n    assert True\n")
    core = Orchestrator(SQLiteStore())
    run = core.create_run("use repository inputs", ["registered inputs unlock delegation"])
    return DurableController(core, run.id, SimpleNamespace(repository=repository))


def test_register_repository_inputs_computes_trusted_digests(tmp_path):
    controller = _repository_controller(tmp_path)
    registered = controller.register_repository_inputs(["calc.py", "test_calc.py"])
    repository = controller.workspaces.repository
    assert registered["calc.py"] == (
        "sha256:" + hashlib.sha256((repository / "calc.py").read_bytes()).hexdigest())
    run = controller.inspect()
    assert set(run.available_inputs) == {"calc.py", "test_calc.py"}
    assert run.available_inputs["calc.py"] == registered["calc.py"]


def test_register_repository_inputs_rejects_unsafe_paths(tmp_path):
    controller = _repository_controller(tmp_path)
    for bad in ("../outside.py", "/etc/hostname", ".git/HEAD", "missing.py"):
        with pytest.raises(ValueError):
            controller.register_repository_inputs([bad])
    assert controller.inspect().available_inputs == {}


def test_receipt_and_model_read_path_exclude_candidate_content():
    """The model-facing read path is bounded; the operator snapshot is not.

    Reverses the 2026-09-20 "inspect_run is the full-truth read" resolution: the
    compact receipts fixed the mutation path and left the read path unbounded,
    which is where the Manager's context actually grew (2026-09-27 decision).
    ``controller.inspect()`` stays full because trusted internals depend on it.
    """
    controller = controller_for(TaskNode(packet=packet(), required_checks=["result_schema"]))
    core = controller.core
    assignment = core.delegate(controller.run_id, "task", "worker")
    core.start(controller.run_id, "task")
    artifact = core.submit(controller.run_id, "task", assignment.id, "worker",
        WorkerResult(task_id="task", status="completed", summary="done",
                     deliverable="FULL-DELIVERABLE-CONTENT-MARKER"))
    receipt = json.loads(controller._receipt())
    assert receipt["tasks"]["task"]["status"] == "SUBMITTED"
    assert receipt["tasks"]["task"]["artifact_ids"] == [artifact.id]
    assert receipt["artifacts"][artifact.id] == "candidate"
    assert "FULL-DELIVERABLE-CONTENT-MARKER" not in json.dumps(receipt)

    # The read tool reports the same operational facts without the candidate body.
    view = json.loads(controller._run_view())
    assert "FULL-DELIVERABLE-CONTENT-MARKER" not in json.dumps(view)
    assert view["tasks"]["task"]["status"] == "SUBMITTED"
    assert view["artifacts"][artifact.id] == "candidate"
    # finish_run needs the criteria verbatim, so the bounded view must carry them.
    assert view["completion_criteria"] == controller.inspect().plan.completion_criteria
    assert "packet" not in json.dumps(view)

    # Drill-down carries the packet; content still only via inspect_artifact.
    detail = json.loads(controller._task_view("task"))
    assert detail["packet"]["task_id"] == "task"
    assert detail["required_checks"] == ["result_schema"]
    assert "FULL-DELIVERABLE-CONTENT-MARKER" not in json.dumps(detail)
    assert detail["artifacts"][0]["content_digest"] == artifact.content_digest

    body = json.loads(controller._artifact_view(artifact.id))
    assert "FULL-DELIVERABLE-CONTENT-MARKER" in body["content"]
    assert body["content_truncated"] is False

    # Operators keep the unbounded truth.
    assert "FULL-DELIVERABLE-CONTENT-MARKER" in controller.inspect().model_dump_json()


def test_unknown_read_targets_name_the_known_identifiers():
    controller = controller_for(TaskNode(packet=packet(), required_checks=["result_schema"]))
    with pytest.raises(ValueError, match="Unknown task nope. Known tasks: \\['task'\\]"):
        controller._task_view("nope")
    with pytest.raises(ValueError, match="Unknown artifact nope"):
        controller._artifact_view("nope")


def test_model_read_path_stays_bounded_as_a_run_grows():
    """Payload size is the honest offline proxy for Manager token cost.

    Offline evals cannot measure chattiness -- scripted steps dictate the call
    count -- but they can prove the read payload does not grow with candidate
    size. A 200x larger deliverable must not enlarge the read at all.
    """
    controller = controller_for(TaskNode(packet=packet(), required_checks=["result_schema"]))
    core = controller.core
    assignment = core.delegate(controller.run_id, "task", "worker")
    core.start(controller.run_id, "task")
    core.submit(controller.run_id, "task", assignment.id, "worker",
        WorkerResult(task_id="task", status="completed", summary="done",
                     deliverable="x" * 200_000))
    view_size = len(controller._run_view())
    full_size = len(controller.inspect().model_dump_json())
    assert view_size < 4_000, f"bounded read grew to {view_size} bytes"
    # The full snapshot carries the 200k candidate; the read path must not.
    assert full_size > 200_000
    detail = json.loads(controller._task_view("task"))
    for record in detail["artifacts"]:
        assert record["content_chars"] == 200_000
        assert "x" * 5_000 not in json.dumps(record)
    body = json.loads(controller._artifact_view(detail["artifacts"][0]["id"]))
    assert body["content_truncated"] is True
    assert len(body["content"]) < 4_500
    assert "further characters omitted" in body["content"]


def test_evidence_excerpt_keeps_the_failing_tail():
    from walter.adapter import MAX_EVIDENCE_EXCERPT_CHARS, _excerpt

    short = "compile passed"
    assert _excerpt(short) == short
    long = "noise" * 1_000 + "AssertionError: multiply(2, 3) == 5"
    excerpt = _excerpt(long)
    assert excerpt.endswith("AssertionError: multiply(2, 3) == 5")
    assert len(excerpt) < MAX_EVIDENCE_EXCERPT_CHARS + 60
    assert "earlier characters omitted" in excerpt


def test_planning_rejects_unresolvable_required_inputs_until_registered(tmp_path):
    controller = _repository_controller(tmp_path)
    wanted = packet()
    wanted.required_inputs = ["calc.py", "test_calc.py"]
    missing = controller._unresolvable_inputs([wanted])
    assert missing == {"task": ["calc.py", "test_calc.py"]}
    controller.register_repository_inputs(["calc.py", "test_calc.py"])
    assert controller._unresolvable_inputs([wanted]) == {}
    # Once registered, the kernel readiness gate that soft-locked the real run
    # clears: the task becomes delegatable.
    controller.core.add_tasks(controller.run_id, [TaskNode(
        packet=wanted, required_checks=["result_schema"])])
    assignment = controller.core.delegate(controller.run_id, "task", "worker")
    assert assignment.task_id == "task"


def test_planning_accepts_same_batch_task_references(tmp_path):
    controller = _repository_controller(tmp_path)
    upstream = packet("upstream")
    downstream = packet("downstream")
    downstream.required_inputs = ["upstream"]
    assert controller._unresolvable_inputs([upstream, downstream]) == {}


def test_workspace_tool_surface_matches_read_and_write_grants():
    class Manager:
        pass

    read_only = {item.name for item in workspace_tools(Manager(), "grant", "reader", writable=False)}
    writable = {item.name for item in workspace_tools(Manager(), "grant", "writer", writable=True)}
    assert read_only == {"read_file", "list_files", "inspect_diff", "workspace_status"}
    assert writable == read_only | {"write_file", "delete_file", "run_check"}


def test_model_only_delegation_persists_provisional_submission(monkeypatch):
    controller = controller_for(TaskNode(packet=packet(), required_checks=["result_schema"]))
    observed = {}

    async def fake_invoke(**kwargs):
        observed.update(kwargs)
        return WorkerResult(task_id="untrusted", status="completed", summary="done",
                            deliverable="fixture output")

    monkeypatch.setattr(controller, "_invoke", fake_invoke)
    artifact = asyncio.run(controller.delegate("task"))
    run = controller.inspect()
    assert artifact.worker_id.startswith("worker-")
    assert observed["tools"] == []
    assert run.tasks["task"].status == "SUBMITTED"
    assert not run.accepted_artifacts


def test_researcher_capability_receives_only_runtime_search_grant(monkeypatch):
    task = TaskNode(packet=packet(), capability=CapabilityProfile.RESEARCHER,
                    required_checks=["result_schema"])
    controller = controller_for(task)
    marker = object()
    observed = {}
    monkeypatch.setattr("walter.adapter.runtime._tools_for", lambda policy: [marker] if policy == "web_search" else [])

    async def fake_invoke(**kwargs):
        observed.update(kwargs)
        return WorkerResult(task_id="task", status="completed", summary="done",
                            deliverable="researched output")

    monkeypatch.setattr(controller, "_invoke", fake_invoke)
    asyncio.run(controller.delegate("task"))
    assert observed["tools"] == [marker]


def test_review_records_fresh_identity_and_cannot_accept_by_itself(monkeypatch):
    controller = controller_for(TaskNode(packet=packet(), required_checks=["result_schema"]))

    async def author(**kwargs):
        return WorkerResult(task_id="task", status="completed", summary="done",
                            deliverable="fixture output")

    monkeypatch.setattr(controller, "_invoke", author)
    asyncio.run(controller.delegate("task"))
    controller.validate("task")

    async def reviewer(**kwargs):
        return ReviewResult(passed=True, evidence=["checked criterion"], reason="passes",
                            verdicts=fakes.verdicts(kwargs["input"]))

    monkeypatch.setattr(controller, "_invoke", reviewer)
    asyncio.run(controller.review("task"))
    run = controller.inspect()
    artifact = run.artifacts[run.tasks["task"].artifact_ids[-1]]
    assert artifact.reviews[-1].reviewer_id != artifact.worker_id
    assert run.tasks["task"].status == "REVIEWING"
    assert not run.accepted_artifacts


def test_developer_revision_gets_fresh_workspace_and_cleans_old_candidate(tmp_path, monkeypatch):
    repository = tmp_path / "fixture"
    repository.mkdir()
    (repository / "README.md").write_text("# Fixture\n")
    _git_fixture(repository)
    core = Orchestrator(SQLiteStore())
    run = core.create_run("developer revision", ["A revised candidate is submitted"])
    core.add_tasks(run.id, [TaskNode(
        packet=packet(), capability=CapabilityProfile.DEVELOPER_SANDBOX,
        required_checks=["compile"],
    )])
    workspaces = WorkspaceManager(repository)
    controller = DurableController(core, run.id, workspaces)

    async def candidate(**kwargs):
        _write_candidate(controller)
        return WorkerResult(task_id="task", status="completed", summary="candidate",
                            deliverable="bounded candidate")

    monkeypatch.setattr(controller, "_invoke", candidate)
    asyncio.run(controller.delegate("task"))
    first = controller.inspect().tasks["task"].workspace_id
    first_root = Path(workspaces.inspect_grant(first).root)
    failure = core.fail(run.id, "task", FailureClass.BAD_OUTPUT, "revision required")
    core.recover(run.id, failure.id, "commission a corrected candidate")

    asyncio.run(controller.delegate("task"))
    current = controller.inspect()
    second = current.tasks["task"].workspace_id
    assert second != first
    assert not first_root.exists()
    assert current.tasks["task"].status == "SUBMITTED"
    assert any(event.kind == "workspace.replaced" for event in core.store.events(run.id))


def test_developer_candidate_with_changed_test_file_uses_scoped_pytest(tmp_path, monkeypatch):
    controller, manager = _developer_pytest_controller(tmp_path)

    async def candidate(**kwargs):
        workspace_id = controller.inspect().tasks[kwargs["task_id"]].workspace_id
        manager.write_file(workspace_id, "tests/test_candidate.py",
                           "def test_ok():\n    assert True\n",
                           worker_id=kwargs["worker_id"])
        return WorkerResult(task_id="task", status="completed", summary="candidate",
                            deliverable="bounded candidate")

    monkeypatch.setattr(controller, "_invoke", candidate)
    asyncio.run(controller.delegate("task"))
    controller.validate("task")
    task = controller.inspect().tasks["task"]
    validation = controller.inspect().artifacts[task.artifact_ids[-1]].validations[-1]
    assert validation.check == "pytest" and validation.passed
    argv = manager.executions[-1][2]
    assert argv[:3] == ["python3", "-m", "pytest"]
    assert "tests/test_candidate.py" in argv
    assert "tests" not in argv


def test_candidate_and_regression_pytest_checks_run_separate_scopes(tmp_path, monkeypatch):
    """pytest_candidate covers the candidate's tests; pytest_regression covers
    the pre-existing suite, so a change that breaks old tests cannot pass by
    adding only its own green file (2026-09-21 decision)."""
    repository = tmp_path / "fixture"
    (repository / "tests").mkdir(parents=True)
    (repository / "README.md").write_text("# Fixture\n")
    (repository / "tests" / "test_existing.py").write_text("def test_old():\n    assert True\n")
    _git_fixture(repository)
    core = Orchestrator(SQLiteStore())
    run = core.create_run("split pytest", ["Candidate and regression suites pass"])
    core.add_tasks(run.id, [TaskNode(
        packet=packet(), capability=CapabilityProfile.DEVELOPER_SANDBOX,
        required_checks=["pytest_candidate", "pytest_regression"],
    )])
    manager = RecordingWorkspaceManager(repository)
    controller = DurableController(core, run.id, manager)

    async def candidate(**kwargs):
        workspace_id = controller.inspect().tasks[kwargs["task_id"]].workspace_id
        manager.write_file(workspace_id, "tests/test_candidate.py",
                           "def test_ok():\n    assert True\n",
                           worker_id=kwargs["worker_id"])
        return WorkerResult(task_id="task", status="completed", summary="candidate",
                            deliverable="bounded candidate")

    monkeypatch.setattr(controller, "_invoke", candidate)
    asyncio.run(controller.delegate("task"))
    controller.validate("task")

    task = controller.inspect().tasks["task"]
    validations = controller.inspect().artifacts[task.artifact_ids[-1]].validations
    by_check = {v.check: v for v in validations}
    assert by_check["pytest_candidate"].passed and by_check["pytest_regression"].passed
    assert len(manager.executions) == 2
    candidate_argv, regression_argv = (manager.executions[0][2], manager.executions[1][2])
    assert "tests/test_candidate.py" in candidate_argv
    assert "tests/test_existing.py" not in candidate_argv
    assert "tests/test_existing.py" in regression_argv
    assert "tests/test_candidate.py" not in regression_argv


def test_regression_check_fails_honestly_without_preexisting_tests(tmp_path, monkeypatch):
    repository = tmp_path / "fixture"
    repository.mkdir()
    (repository / "README.md").write_text("# Fixture\n")
    _git_fixture(repository)
    core = Orchestrator(SQLiteStore())
    run = core.create_run("regression scope", ["Regression suite passes"])
    core.add_tasks(run.id, [TaskNode(
        packet=packet(), capability=CapabilityProfile.DEVELOPER_SANDBOX,
        required_checks=["pytest_regression"],
    )])
    manager = RecordingWorkspaceManager(repository)
    controller = DurableController(core, run.id, manager)

    async def candidate(**kwargs):
        workspace_id = controller.inspect().tasks[kwargs["task_id"]].workspace_id
        manager.write_file(workspace_id, "src/module.py", "VALUE = 2\n",
                           worker_id=kwargs["worker_id"])
        return WorkerResult(task_id="task", status="completed", summary="candidate",
                            deliverable="bounded candidate")

    monkeypatch.setattr(controller, "_invoke", candidate)
    asyncio.run(controller.delegate("task"))
    controller.validate("task")
    validation = controller.inspect().artifacts[
        controller.inspect().tasks["task"].artifact_ids[-1]].validations[-1]
    assert validation.check == "pytest_regression" and not validation.passed
    assert "No pre-existing test files" in validation.evidence
    assert manager.executions == []


def test_developer_candidate_without_changed_test_files_fails_validation(tmp_path, monkeypatch):
    controller, manager = _developer_pytest_controller(tmp_path)

    async def candidate(**kwargs):
        workspace_id = controller.inspect().tasks[kwargs["task_id"]].workspace_id
        manager.write_file(workspace_id, "src/module.py", "VALUE = 2\n",
                           worker_id=kwargs["worker_id"])
        return WorkerResult(task_id="task", status="completed", summary="candidate",
                            deliverable="bounded candidate")

    monkeypatch.setattr(controller, "_invoke", candidate)
    asyncio.run(controller.delegate("task"))
    controller.validate("task")
    task = controller.inspect().tasks["task"]
    validation = controller.inspect().artifacts[task.artifact_ids[-1]].validations[-1]
    assert validation.check == "pytest" and not validation.passed
    assert "No candidate test files" in validation.evidence
    assert manager.executions == []


@pytest.mark.parametrize("stale_outcome", ["completion", "error"])
def test_stale_worker_cannot_fail_new_running_assignment(monkeypatch, stale_outcome):
    controller = controller_for(TaskNode(packet=packet(), required_checks=["result_schema"]))
    core = controller.core

    async def scenario():
        first_started = asyncio.Event()
        second_started = asyncio.Event()
        release_first = asyncio.Event()
        release_second = asyncio.Event()
        calls = 0

        async def invoke(**kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                first_started.set()
                await release_first.wait()
                if stale_outcome == "error":
                    raise RuntimeError("old worker failed late")
                return WorkerResult(task_id="task", status="completed", summary="stale",
                                    deliverable="stale completion")
            second_started.set()
            await release_second.wait()
            return WorkerResult(task_id="task", status="completed", summary="current",
                                deliverable="current completion")

        monkeypatch.setattr(controller, "_invoke", invoke)
        first_call = asyncio.create_task(controller.delegate("task"))
        await first_started.wait()
        first_assignment = controller.inspect().tasks["task"].assignment
        failure = core.fail_assignment(
            controller.run_id, "task", first_assignment.id, first_assignment.worker_id,
            FailureClass.PROVIDER_FAILURE, "manager observed interrupted provider call",
        )
        core.recover(controller.run_id, failure.id, "retry with a fresh worker")

        second_call = asyncio.create_task(controller.delegate("task"))
        await second_started.wait()
        second_assignment = controller.inspect().tasks["task"].assignment
        assert second_assignment.id != first_assignment.id
        release_first.set()
        with pytest.raises(Exception):
            await first_call
        current = controller.inspect().tasks["task"]
        assert current.status == "RUNNING"
        assert current.assignment.id == second_assignment.id

        release_second.set()
        artifact = await second_call
        assert artifact.worker_id == second_assignment.worker_id
        assert controller.inspect().tasks["task"].status == "SUBMITTED"

    asyncio.run(scenario())


def test_replan_add_path_preserves_declared_capability_and_checks():
    controller = controller_for(TaskNode(packet=packet(), required_checks=["result_schema"]))
    developer_packet = packet("dev-fix")
    proposal, approval = controller.propose_replan(
        trigger="developer lane required", evidence=["implementation evidence needed"],
        add=[developer_packet], remove=[], reopen=[], dependencies={}, risks=[],
        add_capabilities=["developer_sandbox"], add_checks=[["pytest"]],
    )
    added = proposal.add[0]
    assert added.capability == CapabilityProfile.DEVELOPER_SANDBOX
    assert added.required_checks == ["pytest"]
    assert added.review_required and added.high_risk
    controller.core.decide_approval(controller.run_id, approval.id, True,
                                    "human", "approve exact developer replan")
    controller.apply_replan(proposal.id)
    task = controller.inspect().tasks["dev-fix"]
    assert task.capability == CapabilityProfile.DEVELOPER_SANDBOX
    assert task.required_checks == ["pytest"]


def test_replan_add_path_rejects_mismatched_capability_declarations():
    controller = controller_for(TaskNode(packet=packet(), required_checks=["result_schema"]))
    with pytest.raises(ValueError, match="one capability and check list per packet"):
        controller.propose_replan(
            trigger="bad declaration", evidence=["gap"], add=[packet("dev-fix")],
            remove=[], reopen=[], dependencies={}, risks=[],
            add_capabilities=["developer_sandbox", "model_only"], add_checks=[["pytest"]],
        )


def test_material_replan_waits_for_exact_approval_and_rejection_cannot_apply():
    controller = controller_for(TaskNode(packet=packet(), required_checks=["result_schema"]))
    proposal, approval = controller.propose_replan(
        trigger="remove obsolete work", evidence=["objective changed"], add=[],
        remove=["task"], reopen=[], dependencies={}, risks=["task is cancelled"],
    )
    assert proposal.requires_approval and approval is not None
    assert controller.inspect().plan.revision == 0
    with pytest.raises(ValueError, match="approval"):
        controller.apply_replan(proposal.id)
    controller.core.decide_approval(controller.run_id, approval.id, False,
                                    "human", "do not alter the plan")
    with pytest.raises(ValueError, match="approval"):
        controller.apply_replan(proposal.id)


def test_invalid_replan_proposal_binds_no_approval_gate():
    """Validation runs before the approval request, so a bad proposal costs no review.

    The pygtrie rehearsal burned all three replans on kernel-invalid proposals
    (task-id collision, bad dependency map) and paid a human approval round-trip
    for each, because the gate binds to the exact proposal.
    """
    controller = controller_for(TaskNode(packet=packet(), required_checks=["result_schema"]))
    before = controller.inspect()
    with pytest.raises(ValueError, match="Invalid replan proposal"):
        controller.propose_replan(
            trigger="collide with the live plan", evidence=["gap"],
            add=[packet("task")], remove=[], reopen=[],
            dependencies={}, risks=[],
        )
    with pytest.raises(ValueError, match="Invalid replan proposal"):
        controller.propose_replan(
            trigger="depend on nothing", evidence=["gap"], add=[], remove=[], reopen=[],
            dependencies={"task": ["ghost"]}, risks=[],
        )
    after = controller.inspect()
    assert after.plan.revision == before.plan.revision
    assert after.approvals == {} and after.replans == {}
    # The same lane proposed correctly still gets exactly one gate.
    proposal, approval = controller.propose_replan(
        trigger="add an independent lane", evidence=["coverage gap"],
        add=[packet("valid")], remove=[], reopen=[], dependencies={}, risks=[],
    )
    assert approval is not None
    assert len(controller.inspect().approvals) == 1


def test_replan_tool_refuses_an_invalid_proposal_without_recording_a_gate():
    """The Manager-facing tool boundary refuses before any approval exists.

    The SDK turns a tool exception into an error string for the model rather
    than raising, so assert on the returned refusal text and prove the durable
    state is untouched: no approval gate, no persisted proposal.
    """
    controller = controller_for(TaskNode(packet=packet(), required_checks=["result_schema"]))
    tools = {item.name: item for item in controller.tools()}

    async def scenario():
        refusal = await _call_tool(tools["replan_tasks"], trigger="collide with the live plan",
                                   evidence=["gap"], add=[packet("task").model_dump()],
                                   remove=[], reopen=[], dependencies_json="{}", risks=[])
        assert "Invalid replan proposal" in refusal and "fresh unique task" in refusal
        assert controller.inspect().approvals == {} and controller.inspect().replans == {}
        return json.loads(await _call_tool(
            tools["replan_tasks"], trigger="add an independent lane", evidence=["coverage gap"],
            add=[packet("valid").model_dump()], remove=[], reopen=[],
            dependencies_json="{}", risks=[]))
    payload = asyncio.run(scenario())
    assert payload["approval"]["status"] == "pending"
    state = controller.inspect()
    assert len(state.approvals) == 1
    assert list(state.replans) == [payload["proposal"]["id"]]


def test_read_only_lane_review_does_not_require_candidate_inspection(tmp_path, monkeypatch):
    """A scout lane's empty diff is expected: its reported content is reviewed.

    Demanding an inspected candidate file failed every read-only lane in the
    pygtrie rehearsal, because a read-only grant can never produce a diff.
    """
    repository = tmp_path / "fixture"
    repository.mkdir()
    (repository / "README.md").write_text("# Fixture\n")
    _git_fixture(repository)
    core = Orchestrator(SQLiteStore())
    run = core.create_run("scout the layout", ["A cited layout report exists"])
    core.add_tasks(run.id, [TaskNode(packet=packet(), capability=CapabilityProfile.REPO_READER,
                                     required_checks=["result_schema"])])
    workspaces = RecordingWorkspaceManager(repository)
    controller = DurableController(core, run.id, workspaces)

    async def scout(**kwargs):
        return WorkerResult(task_id="task", status="completed", summary="layout report",
                            deliverable="Top-level: README.md, tests/; tests run via pytest")

    monkeypatch.setattr(controller, "_invoke", scout)
    asyncio.run(controller.delegate("task"))
    assert controller.inspect().tasks["task"].workspace_id
    controller.validate("task")

    observed = {}

    async def reviewer(**kwargs):
        observed.update(kwargs)
        return ReviewResult(passed=True, evidence=["report names files this repo contains"],
                            reason="content reviewed against the repository",
                            verdicts=fakes.verdicts(kwargs["input"]))

    monkeypatch.setattr(controller, "_invoke", reviewer)
    asyncio.run(controller.review("task"))
    state = controller.inspect()
    review = state.artifacts[state.tasks["task"].artifact_ids[-1]].reviews[-1]
    assert review.passed
    assert "did not inspect" not in review.evidence
    assert "read-only investigation report" in observed["instructions"]


def test_developer_lane_review_still_requires_candidate_inspection(tmp_path, monkeypatch):
    """The inspection requirement survives for lanes that can change files."""
    controller, _ = _developer_pytest_controller(tmp_path)

    async def candidate(**kwargs):
        _write_candidate(controller)
        return WorkerResult(task_id="task", status="completed", summary="candidate",
                            deliverable="bounded candidate")

    monkeypatch.setattr(controller, "_invoke", candidate)
    asyncio.run(controller.delegate("task"))
    controller.validate("task")

    attempts = []

    async def uninspected(**kwargs):
        attempts.append(kwargs["instructions"])
        return ReviewResult(passed=True, evidence=["looks fine"], reason="no files read",
                            verdicts=fakes.verdicts(kwargs["input"]))

    monkeypatch.setattr(controller, "_invoke", uninspected)
    report = asyncio.run(controller.review("task"))
    state = controller.inspect()
    review = state.artifacts[state.tasks["task"].artifact_ids[-1]].reviews[-1]
    assert not review.passed
    assert "did not open any candidate file" in review.evidence
    # Asked once more before the verdict is discarded, and the reason says why.
    assert len(attempts) == 2 and "rejected because you read no file" in attempts[1]
    assert report.reason.startswith("The reviewer did not open any candidate file")


def test_a_reviewer_that_reads_on_its_second_attempt_is_accepted(tmp_path, monkeypatch):
    controller, _ = _developer_pytest_controller(tmp_path)

    async def candidate(**kwargs):
        _write_candidate(controller)
        return WorkerResult(task_id="task", status="completed", summary="candidate",
                            deliverable="bounded candidate")

    monkeypatch.setattr(controller, "_invoke", candidate)
    asyncio.run(controller.delegate("task"))
    controller.validate("task")
    attempts = []

    async def reads_when_told(**kwargs):
        attempts.append(1)
        if len(attempts) == 2:
            read_file = next(t for t in kwargs["tools"] if t.name == "read_file")
            await _call_tool(read_file, path="test_candidate.py")
        return ReviewResult(passed=True, evidence=["read test_candidate.py"], reason="met",
                            verdicts=fakes.verdicts(kwargs["input"]))

    monkeypatch.setattr(controller, "_invoke", reads_when_told)
    assert asyncio.run(controller.review("task")).passed
    assert len(attempts) == 2


def _submitted_for_review(monkeypatch, node=None):
    controller = controller_for(node or TaskNode(packet=packet(), required_checks=["result_schema"]))

    async def author(**kwargs):
        return WorkerResult(task_id="task", status="completed", summary="done",
                            deliverable="fixture output")

    monkeypatch.setattr(controller, "_invoke", author)
    asyncio.run(controller.delegate("task"))
    controller.validate("task")
    return controller


def _review_with(controller, monkeypatch, result, **review_arguments):
    observed = {}

    async def reviewer(**kwargs):
        observed.update(kwargs)
        return result(kwargs["input"]) if callable(result) else result

    monkeypatch.setattr(controller, "_invoke", reviewer)
    report = asyncio.run(controller.review("task", **review_arguments))
    state = controller.inspect()
    return report, state.artifacts[state.tasks["task"].artifact_ids[-1]].reviews[-1], observed


def test_reviewer_is_given_every_planned_item_and_the_request(monkeypatch):
    planned = packet().model_copy(update={"constraints": ["Never read a secret from argv"]})
    controller = _submitted_for_review(monkeypatch, TaskNode(packet=planned, required_checks=["result_schema"]))
    _, _, observed = _review_with(
        controller, monkeypatch,
        lambda review_input: ReviewResult(passed=True, evidence=["checked"], reason="passes",
                                          verdicts=fakes.verdicts(review_input)),
        run_criteria=["The vault folder is hidden"])
    payload = json.loads(observed["input"])
    assert payload["request"] == controller.inspect().objective
    assert payload["plan_items"] == [
        {"item": 1, "text": "Planned deliverable is present and complete: One inspectable result"},
        {"item": 2, "text": "Acceptance criterion: result is present"},
        {"item": 3, "text": "Constraint respected: Never read a secret from argv"},
        {"item": 4, "text": "Run success criterion this task alone covers: The vault folder is hidden"},
    ]
    assert "one verdict for each" in observed["instructions"]


def test_review_cannot_pass_without_a_verdict_on_every_plan_item(monkeypatch):
    """A reviewer's "passed" is a claim; silence about part of the plan fails it."""
    controller = _submitted_for_review(monkeypatch)
    report, review, _ = _review_with(
        controller, monkeypatch,
        ReviewResult(passed=True, evidence=["tests pass"], reason="looks complete",
                     verdicts=[{"item": 1, "met": True, "evidence": "present"}]))
    assert not report.passed and not review.passed
    assert report.reason == "No verdict on plan item 2: Acceptance criterion: result is present"
    with pytest.raises(GateError, match="Independent review required"):
        controller.core.accept(controller.run_id, "task", reason="try anyway")


def test_review_fails_on_an_unmet_plan_item_whatever_the_reviewer_concluded(monkeypatch):
    controller = _submitted_for_review(monkeypatch)
    report, review, _ = _review_with(
        controller, monkeypatch,
        ReviewResult(passed=True, evidence=["tests pass"], reason="good enough",
                     verdicts=[{"item": 1, "met": False, "evidence": "no launcher shortcut in the diff"},
                               {"item": 2, "met": True, "evidence": "result.txt"}]))
    assert not review.passed
    assert report.reason == ("Plan item 1 not met: Planned deliverable is present and complete: "
                             "One inspectable result (no launcher shortcut in the diff)")
    assert report.reason in json.loads(review.evidence)["evidence"]


def test_review_fails_on_a_plan_item_ruled_met_without_evidence(monkeypatch):
    """An empty "met" is as unbacked as silence: the reviewer has to say where it found the item."""
    controller = _submitted_for_review(monkeypatch)
    report, review, _ = _review_with(
        controller, monkeypatch,
        ReviewResult(passed=True, evidence=["tests pass"], reason="looks complete",
                     verdicts=[{"item": 1, "met": True, "evidence": "result.txt"},
                               {"item": 2, "met": True, "evidence": "  "}]))
    assert not report.passed and not review.passed
    assert report.reason == "Plan item 2 ruled met without evidence: Acceptance criterion: result is present"


@pytest.mark.parametrize("severity,passes", [("critical", False), ("High", False), ("medium", True), ("low", True)])
def test_serious_findings_fail_a_review_that_meets_every_plan_item(monkeypatch, severity, passes):
    controller = _submitted_for_review(monkeypatch)
    report, review, _ = _review_with(
        controller, monkeypatch,
        lambda review_input: ReviewResult.model_validate({
            "passed": True, "evidence": ["checked"], "reason": "meets the criteria",
            "verdicts": fakes.verdicts(review_input),
            "findings": [{"severity": severity, "issue": "The passphrase is read from argv"}]}))
    assert review.passed is passes
    if not passes:
        assert report.reason == f"{severity.capitalize()} finding: The passphrase is read from argv"


def test_failed_review_keeps_the_reviewers_reason_and_names_the_plan_items(monkeypatch):
    controller = _submitted_for_review(monkeypatch)
    report, _, _ = _review_with(
        controller, monkeypatch,
        lambda review_input: ReviewResult(passed=False, evidence=["read it"], reason="Half of it is missing.",
                                          verdicts=fakes.verdicts(review_input, met=False)))
    assert report.reason.startswith("Half of it is missing. Plan item 1 not met: ")


def test_exact_approved_material_replan_applies_and_stale_one_fails():
    controller = controller_for(TaskNode(packet=packet(), required_checks=["result_schema"]))
    stale, stale_approval = controller.propose_replan(
        trigger="candidate removal", evidence=["new constraint"], add=[],
        remove=["task"], reopen=[], dependencies={}, risks=[],
    )
    additive_packet = packet("additive")
    additive, approval = controller.propose_replan(
        trigger="add independent evidence", evidence=["coverage gap"], add=[additive_packet],
        remove=[], reopen=[], dependencies={}, risks=[],
    )
    assert approval is not None
    assert controller.inspect().plan.revision == 0
    controller.core.decide_approval(controller.run_id, approval.id, True,
                                    "human", "approve exact additive proposal")
    controller.apply_replan(additive.id)
    assert controller.inspect().plan.revision == 1
    controller.core.decide_approval(controller.run_id, stale_approval.id, True,
                                    "human", "approve original revision")
    with pytest.raises(ValueError, match="Stale proposal"):
        controller.apply_replan(stale.id)

    current, current_approval = controller.propose_replan(
        trigger="remove obsolete original", evidence=["superseded by additive"], add=[],
        remove=["task"], reopen=[], dependencies={}, risks=[],
    )
    controller.core.decide_approval(controller.run_id, current_approval.id, True,
                                    "human", "approve exact current proposal")
    controller.apply_replan(current.id)
    run = controller.inspect()
    assert run.plan.revision == 2
    assert run.tasks["task"].status == "CANCELLED"


@pytest.mark.parametrize("status", ["blocked", "needs_revision"])
def test_provisional_worker_result_is_preserved_and_routed_without_submission(monkeypatch, status):
    controller = controller_for(TaskNode(packet=packet(), required_checks=["result_schema"]))

    async def provisional(**kwargs):
        return WorkerResult(task_id="task", status=status, summary="partial result",
                            deliverable="useful partial evidence", blocker="criterion unresolved")

    monkeypatch.setattr(controller, "_invoke", provisional)
    result = asyncio.run(controller.delegate("task"))
    run = controller.inspect()
    assert result.status == status
    assert run.tasks["task"].status == "REVISION_REQUIRED"
    assert run.tasks["task"].result.deliverable == "useful partial evidence"
    assert not run.tasks["task"].artifact_ids
    assert run.failures[-1].classification in {FailureClass.BAD_OUTPUT, FailureClass.MISSING_EVIDENCE}


def _controller_with_capability_request(monkeypatch, repository, *, checks=None,
                                        requested="developer_sandbox"):
    core = Orchestrator(SQLiteStore())
    run = core.create_run("capability escalation", ["A bounded candidate is produced"])
    if checks is None:
        checks = ["compile"] if requested == "developer_sandbox" else ["result_schema"]
    core.add_tasks(run.id, [TaskNode(packet=packet(), required_checks=checks)])
    controller = DurableController(core, run.id, WorkspaceManager(repository))
    observed = {}

    async def blocked(**kwargs):
        observed["tools"] = kwargs["tools"]
        return WorkerResult(
            task_id="task", status="blocked", summary="repository write access required",
            deliverable="analysis completed before capability boundary",
            blocker="developer sandbox required",
            capability_request={
                "requested_capability": requested,
                "reason": "Implement the accepted bounded change",
                "risk": "Candidate source can be modified only in isolation",
            },
        )

    monkeypatch.setattr(controller, "_invoke", blocked)
    asyncio.run(controller.delegate("task"))
    request = next(iter(controller.inspect().capability_requests.values()))
    return controller, request, observed


def test_capability_escalation_pending_and_denied_never_grants_tools(tmp_path, monkeypatch):
    repository = tmp_path / "fixture"
    repository.mkdir()
    (repository / "README.md").write_text("# Fixture\n")
    _git_fixture(repository)
    controller, request, observed = _controller_with_capability_request(monkeypatch, repository)
    assert observed["tools"] == []
    run = controller.inspect()
    assert run.tasks["task"].status == "BLOCKED"
    assert run.tasks["task"].capability == CapabilityProfile.MODEL_ONLY

    linked, approval = controller.request_capability_change(request.id, "Human review required")
    workspace_id = json.loads(approval.scope_json)["workspace_id"]
    assert linked.approval_id == approval.id
    assert controller.inspect().tasks["task"].workspace_id is None
    with pytest.raises(ValueError, match="not been granted"):
        controller.apply_capability_change(request.id)
    controller.core.decide_approval(controller.run_id, approval.id, False,
                                    "human", "deny repository write access")
    with pytest.raises(ValueError, match="denied"):
        controller.apply_capability_change(request.id)
    run = controller.inspect()
    assert run.capability_requests[request.id].status == CapabilityRequestStatus.DENIED
    assert run.tasks["task"].capability == CapabilityProfile.MODEL_ONLY
    with pytest.raises(Exception):
        controller.workspaces.inspect_grant(workspace_id)


def test_developer_escalation_without_executable_check_is_denied_before_allocation(tmp_path, monkeypatch):
    repository = tmp_path / "fixture"
    repository.mkdir()
    (repository / "README.md").write_text("# Fixture\n")
    _git_fixture(repository)
    controller, request, _ = _controller_with_capability_request(
        monkeypatch, repository, checks=["result_schema"]
    )
    with pytest.raises(ValueError, match="requires compile"):
        controller.request_capability_change(request.id, "unsafe escalation")
    run = controller.inspect()
    assert run.capability_requests[request.id].status == CapabilityRequestStatus.DENIED
    assert run.approvals == {}
    assert run.tasks["task"].workspace_id is None
    sandboxes = repository / ".local" / "sandboxes"
    assert not list(sandboxes.glob("candidate-*"))


def test_exact_approved_capability_request_applies_persisted_profile(tmp_path, monkeypatch):
    repository = tmp_path / "fixture"
    repository.mkdir()
    (repository / "README.md").write_text("# Fixture\n")
    _git_fixture(repository)
    controller, request, _ = _controller_with_capability_request(monkeypatch, repository)
    _, approval = controller.request_capability_change(request.id, "Review exact sandbox grant")
    controller.core.decide_approval(controller.run_id, approval.id, True,
                                    "human", "approve isolated candidate workspace")
    run = controller.apply_capability_change(request.id)
    scope = json.loads(approval.scope_json)
    assert run.capability_requests[request.id].status == CapabilityRequestStatus.ESCALATED
    assert run.tasks["task"].capability == CapabilityProfile.DEVELOPER_SANDBOX
    assert run.tasks["task"].workspace_id == scope["workspace_id"]
    assert run.tasks["task"].status == "READY"

    version = run.version
    reloaded = DurableController(
        Orchestrator(controller.core.store), controller.run_id, WorkspaceManager(repository)
    )
    reloaded_run = reloaded.apply_capability_change(request.id)
    assert reloaded_run.version == version

    async def completed(**kwargs):
        _write_candidate(reloaded)
        return WorkerResult(task_id="task", status="completed", summary="implemented",
                            deliverable="approved workspace candidate")

    monkeypatch.setattr(reloaded, "_invoke", completed)
    artifact = asyncio.run(reloaded.delegate("task"))
    after = reloaded.inspect()
    assert after.tasks["task"].workspace_id == scope["workspace_id"]
    assert artifact.workspace_fingerprint == reloaded.workspaces.fingerprint(scope["workspace_id"])
    replacements = [event for event in reloaded.core.store.events(reloaded.run_id)
                    if event.kind == "workspace.replaced"]
    assert replacements == []


def test_repo_reader_escalation_reuses_exact_workspace_with_read_only_tools(tmp_path, monkeypatch):
    repository = tmp_path / "fixture"
    repository.mkdir()
    (repository / "README.md").write_text("# Fixture\n")
    _git_fixture(repository)
    controller, request, _ = _controller_with_capability_request(
        monkeypatch, repository, requested="repo_reader"
    )
    _, approval = controller.request_capability_change(request.id, "Approve bounded repository reads")
    scope = json.loads(approval.scope_json)
    assert scope["workspace_id"]
    controller.core.decide_approval(controller.run_id, approval.id, True,
                                    "human", "approve read-only repository access")
    run = controller.apply_capability_change(request.id)
    assert run.tasks["task"].capability == CapabilityProfile.REPO_READER
    assert run.tasks["task"].workspace_id == scope["workspace_id"]
    observed = {}

    async def completed(**kwargs):
        observed["tools"] = {tool.name for tool in kwargs["tools"]}
        return WorkerResult(task_id="task", status="completed", summary="inspected",
                            deliverable="repository inspection")

    monkeypatch.setattr(controller, "_invoke", completed)
    asyncio.run(controller.delegate("task"))
    assert observed["tools"] == {"read_file", "list_files", "inspect_diff", "workspace_status"}
    assert controller.inspect().tasks["task"].workspace_id == scope["workspace_id"]


def test_controller_instructions_combine_prompt_appendix_and_run_id():
    controller = controller_for(TaskNode(packet=packet(), required_checks=["result_schema"]))
    instructions = controller.instructions()
    assert ("You are **Walter**, a general-purpose orchestration Manager."
            in instructions)
    assert DURABLE_INSTRUCTIONS in instructions
    assert instructions.endswith("Run ID: " + controller.run_id)


def test_load_system_prompt_returns_repo_prompt_stripped_and_non_empty():
    prompt = load_system_prompt()
    repo_prompt = (Path(__file__).resolve().parents[1] / "doctrine" / "SYSTEM_PROMPT.md").read_text(
        encoding="utf-8"
    )
    assert prompt == repo_prompt.strip()
    assert prompt


def test_load_system_prompt_missing_file_raises_runtime_error(monkeypatch, tmp_path):
    fake_module = tmp_path / "src" / "walter" / "adapter.py"
    fake_module.parent.mkdir(parents=True)
    monkeypatch.setattr("walter.adapter.Path", lambda *_: fake_module)
    with pytest.raises(RuntimeError, match="Walter system prompt not found"):
        load_system_prompt()


def test_invoke_passes_configured_budget_to_worker_model(monkeypatch):
    from walter import runtime
    from walter.usage import UsageBudget
    from walter.usage_model import UsageRecordingModel

    controller = controller_for(TaskNode(packet=packet(), required_checks=["result_schema"]))
    budget = UsageBudget(max_calls=5)
    captured = {}

    monkeypatch.setattr(
        runtime.RuntimeConfig,
        "from_env",
        classmethod(lambda cls: runtime.RuntimeConfig(
            "openrouter", "key", "https://openrouter.ai/api/v1", "manager", "worker", budget
        )),
    )
    monkeypatch.setattr(runtime, "build_models", lambda config: (object(), object()))
    monkeypatch.setattr(runtime, "_agent", lambda **kwargs: captured.update(kwargs) or kwargs)

    class Result:
        final_output = "ok"

    async def fake_run(*args, **kwargs):
        return Result()

    monkeypatch.setattr("walter.adapter.Runner.run", fake_run)

    asyncio.run(controller._invoke(
        name="Worker", instructions="do the task", output_type=str, tools=[],
        input="payload", task_id="task", worker_id="worker-1", role="worker"))

    model = captured["model"]
    assert isinstance(model, UsageRecordingModel)
    assert model.budget is budget


def test_invoke_resolves_configuration_once_per_controller(monkeypatch):
    from walter import runtime
    from walter.usage import UsageBudget
    from walter.usage_model import UsageRecordingModel

    controller = controller_for(TaskNode(packet=packet(), required_checks=["result_schema"]))
    budget = UsageBudget(max_calls=5)
    captured = {}
    resolutions = []

    def counting_from_env(cls):
        resolutions.append(1)
        return runtime.RuntimeConfig(
            "openrouter", "key", "https://openrouter.ai/api/v1", "manager", "worker", budget
        )

    monkeypatch.setattr(runtime.RuntimeConfig, "from_env", classmethod(counting_from_env))
    monkeypatch.setattr(runtime, "build_models", lambda config: (object(), object()))
    monkeypatch.setattr(runtime, "_agent", lambda **kwargs: captured.update(kwargs) or kwargs)

    class Result:
        final_output = "ok"

    async def fake_run(*args, **kwargs):
        return Result()

    monkeypatch.setattr("walter.adapter.Runner.run", fake_run)

    for _ in range(2):
        asyncio.run(controller._invoke(
            name="Worker", instructions="do the task", output_type=str, tools=[],
            input="payload", task_id="task", worker_id="worker-1", role="worker"))

    assert len(resolutions) == 1
    model = captured["model"]
    assert isinstance(model, UsageRecordingModel)
    assert model.budget is budget


def test_validate_task_reports_the_actual_check_outcome():
    """A mutating tool result must say whether the check it just ran passed.

    The compact receipt introduced for token economy reports only task and
    artifact status, so a Manager that ran validate_task could not tell a
    recorded failure from a pass without a full inspect_run.
    """
    controller = controller_for(TaskNode(packet=packet(), required_checks=["result_schema"]))
    core = controller.core
    assignment = core.delegate(controller.run_id, "task", "worker")
    core.start(controller.run_id, "task")
    core.submit(controller.run_id, "task", assignment.id, "worker",
        WorkerResult(task_id="task", status="completed", summary="done",
                     deliverable="an inspectable result"))
    tools = {tool.name: tool for tool in controller.tools()}
    payload = json.loads(asyncio.run(_call_tool(tools["validate_task"], task_id="task")))
    assert payload["validations"] == [{"check": "result_schema", "passed": True}]
    assert payload["run"]["tasks"]["task"]["status"] == "REVIEWING"


def test_validate_task_surfaces_a_failure_with_actionable_evidence(tmp_path):
    repository = tmp_path / "repo"
    repository.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repository, check=True)
    (repository / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A"],
                   cwd=repository, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "base"], cwd=repository, check=True)
    workspaces = RecordingWorkspaceManager(repository)
    core = Orchestrator(SQLiteStore())
    run = core.create_run("fixture", ["accepted fixture"])
    core.add_tasks(run.id, [TaskNode(packet=packet(),
        capability=CapabilityProfile.DEVELOPER_SANDBOX, required_checks=["pytest"])])
    controller = DurableController(core, run.id, workspaces)
    grant = workspaces.create_candidate(run.id, "task", "author")
    core.bind_workspace(run.id, "task", grant.id)
    assignment = core.delegate(run.id, "task", "author")
    core.start(run.id, "task")
    fingerprint = workspaces.freeze(grant.id)
    core.submit(run.id, "task", assignment.id, "author",
        WorkerResult(task_id="task", status="completed", summary="done",
                     deliverable="claimed complete"),
        workspace_fingerprint=fingerprint)
    tools = {tool.name: tool for tool in controller.tools()}
    payload = json.loads(asyncio.run(_call_tool(tools["validate_task"], task_id="task")))
    outcome = payload["validations"][0]
    assert outcome == {"check": "pytest", "passed": False,
                       "evidence": "No candidate test files were added or changed; "
                                   "a developer candidate must include tests"}
    assert payload["run"]["tasks"]["task"]["attempts_remaining"] == 2
    # The kernel must still refuse acceptance, and name the failing gate.
    with pytest.raises(ValueError, match="failed: pytest"):
        core.accept(run.id, "task", reason="ignore", workspace_fingerprint=fingerprint)


def test_registering_an_unchanged_input_again_is_a_no_op(tmp_path):
    """A later planning round may repeat a path it already registered."""
    controller = _repository_controller(tmp_path)
    first = controller.register_repository_inputs(["calc.py"])
    both = controller.register_repository_inputs(["calc.py", "test_calc.py"])
    assert both["calc.py"] == first["calc.py"]
    run = controller.inspect()
    assert set(run.available_inputs) == {"calc.py", "test_calc.py"}
    assert [event.kind for event in controller.core.store.events(controller.run_id)].count(
        "input.registered") == 2


def test_registering_a_changed_input_reports_the_digest_conflict(tmp_path):
    controller = _repository_controller(tmp_path)
    controller.register_repository_inputs(["calc.py"])
    repository = controller.workspaces.repository
    (repository / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    with pytest.raises(ValueError, match="immutable but the repository file now"):
        controller.register_repository_inputs(["calc.py"])
    assert len(controller.inspect().available_inputs) == 1


def test_validation_outcome_extracts_stream_tails_from_executor_evidence():
    """Executable checks record a JSON blob; slicing it would cut mid-field."""
    evidence = json.dumps({"argv": ["python3", "-m", "pytest"], "returncode": 1,
                           "stdout": "x" * 4000 + "1 failed", "stderr": ""})
    record = SimpleNamespace(check="pytest", passed=False, evidence=evidence)
    outcome = DurableController._validation_outcome([record])[0]
    assert outcome["check"] == "pytest" and outcome["passed"] is False
    assert outcome["returncode"] == 1
    assert outcome["stdout"].endswith("1 failed") and len(outcome["stdout"]) == 1200
    assert "stderr" not in outcome


def test_failed_attempt_workspace_is_dirty_and_retry_gets_a_fresh_one(tmp_path, monkeypatch):
    """The escalation-reuse branch must fire only for a never-executed workspace.

    A worker that fails before submitting leaves a partially-modified worktree
    behind; reusing it (and its worker identity) for the retry contradicts
    replace_workspace's fresh-isolation contract (2026-09-21 decision).
    """
    repository = tmp_path / "fixture"
    repository.mkdir()
    (repository / "README.md").write_text("# Fixture\n")
    _git_fixture(repository)
    controller, request, _ = _controller_with_capability_request(monkeypatch, repository)
    _, approval = controller.request_capability_change(request.id, "Review exact sandbox grant")
    controller.core.decide_approval(controller.run_id, approval.id, True,
                                    "human", "approve isolated candidate workspace")
    controller.apply_capability_change(request.id)
    approved_workspace = controller.inspect().tasks["task"].workspace_id
    approved_worker = controller.workspaces.inspect_grant(approved_workspace).worker_id

    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()

        async def hanging(**kwargs):
            started.set()
            await release.wait()
            raise RuntimeError("provider connection dropped")

        monkeypatch.setattr(controller, "_invoke", hanging)
        first = asyncio.create_task(controller.delegate("task"))
        await started.wait()
        # Dirty the workspace mid-attempt, as a real failure would.
        controller.workspaces.write_file(approved_workspace, "half.py", "x = (\n",
                                         worker_id=approved_worker)
        assignment = controller.inspect().tasks["task"].assignment
        failure = controller.core.fail_assignment(
            controller.run_id, "task", assignment.id, assignment.worker_id,
            FailureClass.PROVIDER_FAILURE, "manager observed dropped provider call")
        controller.core.recover(controller.run_id, failure.id,
                                "retry with a fresh isolated workspace")
        release.set()
        with pytest.raises(Exception):
            await first

    asyncio.run(scenario())
    assert controller.workspaces.inspect_grant(approved_workspace).used is True

    async def candidate(**kwargs):
        return WorkerResult(task_id="task", status="completed", summary="implemented",
                            deliverable="approved workspace candidate")

    monkeypatch.setattr(controller, "_invoke", candidate)
    asyncio.run(controller.delegate("task"))
    after = controller.inspect()
    fresh = after.tasks["task"].workspace_id
    assert fresh != approved_workspace
    assert after.tasks["task"].assignment.worker_id != approved_worker
    assert any(event.kind == "workspace.replaced"
               for event in controller.core.store.events(controller.run_id))
    with pytest.raises(Exception):
        controller.workspaces.inspect_grant(approved_workspace)


def test_concurrent_update_is_retried_once_then_translated(monkeypatch):
    """A cross-operator collision (e.g. `walter run approve` mid-run) must not
    reach the Manager as an opaque tool error (2026-09-21 decision)."""
    from walter.store import ConcurrentUpdate

    controller = controller_for(TaskNode(packet=packet(), required_checks=["result_schema"]))
    tools = {tool.name: tool for tool in controller.tools()}
    criteria = ["A measurable criterion for the collision test"]

    calls = {"count": 0}
    original = controller.core.set_completion_criteria

    def flaky(run_id, value):
        calls["count"] += 1
        if calls["count"] == 1:
            raise ConcurrentUpdate("Run changed; reload before applying this operation")
        return original(run_id, value)

    monkeypatch.setattr(controller.core, "set_completion_criteria", flaky)
    payload = json.loads(asyncio.run(
        _call_tool(tools["set_completion_criteria"], criteria=criteria)))
    assert calls["count"] == 2
    assert payload["run_id"] == controller.run_id  # retried transparently

    def always(run_id, value):
        raise ConcurrentUpdate("Run changed; reload before applying this operation")

    monkeypatch.setattr(controller.core, "set_completion_criteria", always)
    payload = json.loads(asyncio.run(
        _call_tool(tools["set_completion_criteria"],
                   criteria=["Another measurable criterion for the collision test"])))
    assert payload["error"] == "concurrent_update"
    assert "Another operator just changed this run" in payload["message"]


def test_receipt_reports_the_budget_remaining_in_this_plan_revision():
    controller = controller_for(TaskNode(packet=packet(), required_checks=["result_schema"]))
    core = controller.core
    for attempt in range(3):
        core.delegate(controller.run_id, "task", f"worker-{attempt}")
        core.start(controller.run_id, "task")
        core.fail(controller.run_id, "task", FailureClass.TIMEOUT, "interrupted")
        core.recover(controller.run_id, controller.inspect().failures[-1].id, "retry")
    exhausted = json.loads(controller._receipt())["tasks"]["task"]
    assert exhausted["attempts"] == 3 and exhausted["attempts_remaining"] == 0

    proposal, approval = controller.propose_replan(
        trigger="Materially different plan", evidence=["operator narrowed scope"],
        add=[], remove=[], reopen=["task"])
    # Reopening work that only ever failed discards nothing anyone accepted, so
    # the kernel classifies it as autonomous and no human gate is created. This
    # is the case that previously cost the operator a round-trip just to unstick
    # a run whose attempt budget was exhausted.
    assert approval is None
    assert proposal.requires_approval is False
    controller.apply_replan(proposal.id)
    reopened = json.loads(controller._receipt())["tasks"]["task"]
    # Lifetime history is preserved; the new revision states a usable budget.
    assert reopened["attempts"] == 3 and reopened["attempts_remaining"] == 3
    assert core.delegate(controller.run_id, "task", "fresh-worker").task_id == "task"


@pytest.mark.skipif(not Path("/usr/bin/bwrap").exists(), reason="Bubblewrap sandbox unavailable")
def test_run_check_takes_a_check_name_and_builds_the_sandbox_template(tmp_path):
    import json as _json
    import subprocess

    from walter.adapter import CHECK_OUTPUT_CHARS, _run_check
    from walter.sandbox import WorkspaceManager

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("# demo\n")
    for args in (["init", "-q"], ["add", "."], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    manager = WorkspaceManager(repo)
    grant = manager.create_candidate("run", "task", "worker")
    manager.write_file(grant.id, "calc.py", "def add(a, b):\n    return a + b\n", worker_id="worker")
    manager.write_file(grant.id, "test_calc.py", "from calc import add\n\n\ndef test_add():\n    assert add(2, 2) == 4\n",
                       worker_id="worker")
    check = lambda name, paths=(): _json.loads(_run_check(manager, grant.id, "worker", name, list(paths)))  # noqa: E731

    assert check("pytest")["passed"] and "1 passed" in check("pytest")["output"]
    assert check("compile")["passed"]
    manager.write_file(grant.id, "test_calc.py", "from calc import add\n\ndef test_add(:\n    pass\n", worker_id="worker")
    compiled = check("compile", ["test_calc.py"])
    assert not compiled["passed"] and "SyntaxError" in compiled["output"]
    manager.write_file(grant.id, "test_calc.py",
                       "def test_noisy():\n    print('x' * 20000)\n    assert False\n", worker_id="worker")
    noisy = check("pytest")
    assert not noisy["passed"] and len(noisy["output"]) <= CHECK_OUTPUT_CHARS + 60
    assert "earlier characters omitted" in noisy["output"]
    assert "Unknown check" in check("make")["output"]
    refused = check("pytest", ["../outside.py"])
    assert not refused["passed"] and "refused" in refused["output"]


def _exhausts_after_writing(controller, manager, *, write: bool):
    from agents.exceptions import MaxTurnsExceeded

    async def invoke(**kwargs):
        if write:
            grant = controller.inspect().tasks["task"].workspace_id
            manager.write_file(grant, "tests/test_ok.py", "def test_ok():\n    assert True\n",
                               worker_id=manager.inspect_grant(grant).worker_id)
        raise MaxTurnsExceeded("Max turns (24) exceeded")
    return invoke


def test_exhausted_specialist_work_is_submitted_for_trusted_validation(tmp_path, monkeypatch):
    controller, manager = _developer_pytest_controller(tmp_path)
    controller.salvage_exhausted = True
    monkeypatch.setattr(controller, "_invoke", _exhausts_after_writing(controller, manager, write=True))
    artifact = asyncio.run(controller.delegate("task"))
    state = controller.inspect()
    task = state.tasks["task"]
    # A candidate exists and awaits the kernel's gates; salvage accepts nothing itself.
    assert task.artifact_ids == [artifact.id] and task.status == "SUBMITTED"
    assert artifact.status == "candidate"
    assert "used all its steps" in state.artifacts[artifact.id].content


@pytest.mark.parametrize("salvage,write", [(False, True), (True, False)])
def test_exhaustion_still_fails_without_salvage_or_without_work(tmp_path, monkeypatch, salvage, write):
    from agents.exceptions import MaxTurnsExceeded

    controller, manager = _developer_pytest_controller(tmp_path)
    controller.salvage_exhausted = salvage
    monkeypatch.setattr(controller, "_invoke", _exhausts_after_writing(controller, manager, write=write))
    try:
        asyncio.run(controller.delegate("task"))
    except MaxTurnsExceeded:
        pass
    task = controller.inspect().tasks["task"]
    assert task.artifact_ids == [] and task.status != "SUBMITTED"


def test_project_build_needs_a_build_script():
    from walter.adapter import _build_script_problem

    class Files:
        def __init__(self, manifest):
            self.manifest = manifest

        def read_file(self, workspace_id, path, *, worker_id=None):
            assert path == "package.json"
            return self.manifest

    assert "package.json" in _build_script_problem(Files(""), "w", "a", ["index.ts"])
    assert "not a valid JSON" in _build_script_problem(Files("{oops"), "w", "a", ["package.json"])
    assert "not a valid JSON" in _build_script_problem(Files("[]"), "w", "a", ["package.json"])
    assert "no \"build\"" in _build_script_problem(Files('{"scripts": {"test": "x"}}'), "w", "a", ["package.json"])
    assert "no \"build\"" in _build_script_problem(Files('{"scripts": {"build": " "}}'), "w", "a", ["package.json"])
    assert _build_script_problem(Files('{"scripts": {"build": "vite build"}}'), "w", "a", ["package.json"]) is None
