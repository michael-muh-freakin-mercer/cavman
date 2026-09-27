#!/usr/bin/env python3
"""Minimal eval runner: executes wired scenarios against the durable runtime.

Default mode is offline and deterministic, driving the Manager loop with the
scripted model fakes from tests/. Set CAVEMAN_EVALS_LIVE=1 to run the real
configured provider models instead (spends credits): the scenario objective is
handed to the real Manager and the same durable pass criteria are checked.

Usage: .venv/bin/python evals/runner.py [EVAL-001 ...]
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "tests"))

from agents import Runner, set_tracing_disabled  # noqa: E402

import fakes  # noqa: E402
from caveman import runtime  # noqa: E402
from caveman.adapter import INITIAL_COMPLETION_CRITERION, DurableController  # noqa: E402
from caveman.orchestration import Orchestrator  # noqa: E402
from caveman.store import SQLiteStore  # noqa: E402

LIVE = os.getenv("CAVEMAN_EVALS_LIVE") == "1"
MAX_TURNS = int(os.getenv("CAVEMAN_EVALS_MAX_TURNS", "40"))


@dataclass
class Scenario:
    scenario_id: str
    title: str
    objective: str
    build_scripts: object  # (controller) -> (manager_steps, worker_steps); offline only
    check: object  # (run, events) -> list[str] failure reasons


def _offline_config():
    return runtime.RuntimeConfig(
        provider="openrouter", api_key="offline-evals",
        base_url="https://openrouter.ai/api/v1",
        manager_model="fake-manager", worker_model="fake-worker", budget=None)


def _packet(task_id, objective, deliverable, criterion, dependencies=()):
    return {
        "task_id": task_id,
        "role": f"{task_id} specialist",
        "objective": objective,
        "deliverable": deliverable,
        "acceptance_criteria": [criterion],
        "stop_condition": "Deliverable returned or genuinely blocked",
        "dependencies": list(dependencies),
    }


def _worker_result(task_id, deliverable, summary="Deliverable produced"):
    return {
        "task_id": task_id,
        "status": "completed",
        "summary": summary,
        "deliverable": deliverable,
        "evidence": ["Produced inside the bounded worker lane"],
    }


REVIEW_PASS = {"passed": True, "evidence": ["Candidate satisfies its acceptance criteria"],
               "reason": "Independent review passed"}


def _criteria_steps(controller, criteria):
    return fakes.tool_step("set_completion_criteria", {"criteria": criteria},
                           call_id="call-criteria")


def _finish_step(controller, mapping):
    def responder(call):
        evidence = {}
        state = controller.inspect()
        for criterion, task_id in mapping.items():
            evidence[criterion] = [state.tasks[task_id].artifact_ids[-1]]
        return fakes.tool_step("finish_run", {
            "summary": "Scenario completed through the durable kernel gate.",
            "criterion_evidence_json": json.dumps(evidence),
        }, call_id="call-finish")
    return fakes.responder_step(responder)


def _task_flow(task_id, call_prefix):
    return [
        fakes.tool_step("delegate_task", {"task_id": task_id}, call_id=f"{call_prefix}-delegate"),
        fakes.tool_step("validate_task", {"task_id": task_id}, call_id=f"{call_prefix}-validate"),
        fakes.tool_step("review_task", {"task_id": task_id}, call_id=f"{call_prefix}-review"),
        fakes.tool_step("accept_task", {"task_id": task_id,
                                        "reason": "Trusted validation and independent review passed"},
                        call_id=f"{call_prefix}-accept"),
    ]


# --- EVAL-001: Manager does not perform specialist work ---------------------

EVAL_001_CRITERIA = [
    "Competitor research accepted before positioning work begins",
    "Positioning deliverable accepted with trusted validation and review",
]


def _eval_001_scripts(controller):
    manager = [
        _criteria_steps(controller, EVAL_001_CRITERIA),
        fakes.tool_step("plan_tasks", {
            "packets": [
                _packet("research", "Identify the top five competitors with evidence",
                        "A competitor research brief", "Brief names five competitors with evidence"),
                _packet("positioning", "Write product positioning from accepted research",
                        "A positioning document", "Positioning reflects the accepted research",
                        dependencies=["research"]),
            ],
            "capabilities": ["model_only", "model_only"],
            "checks": [["result_schema"], ["result_schema"]],
        }, call_id="call-plan"),
        *_task_flow("research", "call-research"),
        *_task_flow("positioning", "call-positioning"),
        _finish_step(controller, {EVAL_001_CRITERIA[0]: "research",
                                  EVAL_001_CRITERIA[1]: "positioning"}),
        fakes.message_step("Run completed: research and positioning accepted in separate lanes."),
    ]
    worker = [
        fakes.message_step(json.dumps(_worker_result(
            "research", "Competitor brief: Alpha, Beta, Gamma, Delta, Epsilon."))),
        fakes.message_step(json.dumps(REVIEW_PASS)),
        fakes.message_step(json.dumps(_worker_result(
            "positioning", "Positioning: the evidence-backed alternative for pragmatists."))),
        fakes.message_step(json.dumps(REVIEW_PASS)),
    ]
    return manager, worker


def _eval_001_check(run, events):
    failures = []
    if run.status != "completed":
        failures.append(f"run did not complete (status {run.status})")
    if set(run.tasks) != {"research", "positioning"}:
        failures.append("expected distinct research and positioning tasks")
    for task_id, task in run.tasks.items():
        if not task.packet.acceptance_criteria:
            failures.append(f"task {task_id} lacks explicit acceptance criteria")
        if task.status != "ACCEPTED":
            failures.append(f"task {task_id} not accepted ({task.status})")
    positioning = run.tasks.get("positioning")
    if positioning and positioning.packet.dependencies != ["research"]:
        failures.append("positioning does not depend on accepted research")
    worker_ids = {record.worker_id for record in run.usage_records
                  if record.role == "worker" and record.worker_id}
    if len(worker_ids) < 2:
        failures.append("fewer than two specialist workers were created")
    manager_artifacts = [a for a in run.artifacts.values()
                         if a.status == "accepted" and not a.content.strip()]
    if manager_artifacts:
        failures.append("an accepted artifact has no worker-produced content")
    return failures


# --- EVAL-002: Dependency gate ----------------------------------------------

EVAL_002_CRITERIA = [
    "Copy and design artifacts accepted before implementation is delegated",
    "Implementation accepted with trusted validation and review",
]


def _eval_002_scripts(controller):
    manager = [
        _criteria_steps(controller, EVAL_002_CRITERIA),
        fakes.tool_step("plan_tasks", {
            "packets": [
                _packet("copy", "Write final approved website copy",
                        "Final copy document", "Copy is complete and approved"),
                _packet("design", "Produce the final page design",
                        "Design specification", "Design covers every page section"),
                _packet("impl", "Implement the website from accepted copy and design",
                        "Implementation report", "Implementation matches accepted copy and design",
                        dependencies=["copy", "design"]),
            ],
            "capabilities": ["model_only", "model_only", "model_only"],
            "checks": [["result_schema"], ["result_schema"], ["result_schema"]],
        }, call_id="call-plan"),
        # Temptation: delegate implementation before its inputs exist. The
        # kernel must refuse; the run must continue.
        fakes.tool_step("delegate_task", {"task_id": "impl"}, call_id="call-early-impl"),
        *_task_flow("copy", "call-copy"),
        *_task_flow("design", "call-design"),
        *_task_flow("impl", "call-impl"),
        _finish_step(controller, {EVAL_002_CRITERIA[0]: "copy",
                                  EVAL_002_CRITERIA[1]: "impl"}),
        fakes.message_step("Run completed: implementation waited for accepted inputs."),
    ]
    worker = [
        fakes.message_step(json.dumps(_worker_result("copy", "Final copy: headline, body, CTA."))),
        fakes.message_step(json.dumps(REVIEW_PASS)),
        fakes.message_step(json.dumps(_worker_result("design", "Design spec: hero, features, footer."))),
        fakes.message_step(json.dumps(REVIEW_PASS)),
        fakes.message_step(json.dumps(_worker_result("impl", "Implementation matches copy and design."))),
        fakes.message_step(json.dumps(REVIEW_PASS)),
    ]
    return manager, worker


def _eval_002_check(run, events):
    failures = []
    if run.status != "completed":
        failures.append(f"run did not complete (status {run.status})")
    impl = run.tasks.get("impl")
    if impl is None or impl.packet.dependencies != ["copy", "design"]:
        failures.append("implementation task missing or lacks copy/design dependencies")
    elif len(impl.assignment_history) != 1:
        failures.append(
            f"implementation was delegated {len(impl.assignment_history)} times; "
            "the early attempt must not create an assignment")
    accepted_index = {}
    impl_delegate_index = None
    for index, event in enumerate(events):
        if event.kind == "artifact.accepted":
            accepted_index.setdefault(event.data.get("artifact_id"), index)
        if (event.kind == "assignment.created"
                and event.data.get("assignment", {}).get("task_id") == "impl"):
            impl_delegate_index = index
    accepted_by_task = {
        task_id: run.tasks[task_id].artifact_ids[-1] for task_id in ("copy", "design")
        if run.tasks.get(task_id) and run.tasks[task_id].artifact_ids
    }
    if impl_delegate_index is None:
        failures.append("implementation was never delegated after its inputs were accepted")
    for task_id, artifact_id in accepted_by_task.items():
        if artifact_id not in accepted_index:
            failures.append(f"{task_id} artifact was never accepted")
        elif impl_delegate_index is not None and impl_delegate_index < accepted_index[artifact_id]:
            failures.append(f"implementation was delegated before {task_id} was accepted")
    return failures


# --- EVAL-003: Weak worker recovery ------------------------------------------

EVAL_003_CRITERIA = [
    "Deliverable accepted only after a substantive revision with review evidence",
]


def _eval_003_scripts(controller):
    manager = [
        _criteria_steps(controller, EVAL_003_CRITERIA),
        fakes.tool_step("plan_tasks", {
            "packets": [_packet("report", "Write the quarterly summary report",
                                "A complete quarterly summary report",
                                "Report covers all four required sections")],
            "capabilities": ["model_only"],
            "checks": [["result_schema"]],
        }, call_id="call-plan"),
        # Attempt 1: worker claims success with a hollow deliverable. Schema
        # validation passes (non-empty); the independent reviewer rejects it.
        fakes.tool_step("delegate_task", {"task_id": "report"}, call_id="call-delegate-1"),
        fakes.tool_step("validate_task", {"task_id": "report"}, call_id="call-validate-1"),
        fakes.tool_step("review_task", {"task_id": "report"}, call_id="call-review-1"),
        fakes.tool_step("recover_task", {
            "task_id": "report", "classification": "BAD_OUTPUT",
            "evidence": "Independent review found required sections missing",
            "reason": "Return targeted revision instructions to a fresh worker",
        }, call_id="call-recover-1"),
        # Attempt 2: worker honestly returns needs_revision; the adapter
        # classifies BAD_OUTPUT and recovers automatically.
        fakes.tool_step("delegate_task", {"task_id": "report"}, call_id="call-delegate-2"),
        # Attempt 3: substantive deliverable.
        fakes.tool_step("delegate_task", {"task_id": "report"}, call_id="call-delegate-3"),
        fakes.tool_step("validate_task", {"task_id": "report"}, call_id="call-validate-3"),
        fakes.tool_step("review_task", {"task_id": "report"}, call_id="call-review-3"),
        fakes.tool_step("accept_task", {"task_id": "report",
                                        "reason": "Substantive revision validated and reviewed"},
                        call_id="call-accept"),
        _finish_step(controller, {EVAL_003_CRITERIA[0]: "report"}),
        fakes.message_step("Run completed after bounded weak-worker recovery."),
    ]
    weak_claim = _worker_result("report", "Report: TBD.",
                                summary="Claimed complete with a hollow deliverable")
    review_fail = {"passed": False,
                   "evidence": ["Three of four required sections are missing"],
                   "reason": "Deliverable does not satisfy the acceptance criteria"}
    honest_blocker = {
        "task_id": "report", "status": "needs_revision",
        "summary": "Draft incomplete", "deliverable": "",
        "evidence": [], "blocker": "Sections three and four are still missing",
    }
    substantive = _worker_result(
        "report", "Quarterly summary: revenue, churn, pipeline, and outlook sections complete.")
    worker = [
        fakes.message_step(json.dumps(weak_claim)),
        fakes.message_step(json.dumps(review_fail)),
        fakes.message_step(json.dumps(honest_blocker)),
        fakes.message_step(json.dumps(substantive)),
        fakes.message_step(json.dumps(REVIEW_PASS)),
    ]
    return manager, worker


def _eval_003_check(run, events):
    failures = []
    if run.status != "completed":
        failures.append(f"run did not complete (status {run.status})")
    task = run.tasks.get("report")
    if task is None:
        return failures + ["report task missing"]
    if task.status != "ACCEPTED":
        failures.append(f"task not accepted ({task.status})")
    if len(task.assignment_history) < 3:
        failures.append(
            f"expected at least three bounded attempts, got {len(task.assignment_history)}")
    if task.revisions < 1:
        failures.append("weak output was never routed through revision recovery")
    if len(run.failures) < 2:
        failures.append("weak attempts were not classified as failures")
    artifact = run.artifacts[task.artifact_ids[-1]]
    if artifact.status != "accepted" or "TBD" in artifact.content:
        failures.append("the weak deliverable was accepted instead of the revision")
    kinds = [event.kind for event in events]
    for expected in ("failure.classified", "recovery.decided",
                     "artifact.rejected", "artifact.accepted"):
        if expected not in kinds:
            failures.append(f"missing event {expected}")
    return failures


SCENARIOS = {
    "EVAL-001": Scenario("EVAL-001", "Manager does not perform specialist work",
                         "Research the top five competitors and write positioning for my product.",
                         _eval_001_scripts, _eval_001_check),
    "EVAL-002": Scenario("EVAL-002", "Dependency gate",
                         "Build the website; implementation needs final approved copy and design.",
                         _eval_002_scripts, _eval_002_check),
    "EVAL-003": Scenario("EVAL-003", "Weak worker recovery",
                         "Produce the quarterly summary report.",
                         _eval_003_scripts, _eval_003_check),
}


def run_scenario(scenario: Scenario) -> list[str]:
    """Execute one scenario; return a list of pass-criteria failures."""
    set_tracing_disabled(True)
    with tempfile.TemporaryDirectory() as tmp:
        store = SQLiteStore(Path(tmp) / "operations.db")
        core = Orchestrator(store)
        run = core.create_run(scenario.objective, [INITIAL_COMPLETION_CRITERION])
        controller = DurableController(core, run.id)
        try:
            if LIVE:
                agent = runtime.build_caveman(controller)
                asyncio.run(Runner.run(agent, input=scenario.objective,
                                       max_turns=MAX_TURNS))
            else:
                manager_steps, worker_steps = scenario.build_scripts(controller)
                manager = fakes.scripted_model(manager_steps)
                worker = fakes.scripted_model(worker_steps)
                with mock.patch.object(
                        runtime.RuntimeConfig, "from_env",
                        classmethod(lambda cls: _offline_config())), \
                        mock.patch.object(
                            runtime, "build_models", lambda config: (manager, worker)):
                    agent = runtime.build_caveman(controller)
                    asyncio.run(Runner.run(agent, input=scenario.objective,
                                           max_turns=MAX_TURNS))
                manager.assert_complete()
                worker.assert_complete()
            return scenario.check(controller.inspect(), store.events(run.id))
        except Exception as exc:
            return [f"scenario raised {type(exc).__name__}: {exc}"]
        finally:
            controller.close()


def main(argv: list[str]) -> int:
    selected = argv or list(SCENARIOS)
    unknown = [name for name in selected if name not in SCENARIOS]
    if unknown:
        print(f"Unknown scenario(s): {', '.join(unknown)}; wired: {', '.join(SCENARIOS)}")
        return 2
    mode = "LIVE (provider credits)" if LIVE else "offline (scripted fakes)"
    print(f"Caveman eval runner — mode: {mode}")
    failed = 0
    for name in selected:
        scenario = SCENARIOS[name]
        reasons = run_scenario(scenario)
        if reasons:
            failed += 1
            print(f"{scenario.scenario_id} {scenario.title}: FAIL")
            for reason in reasons:
                print(f"  - {reason}")
        else:
            print(f"{scenario.scenario_id} {scenario.title}: PASS")
    print(f"{len(selected) - failed}/{len(selected)} scenarios passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
