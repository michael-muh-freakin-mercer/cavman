"""Manager-only control plane. Never expose this object as a worker tool.

Every public mutation reloads a snapshot and commits it with its events atomically.
Validation/review methods accept trusted executor evidence, never WorkerResult claims.
"""
from __future__ import annotations
import hashlib
import json
from typing import Callable
from .contracts import WorkerResult
from .models import (AcceptanceDecision, ApprovalDecision, ApprovalGate, ApprovalRequest, ApprovalStatus, Artifact,
    ArtifactValidation, BlockerReason, CapabilityProfile, CapabilityRequest, CapabilityRequestStatus, Event, FailureClass, ModelUsageRecord, RecoveryDecision,
    ReplanProposal, Review, Run, TaskNode, TaskStatus, WorkerAssignment, WorkerFailure,
    WorkPlan, now)
from .store import ConcurrentUpdate, SQLiteStore
from .usage import token_counts, usage_mapping


class GateError(ValueError):
    pass


# pytest = candidate-scoped alias of pytest_candidate (legacy name). The two
# named forms exist so a task can require the candidate's own tests, the
# pre-existing suite, or both (2026-09-21 decision).
# node_test runs the candidate's Node test files (node:test, TypeScript via type
# stripping); tsc type-checks the project with its own typescript dependency;
# npm_build runs the project's own `npm run build`.
EXECUTABLE_DEVELOPER_CHECKS = frozenset(
    {"compile", "pytest", "pytest_candidate", "pytest_regression", "node_test", "tsc", "npm_build"})

# Bounded retries for append-only usage accounting when another process commits
# to the same run between this load and save.
USAGE_RECORD_ATTEMPTS = 5

# A BLOCKED task carrying one of these reasons is waiting only on gates that
# _refresh can re-evaluate on its own. Any other reason -- classified failure
# evidence, a pending approval, an outstanding legacy gate -- needs an explicit
# Manager action before the task may return to READY.
REFRESHABLE_BLOCKERS = frozenset({
    BlockerReason.CAPABILITY_PREREQUISITES_MISSING,
    BlockerReason.APPROVAL_PREREQUISITES_SATISFIED,
})

# Work that could still produce output or is holding a worker/gate. A run with
# any task in these states is not abandonable offline: closing it would discard
# live work instead of recording a decision about it.
IN_FLIGHT_TASK_STATUSES = frozenset({
    TaskStatus.DELEGATED,
    TaskStatus.RUNNING,
    TaskStatus.SUBMITTED,
    TaskStatus.REVIEWING,
})


TRANSITIONS = {
    TaskStatus.PLANNED: {TaskStatus.READY, TaskStatus.BLOCKED},
    TaskStatus.READY: {TaskStatus.DELEGATED, TaskStatus.BLOCKED},
    TaskStatus.DELEGATED: {TaskStatus.RUNNING, TaskStatus.FAILED, TaskStatus.BLOCKED},
    TaskStatus.RUNNING: {TaskStatus.SUBMITTED, TaskStatus.FAILED, TaskStatus.BLOCKED},
    TaskStatus.SUBMITTED: {TaskStatus.REVIEWING, TaskStatus.FAILED},
    TaskStatus.REVIEWING: {TaskStatus.ACCEPTED, TaskStatus.REVISION_REQUIRED, TaskStatus.REPLACED, TaskStatus.FAILED},
    TaskStatus.REVISION_REQUIRED: {TaskStatus.DELEGATED, TaskStatus.BLOCKED},
    TaskStatus.BLOCKED: {TaskStatus.READY, TaskStatus.REPLACED},
    TaskStatus.FAILED: {TaskStatus.READY, TaskStatus.REVISION_REQUIRED, TaskStatus.REPLACED, TaskStatus.BLOCKED},
    TaskStatus.ACCEPTED: set(), TaskStatus.REPLACED: set(), TaskStatus.CANCELLED: set(),
}


def _scope(scope: dict) -> tuple[str, str]:
    value = json.dumps(scope, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return value, hashlib.sha256(value.encode()).hexdigest()


class Orchestrator:
    def __init__(self, store: SQLiteStore, *, manager_id: str = "manager"):
        if not manager_id.strip():
            raise ValueError("Configured manager authority must be substantive")
        self.store = store
        self.manager_id = manager_id

    def get_run(self, run_id: str) -> Run:
        return self.store.load(run_id)

    inspect = get_run

    @classmethod
    def _normalize_usage_record(cls, *, run_id: str, provider: str, model: str, role: str,
            task_id: str | None = None, assignment_id: str | None = None,
            worker_id: str | None = None, raw_usage: object | None = None) -> ModelUsageRecord:
        mapping = usage_mapping(raw_usage)
        input_tokens, output_tokens, total_tokens = token_counts(mapping)
        if input_tokens is None and output_tokens is None and total_tokens is None:
            usage_known = False
            reason = "Provider did not return token usage data"
        else:
            usage_known = True
            reason = None
        record = ModelUsageRecord(
            run_id=run_id,
            task_id=task_id,
            assignment_id=assignment_id,
            worker_id=worker_id,
            provider=provider,
            model=model,
            role=role,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            usage_known=usage_known,
            unknown_reason=reason,
            raw_usage=mapping,
        )
        return record

    def record_usage(self, run_id: str, *, record: ModelUsageRecord | None = None,
            provider: str = "openrouter", model: str = "unknown", role: str = "manager",
            task_id: str | None = None, assignment_id: str | None = None,
            worker_id: str | None = None, raw_usage: object | None = None) -> ModelUsageRecord:
        if record is None:
            record = self._normalize_usage_record(
                run_id=run_id,
                provider=provider,
                model=model,
                role=role,
                task_id=task_id,
                assignment_id=assignment_id,
                worker_id=worker_id,
                raw_usage=raw_usage,
            )
        else:
            record = record.model_copy(update={"run_id": run_id})

        # A final model response can arrive after finish_run completes the run.
        # Append accounting only; all operational mutations retain _mutate's gate.
        #
        # This row describes a provider call that has already happened, so losing
        # it understates the run's cost and weakens the pre-call budget. Appending
        # a record with a stable ID is commutative and idempotent, so a lost race
        # against another process is retried rather than surfaced as a failure of
        # the (already billed) model call.
        for remaining in reversed(range(USAGE_RECORD_ATTEMPTS)):
            try:
                with self.store.transaction():
                    run = self.get_run(run_id)
                    events = []
                    run.usage_records.append(record.model_copy(deep=True))
                    self._event(run, events, "model.usage.recorded", usage=record.model_dump(mode="json"))
                    self.store.save(run, events, run.version)
                break
            except ConcurrentUpdate:
                if not remaining:
                    raise
        return record.model_copy(deep=True)

    def _mutate(self, run_id: str, operation: Callable):
        with self.store.transaction():
            run = self.get_run(run_id)
            if run.status != "active":
                raise GateError("Run is terminal")
            events = []
            result = operation(run, events)
            self.store.save(run, events, run.version)
            return result

    @staticmethod
    def _event(run, events, kind, **data):
        events.append(Event(run_id=run.id, kind=kind, data=data))

    def _transition(self, run, events, task, status, reason):
        if status not in TRANSITIONS[task.status]:
            raise GateError(f"Illegal transition {task.status} -> {status}")
        previous = task.status
        task.status = status
        task.updated_at = now()
        self._event(run, events, "task."+status.value.lower(), task_id=task.id, previous=previous.value, reason=reason)

    @staticmethod
    def _validate_capability_checks(task: TaskNode, capability: CapabilityProfile | None = None):
        profile = capability or task.capability
        if (profile == CapabilityProfile.DEVELOPER_SANDBOX and
                not EXECUTABLE_DEVELOPER_CHECKS.intersection(task.required_checks)):
            raise GateError("Developer sandbox requires compile or pytest validation (or node_test/tsc for Node projects)")

    def create_run(self, objective: str, completion_criteria: list[str], *, constraints: list[str] | None = None, max_replans: int = 3) -> Run:
        if not objective.strip() or not all(x.strip() for x in completion_criteria):
            raise GateError("Objective and criteria must be substantive")
        run = Run(objective=objective, constraints=constraints or [], plan=WorkPlan(objective=objective, completion_criteria=completion_criteria, max_replans=max_replans))
        return self.store.save(run, [Event(run_id=run.id, kind="run.created")], None)

    @staticmethod
    def _graph(run):
        visiting, visited = set(), set()
        def visit(key):
            if key in visiting:
                raise GateError("Dependency cycle")
            if key in visited:
                return
            visiting.add(key)
            packet = run.tasks[key].packet
            for dep in [*packet.dependencies, *packet.required_inputs]:
                if dep in run.tasks:
                    if run.tasks[dep].status in {TaskStatus.CANCELLED, TaskStatus.REPLACED}:
                        raise GateError("Dependency references retired task")
                    visit(dep)
                elif dep in run.artifacts:
                    producer = run.artifacts[dep].task_id
                    if producer not in run.tasks:
                        raise GateError("Artifact references unknown producer")
                    if run.tasks[producer].status not in {TaskStatus.CANCELLED, TaskStatus.REPLACED}:
                        visit(producer)
                elif dep in packet.dependencies:
                    raise GateError(f"Unknown dependency {dep}")
            visiting.remove(key)
            visited.add(key)
        for key, task in run.tasks.items():
            if task.status not in {TaskStatus.CANCELLED, TaskStatus.REPLACED}:
                visit(key)

    def _input_artifact_ids(self, run, task, visiting=None, checked=None):
        """Resolve declared inputs and verify their complete canonical lineage."""
        visiting = set() if visiting is None else visiting
        checked = set() if checked is None else checked
        inputs = []
        for value in [*task.packet.dependencies, *task.packet.required_inputs]:
            if value in run.tasks:
                upstream = run.tasks[value]
                if upstream.status != TaskStatus.ACCEPTED or not upstream.artifact_ids:
                    raise GateError(f"Input task is not accepted: {value} ({upstream.status.value})")
                artifact_id = upstream.artifact_ids[-1]
            elif value in run.artifacts:
                artifact_id = value
            elif value in run.available_inputs and value not in task.packet.dependencies:
                continue
            else:
                raise GateError(f"Required input is unavailable: {value}")
            self._validate_accepted_artifact(run, artifact_id, visiting, checked)
            if artifact_id not in inputs:
                inputs.append(artifact_id)
        return inputs

    def _validate_accepted_artifact(self, run, artifact_id, visiting=None, checked=None):
        visiting = set() if visiting is None else visiting
        checked = set() if checked is None else checked
        if artifact_id in visiting:
            raise GateError("Artifact input cycle")
        if artifact_id in checked:
            return
        artifact = run.artifacts.get(artifact_id)
        task = run.tasks.get(artifact.task_id) if artifact else None
        if (not artifact or not task or artifact.run_id != run.id or
                artifact.status != "accepted" or artifact_id not in run.accepted_artifacts or
                task.status != TaskStatus.ACCEPTED or task.artifact_ids[-1:] != [artifact_id]):
            raise GateError("Input artifact is no longer canonical and accepted")
        visiting.add(artifact_id)
        inputs = self._input_artifact_ids(run, task, visiting, checked)
        if set(artifact.input_artifact_ids) != set(inputs):
            raise GateError("Artifact input provenance does not match declared inputs")
        visiting.remove(artifact_id)
        checked.add(artifact_id)

    def _validate_task_inputs(self, run, task):
        inputs = self._input_artifact_ids(run, task)
        if not task.artifact_ids or set(run.artifacts[task.artifact_ids[-1]].input_artifact_ids) != set(inputs):
            raise GateError("Candidate input provenance does not match declared inputs")

    def _readiness_blocker(self, run, task) -> str | None:
        """Precise reason a task is not delegatable, or None when it is ready."""
        try:
            self._input_artifact_ids(run, task)
        except GateError as exc:
            return str(exc)
        if task.capability == CapabilityProfile.DEVELOPER_SANDBOX and not task.workspace_id:
            return "developer sandbox task has no bound workspace"
        if task.approval_ids:
            return "task has unresolved approval gates"
        for gate in task.approval_gates:
            request = run.approvals.get(gate.request_id)
            decision = run.approval_decisions.get(gate.request_id)
            if (not request or request.status != ApprovalStatus.APPROVED or not decision or
                    not decision.approved or request.action != gate.action or
                    request.scope_digest != gate.scope_digest or request.scope_json != gate.scope_json):
                return f"approval gate {gate.request_id} is not exactly approved"
        if not task.packet.acceptance_criteria:
            return "task packet has no acceptance criteria"
        return None

    def _ready(self, run, task):
        return self._readiness_blocker(run, task) is None

    @staticmethod
    def _refreshable_blocker(task) -> bool:
        """Whether _refresh may return this BLOCKED task to READY on its own.

        A readiness-lapsed blocker carries the precise reason as free text after
        a fixed prefix, so it is matched by prefix rather than by set membership.
        """
        blocker = task.blocker or ""
        return (blocker in REFRESHABLE_BLOCKERS
                or blocker.startswith(BlockerReason.READINESS_LAPSED))

    def _refresh(self, run, events):
        for task in run.tasks.values():
            if task.status == TaskStatus.PLANNED and self._ready(run, task):
                self._transition(run, events, task, TaskStatus.READY, "Dependencies and input gates satisfied")
            elif (task.status == TaskStatus.BLOCKED and self._refreshable_blocker(task)
                    and self._ready(run, task)):
                task.blocker = None
                self._transition(run, events, task, TaskStatus.READY, "Capability prerequisites satisfied")
            elif task.status == TaskStatus.READY:
                # Only promoting left the snapshot able to advertise readiness
                # that no longer holds: delegate() refused the task while
                # inspect_run still reported READY. Demote with the precise
                # reason instead. In-flight, terminal and revision states are
                # untouched -- their gates are re-checked at their own
                # transitions, and demoting active work would discard it.
                lapsed = self._readiness_blocker(run, task)
                if lapsed:
                    self._transition(run, events, task, TaskStatus.BLOCKED, lapsed)
                    task.blocker = BlockerReason.READINESS_LAPSED + ": " + lapsed

    def set_completion_criteria(self, run_id: str, criteria: list[str]):
        def operation(run, events):
            if not criteria or not all(c.strip() for c in criteria) or any(t.attempts for t in run.tasks.values()):
                raise GateError("Completion criteria must be defined before execution")
            run.plan.completion_criteria = list(criteria)
            self._event(run, events, "plan.criteria_defined", criteria=criteria)
        self._mutate(run_id, operation)

    def resume(self, run_id: str) -> Run:
        """Explicit interruption recovery, without automatically rerunning a worker."""
        def operation(run, events):
            for task in run.tasks.values():
                if task.status in {TaskStatus.DELEGATED, TaskStatus.RUNNING}:
                    failure = WorkerFailure(task_id=task.id, classification=FailureClass.TIMEOUT, evidence="Assignment interrupted before durable submission")
                    run.failures.append(failure)
                    self._transition(run, events, task, TaskStatus.FAILED, failure.evidence)
                    task.blocker = failure.evidence
                    self._event(run, events, "failure.classified", failure=failure.model_dump(mode="json"))
                    if task.assignment:
                        self._event(run, events, "assignment.failed", assignment_id=task.assignment.id,
                            task_id=task.id, reason=failure.evidence)
            self._event(run, events, "run.resumed", reason="Operator resumed durable run")
        self._mutate(run_id, operation)
        return self.get_run(run_id)

    def abandon(self, run_id: str, reason: str, *, actor_id: str | None = None) -> Run:
        """Close a work-free active run offline, without spending provider calls.

        `resume` only converts interrupted DELEGATED/RUNNING work to FAILED, and
        every other route to a terminal run either invokes the Manager model or
        demands accepted artifacts. A run whose plan holds no in-flight work and
        no pending gate therefore had no offline exit and stayed `active`
        forever, polluting run listings and operator audits (2026-09-22 decision).

        Abandonment is deliberately narrow. Any in-flight task, pending approval,
        or pending capability request refuses it, because those need an explicit
        decision rather than a silent close. Accepted work, failed tasks and
        recorded unresolved issues are preserved as history, not erased; the run
        and its append-only events stay fully readable.
        """
        if not reason.strip():
            raise GateError("Abandonment requires an audit-worthy reason")
        def operation(run, events):
            in_flight = sorted(key for key, task in run.tasks.items() if task.status in IN_FLIGHT_TASK_STATUSES)
            if in_flight:
                raise GateError("Run still has in-flight work: " + ", ".join(in_flight))
            pending = sorted(request.id for request in run.approvals.values()
                             if request.status == ApprovalStatus.PENDING)
            if pending:
                raise GateError("Run still has pending approval gates: " + ", ".join(pending))
            pending_capability = sorted(request.id for request in run.capability_requests.values()
                                        if request.status == CapabilityRequestStatus.PENDING)
            if pending_capability:
                raise GateError("Run still has pending capability requests: " + ", ".join(pending_capability))
            run.status, run.plan.status = "abandoned", "abandoned"
            self._event(run, events, "run.abandoned", reason=reason, actor_id=actor_id,
                        unresolved_issues=list(run.unresolved_issues))
        self._mutate(run_id, operation)
        return self.get_run(run_id)

    @staticmethod
    def _reject_identifier_collision(run, task_id: str):
        """Task IDs, input names and artifact IDs share one reference namespace.

        ``_input_artifact_ids`` resolves a declared reference against tasks
        first, so introducing a task whose ID equals a registered input name
        silently retargets every packet that already declared that name. The
        consumer keeps its READY status while delegation starts failing, so the
        collision is refused instead. ``register_input`` already guards the
        opposite direction.
        """
        if task_id in run.available_inputs:
            raise GateError(f"Task ID collides with a registered input name: {task_id}")
        if task_id in run.artifacts:
            raise GateError(f"Task ID collides with an artifact ID: {task_id}")

    @staticmethod
    def _is_fresh_task(run, task) -> bool:
        """A task that carries no execution history and collides with no existing task."""
        return not (task.id in run.tasks or task.status != TaskStatus.PLANNED or task.attempts
                    or task.artifact_ids or task.assignment or task.attempt_baseline
                    or task.revision_baseline)

    def add_tasks(self, run_id: str, tasks: list[TaskNode]):
        def operation(run, events):
            if run.plan.revision or any(t.attempts for t in run.tasks.values()):
                raise GateError("Use explicit replan after execution begins")
            for source in tasks:
                task = source.model_copy(deep=True)
                if not Orchestrator._is_fresh_task(run, task):
                    raise GateError("Only fresh unique tasks can be added")
                self._reject_identifier_collision(run, task.id)
                self._validate_capability_checks(task)
                run.tasks[task.id] = task
                run.plan.task_ids.append(task.id)
                self._event(run, events, "task.created", task_id=task.id)
            self._graph(run)
            self._refresh(run, events)
        self._mutate(run_id, operation)

    plan = add_tasks

    def register_input(self, run_id: str, name: str, reference: str):
        def operation(run, events):
            if not reference.strip() or name in run.available_inputs or name in run.tasks or name in run.artifacts:
                raise GateError("Input reference must be nonempty and immutable")
            run.available_inputs[name] = reference
            self._event(run, events, "input.registered", name=name, reference=reference)
            self._refresh(run, events)
        self._mutate(run_id, operation)

    def bind_workspace(self, run_id: str, task_id: str, workspace_id: str):
        """Bind initial trusted sandbox grant; rebinding needs a capability approval."""
        def operation(run, events):
            task = run.tasks[task_id]
            if task.workspace_id or task.attempts or task.status not in {TaskStatus.PLANNED, TaskStatus.READY, TaskStatus.BLOCKED} or not workspace_id.strip():
                raise GateError("Workspace binding must be initial and precede delegation")
            if task.status == TaskStatus.BLOCKED and task.blocker != BlockerReason.CAPABILITY_PREREQUISITES_MISSING:
                raise GateError("Workspace cannot resolve this blocker")
            task.workspace_id = workspace_id
            self._event(run, events, "workspace.bound", task_id=task_id, workspace_id=workspace_id)
            self._refresh(run, events)
        self._mutate(run_id, operation)

    def replace_workspace(self, run_id: str, task_id: str, expected_workspace_id: str,
            new_workspace_id: str, reason: str):
        """Audit a Manager-created replacement sandbox after explicit task recovery."""
        def operation(run, events):
            task = run.tasks[task_id]
            if task.capability != CapabilityProfile.DEVELOPER_SANDBOX:
                raise GateError("Workspace replacement requires a developer sandbox task")
            if task.status not in {TaskStatus.READY, TaskStatus.REVISION_REQUIRED} or not task.attempts:
                raise GateError("Workspace can only be replaced after recovery in a safe retry state")
            if task.workspace_id != expected_workspace_id:
                raise GateError("Expected old workspace does not match persisted task state")
            if (not new_workspace_id.strip() or new_workspace_id == expected_workspace_id or
                    not reason.strip()):
                raise GateError("Replacement workspace and reason must be substantive")
            if task.artifact_ids and run.artifacts[task.artifact_ids[-1]].status == "candidate":
                raise GateError("Current candidate must be rejected through failure recovery first")
            old_workspace_id = task.workspace_id
            task.workspace_id = new_workspace_id
            task.updated_at = now()
            self._event(run, events, "workspace.replaced", task_id=task_id,
                old_workspace_id=old_workspace_id, new_workspace_id=new_workspace_id,
                reason=reason)
        self._mutate(run_id, operation)

    def reload(self, run_id: str) -> Run:
        """Load durable state; in-flight assignments stay in-flight until explicit recovery."""
        return self.get_run(run_id)

    def delegate(self, run_id: str, task_id: str, worker_id: str) -> WorkerAssignment:
        def operation(run, events):
            task = run.tasks[task_id]
            if not worker_id.strip() or worker_id == self.manager_id:
                raise GateError("Worker identity is not delegatable")
            if task.attempts - task.attempt_baseline >= task.max_attempts:
                raise GateError(
                    f"Attempt budget exhausted "
                    f"({task.attempts - task.attempt_baseline}/{task.max_attempts})")
            blocker = self._readiness_blocker(run, task)
            if blocker:
                raise GateError(f"Task is not ready: {blocker}")
            if task.status not in {TaskStatus.READY, TaskStatus.REVISION_REQUIRED}:
                raise GateError("Task is not delegatable")
            assignment = WorkerAssignment(worker_id=worker_id, task_id=task_id, capability=task.capability, workspace_id=task.workspace_id)
            self._transition(run, events, task, TaskStatus.DELEGATED, "Manager delegation")
            task.assignment = assignment
            task.assignment_history.append(assignment)
            task.attempts += 1
            self._event(run, events, "assignment.created", assignment=assignment.model_dump(mode="json"), attempt=task.attempts)
            return assignment
        return self._mutate(run_id, operation)

    def start(self, run_id: str, task_id: str):
        def operation(run, events):
            task = run.tasks[task_id]
            if not self._ready(run, task):
                raise GateError("Execution gates no longer satisfied")
            self._transition(run, events, task, TaskStatus.RUNNING, "Worker started")
            self._event(run, events, "assignment.started", assignment_id=task.assignment.id,
                task_id=task.id, worker_id=task.assignment.worker_id)
        self._mutate(run_id, operation)

    def submit(self, run_id: str, task_id: str, assignment_id: str, worker_id: str,
            result: WorkerResult, *, workspace_fingerprint: str | None = None) -> Artifact:
        def operation(run, events):
            task = run.tasks[task_id]
            if (not task.assignment or task.assignment.id != assignment_id or
                    task.assignment.worker_id != worker_id):
                raise GateError("Submission does not match the current worker assignment")
            if result.task_id != task_id or result.status != "completed" or not result.deliverable.strip():
                raise GateError("Expected matching completed candidate; route blockers through fail")
            inputs = self._input_artifact_ids(run, task)
            self._transition(run, events, task, TaskStatus.SUBMITTED, "Candidate received")
            if task.capability == CapabilityProfile.DEVELOPER_SANDBOX and not workspace_fingerprint:
                raise GateError("Developer candidate requires observed workspace fingerprint")
            artifact = Artifact(run_id=run_id, task_id=task_id, worker_id=worker_id, content=result.deliverable, content_digest=hashlib.sha256(result.deliverable.encode()).hexdigest(), workspace_fingerprint=workspace_fingerprint, version=len(task.artifact_ids) + 1, predecessor_id=task.artifact_ids[-1] if task.artifact_ids else None, input_artifact_ids=inputs)
            task.result = result.model_copy(deep=True)
            task.artifact_ids.append(artifact.id)
            run.artifacts[artifact.id] = artifact
            self._event(run, events, "artifact.created", artifact=artifact.model_dump(mode="json"))
            self._event(run, events, "artifact.submitted", artifact_id=artifact.id, task_id=task_id, version=artifact.version)
            self._event(run, events, "assignment.completed", assignment_id=task.assignment.id,
                task_id=task.id, artifact_id=artifact.id)
            return artifact.model_copy(deep=True)
        return self._mutate(run_id, operation)

    @staticmethod
    def _provisional_result(task, assignment_id: str, worker_id: str, result: WorkerResult):
        if (task.status != TaskStatus.RUNNING or not task.assignment or
                task.assignment.id != assignment_id or task.assignment.worker_id != worker_id):
            raise GateError("Provisional result does not match the current active assignment")
        if result.task_id != task.id or result.status not in {"blocked", "needs_revision"}:
            raise GateError("Expected a matching blocked or needs_revision result")
        task.result = result.model_copy(deep=True)
        task.blocker = result.blocker or result.summary
        task.updated_at = now()

    def record_provisional_result(self, run_id: str, task_id: str, assignment_id: str,
            worker_id: str, result: WorkerResult):
        """Preserve partial worker output before trusted failure classification."""
        def operation(run, events):
            task = run.tasks[task_id]
            self._provisional_result(task, assignment_id, worker_id, result)
            self._event(run, events, "assignment.result_provisional", task_id=task_id,
                assignment_id=assignment_id, worker_id=worker_id,
                result=result.model_dump(mode="json"))
        self._mutate(run_id, operation)

    def record_capability_request(self, run_id: str, task_id: str, assignment_id: str,
            worker_id: str, result: WorkerResult) -> CapabilityRequest:
        """Persist an untrusted worker request without granting capability."""
        def operation(run, events):
            task = run.tasks[task_id]
            self._provisional_result(task, assignment_id, worker_id, result)
            payload = result.capability_request
            if payload is None:
                raise GateError("Structured capability request required")
            requested = CapabilityProfile(payload.requested_capability)
            if requested == task.capability:
                raise GateError("Requested capability is already assigned")
            request = CapabilityRequest(run_id=run.id, task_id=task_id,
                assignment_id=assignment_id, worker_id=worker_id,
                requested_capability=requested, reason=payload.reason, risk=payload.risk)
            run.capability_requests[request.id] = request
            self._event(run, events, "capability.requested",
                request=request.model_dump(mode="json"),
                provisional_result=result.model_dump(mode="json"))
            return request.model_copy(deep=True)
        return self._mutate(run_id, operation)

    def _candidate(self, run, events, artifact_id, workspace_fingerprint=None):
        artifact = run.artifacts[artifact_id]
        task = run.tasks[artifact.task_id]
        if hashlib.sha256(artifact.content.encode()).hexdigest() != artifact.content_digest:
            raise GateError("Candidate content changed")
        if artifact.workspace_fingerprint and artifact.workspace_fingerprint != workspace_fingerprint:
            raise GateError("Current workspace fingerprint must match candidate")
        if task.artifact_ids[-1] != artifact_id or artifact.status != "candidate":
            raise GateError("Not the current candidate")
        if task.status == TaskStatus.SUBMITTED:
            self._transition(run, events, task, TaskStatus.REVIEWING, "Manager QA started")
        if task.status != TaskStatus.REVIEWING:
            raise GateError("Task is not reviewing")
        return artifact, task

    def validate(self, run_id: str, artifact_id: str, check: str, passed: bool, evidence: str, validator_id: str, *, workspace_fingerprint: str | None = None):
        def operation(run, events):
            artifact, task = self._candidate(run, events, artifact_id, workspace_fingerprint)
            if validator_id == self.manager_id or validator_id in {a.worker_id for a in task.assignment_history}:
                raise GateError("Author cannot validate own candidate")
            record = ArtifactValidation(artifact_id=artifact_id, content_digest=artifact.content_digest, check=check, passed=passed, evidence=evidence, validator_id=validator_id)
            self._event(run, events, "artifact.validation_started", artifact_id=artifact_id,
                check=check, validator_id=validator_id)
            artifact.validations.append(record)
            self._event(run, events, "artifact.validation_completed", validation=record.model_dump())
            return record
        return self._mutate(run_id, operation)

    def review(self, run_id: str, artifact_id: str, reviewer_id: str, passed: bool, evidence: str, *, workspace_fingerprint: str | None = None):
        def operation(run, events):
            artifact, task = self._candidate(run, events, artifact_id, workspace_fingerprint)
            if reviewer_id == self.manager_id or reviewer_id in {a.worker_id for a in task.assignment_history}:
                raise GateError("Reviewer must be independent of all candidate authors")
            record = Review(artifact_id=artifact_id, content_digest=artifact.content_digest, reviewer_id=reviewer_id, passed=passed, evidence=evidence)
            artifact.reviews.append(record)
            self._event(run, events, "artifact.reviewed", review=record.model_dump())
            return record
        return self._mutate(run_id, operation)

    def accept(self, run_id: str, task_id: str, manager_id: str | None = None, reason: str = "", *, workspace_fingerprint: str | None = None):
        """Accept using configured Manager authority; caller identity is never trusted."""
        if manager_id is not None and manager_id != self.manager_id:
            raise GateError("Acceptance authority does not match configured Manager")
        def operation(run, events):
            task = run.tasks[task_id]
            if not task.artifact_ids:
                raise GateError("No candidate")
            artifact, task = self._candidate(run, events, task.artifact_ids[-1], workspace_fingerprint)
            evidence_principals = ({a.worker_id for a in task.assignment_history} |
                {v.validator_id for v in artifact.validations} | {r.reviewer_id for r in artifact.reviews})
            if self.manager_id in evidence_principals:
                raise GateError("Author cannot accept own output")
            if any(v.content_digest != artifact.content_digest for v in [*artifact.validations, *artifact.reviews]):
                raise GateError("Evidence does not match candidate content")
            # Strict evidence rule: a recorded failure on the current content
            # bytes permanently blocks that candidate. A genuine environment
            # problem raises SandboxUnavailable rather than recording a
            # failure, so every recorded failure is a real or flaky result —
            # and flaky tolerance is not worth the evidence-laundering risk of
            # letting a later pass supersede it (DECISION 2026-09-21). The fix
            # for a failed check is a revised candidate, not a re-run.
            failed = sorted({v.check for v in artifact.validations if not v.passed})
            missing = sorted(set(task.required_checks) - {v.check for v in artifact.validations})
            if failed or missing:
                raise GateError("Validation gates failed"
                    + (f"; failed: {', '.join(failed)}" if failed else "")
                    + (f"; missing: {', '.join(missing)}" if missing else ""))
            if (task.review_required or task.high_risk or task.capability == CapabilityProfile.DEVELOPER_SANDBOX) and (not artifact.reviews or not artifact.reviews[-1].passed):
                raise GateError("Independent review required")
            if not task.required_checks and not artifact.reviews:
                raise GateError("At least one external validation or review is required")
            if not self._ready(run, task):
                raise GateError("Inputs are no longer accepted")
            self._validate_task_inputs(run, task)
            decision = AcceptanceDecision(action="ACCEPT", reason=reason, actor_id=self.manager_id,
                context=f"Acceptance of artifact {artifact.id} for task {task_id}",
                options_considered=["ACCEPT", "REVISE", "REJECT", "REPLACE"],
                consequences=["Artifact enters canonical state", "Accepted dependencies may become ready"],
                artifact_id=artifact.id, affected_ids=[task_id], evidence=[v.id for v in artifact.validations]+[r.id for r in artifact.reviews])
            run.acceptances.append(decision)
            run.decisions.append(decision)
            artifact.status = "accepted"
            run.accepted_artifacts.append(artifact.id)
            self._transition(run, events, task, TaskStatus.ACCEPTED, reason)
            self._event(run, events, "artifact.accepted", artifact_id=artifact.id,
                decision_id=decision.id, superseded_failed_checks=[])
            self._refresh(run, events)
            return decision
        return self._mutate(run_id, operation)

    def record_integration(self, run_id: str, artifact_id: str, commit: str):
        """Record that trusted code integrated this accepted candidate at ``commit``.

        Only the current canonical accepted workspace artifact of an accepted
        task can be integrated, once. Re-recording the same commit is a no-op
        so an interrupted integration can be completed idempotently.
        """
        if not commit or len(commit) not in (40, 64) or any(c not in "0123456789abcdef" for c in commit):
            raise GateError("Integration commit must be a full object id")
        existing = self.get_run(run_id).artifacts.get(artifact_id)
        if existing is not None and existing.integrated_commit == commit:
            return  # already recorded; nothing to append
        def operation(run, events):
            artifact = run.artifacts.get(artifact_id)
            task = run.tasks.get(artifact.task_id) if artifact else None
            if (not artifact or not task or artifact.status != "accepted" or
                    artifact_id not in run.accepted_artifacts or task.status != TaskStatus.ACCEPTED or
                    task.artifact_ids[-1:] != [artifact_id] or not artifact.workspace_fingerprint):
                raise GateError("Only the canonical accepted workspace artifact can be integrated")
            if artifact.integrated_commit is not None:
                raise GateError("Artifact is already integrated at a different commit")
            artifact.integrated_commit = commit
            self._event(run, events, "artifact.integrated", artifact_id=artifact_id,
                        task_id=task.id, commit=commit)
        self._mutate(run_id, operation)

    def _fail(self, run, events, task, classification: FailureClass, evidence: str) -> WorkerFailure:
        failure = WorkerFailure(task_id=task.id, classification=classification, evidence=evidence)
        self._transition(run, events, task, TaskStatus.FAILED, evidence)
        task.blocker = evidence
        run.failures.append(failure)
        self._event(run, events, "failure.classified", failure=failure.model_dump(mode="json"))
        if task.assignment:
            self._event(run, events, "assignment.failed", assignment_id=task.assignment.id,
                task_id=task.id, reason=evidence)
        return failure

    def fail(self, run_id: str, task_id: str, classification: FailureClass, evidence: str) -> WorkerFailure:
        """Record a trusted Manager/system classification outside a worker callback."""
        def operation(run, events):
            return self._fail(run, events, run.tasks[task_id], classification, evidence)
        return self._mutate(run_id, operation)

    def fail_assignment(self, run_id: str, task_id: str, assignment_id: str, worker_id: str,
            classification: FailureClass, evidence: str) -> WorkerFailure:
        """Record worker execution failure only for the exact current assignment."""
        def operation(run, events):
            task = run.tasks[task_id]
            if (not task.assignment or task.assignment.id != assignment_id or
                    task.assignment.worker_id != worker_id):
                raise GateError("Failure does not match the current worker assignment")
            return self._fail(run, events, task, classification, evidence)
        return self._mutate(run_id, operation)

    def recover(self, run_id: str, failure_id: str, reason: str, *, actor_id: str = "manager") -> RecoveryDecision:
        if actor_id not in {"manager", self.manager_id}:
            raise GateError("Recovery authority does not match configured Manager")
        def operation(run, events):
            failure = next((f for f in run.failures if f.id == failure_id), None)
            if failure is None:
                raise GateError(f"Unknown failure {failure_id}")
            task = run.tasks[failure.task_id]
            if any(r.failure_id == failure_id for r in run.recoveries) or task.status != TaskStatus.FAILED:
                raise GateError("Failure already recovered or superseded")
            kind = failure.classification
            action = "REPLAN"
            if kind in {FailureClass.PROVIDER_FAILURE, FailureClass.TIMEOUT, FailureClass.STALE_BASE}:
                action = "RETRY"
            elif kind in {FailureClass.BAD_OUTPUT, FailureClass.MISSING_EVIDENCE}:
                action = "REVISE"
            elif kind in {FailureClass.REPEATED_BAD_OUTPUT, FailureClass.CONSTRAINT_VIOLATION}:
                action = "REPLACE"
            elif kind in {FailureClass.CAPABILITY_UNAVAILABLE, FailureClass.UNSUPPORTED_CAPABILITY, FailureClass.TOOL_FAILURE}:
                action = "ESCALATE"
            if action in {"RETRY", "REVISE"} and task.attempts - task.attempt_baseline >= task.max_attempts:
                action = "REPLAN"
            if action == "REVISE" and task.revisions - task.revision_baseline >= task.max_revisions:
                action = "REPLACE"
            if action == "RETRY":
                if not self._ready(run, task):
                    raise GateError("Retry gates unresolved")
                self._transition(run, events, task, TaskStatus.READY, reason)
                task.blocker = None
                self._event(run, events, "retry.scheduled", task_id=task.id, failure_id=failure.id, next_attempt=task.attempts + 1)
            elif action == "REVISE":
                task.revisions += 1
                self._transition(run, events, task, TaskStatus.REVISION_REQUIRED, reason)
                task.blocker = failure.evidence
            elif action == "REPLACE":
                self._transition(run, events, task, TaskStatus.REPLACED, reason)
                task.blocker = BlockerReason.WORKER_REPLACEMENT_REQUIRED
                self._event(run, events, "worker.replaced", task_id=task.id, failure_id=failure.id,
                    worker_id=task.assignment.worker_id if task.assignment else None)
            elif action == "ESCALATE":
                self._transition(run, events, task, TaskStatus.BLOCKED, reason)
                task.blocker = {
                    FailureClass.CAPABILITY_UNAVAILABLE: BlockerReason.CAPABILITY_ESCALATION_PENDING,
                    FailureClass.UNSUPPORTED_CAPABILITY: BlockerReason.UNSUPPORTED_CAPABILITY,
                    FailureClass.TOOL_FAILURE: BlockerReason.TOOL_FAILURE,
                }[kind]
            elif action == "REPLAN":
                self._transition(run, events, task, TaskStatus.BLOCKED, reason)
                task.blocker = BlockerReason.MANAGER_REPLAN_REQUIRED
            if task.artifact_ids:
                rejected = run.artifacts[task.artifact_ids[-1]]
                rejected.status = "rejected"
                self._event(run, events, "artifact.rejected", artifact_id=rejected.id, reason=reason, failure_id=failure.id)
            decision = RecoveryDecision(failure_id=failure_id, action=action, reason=reason, actor_id=self.manager_id,
                context=f"Recovery from {failure.classification.value} on task {task.id}",
                options_considered=["RETRY", "REVISE", "REPLACE", "ESCALATE", "REPLAN"],
                consequences=[f"Task recovery action: {action}"], affected_ids=[task.id], evidence=[failure.evidence])
            run.recoveries.append(decision)
            run.decisions.append(decision)
            self._event(run, events, "recovery.decided", decision=decision.model_dump())
            return decision
        return self._mutate(run_id, operation)

    def request_approval(self, run_id: str, action: str, scope: dict, reason: str, *,
            category: str | None = None, target: str | None = None, risk: str = "material action",
            artifact_refs: list[str] | None = None, requested_capability: CapabilityProfile | None = None,
            required: bool = True, supersedes: str | None = None) -> ApprovalRequest:
        def operation(run, events):
            value, digest = _scope(scope)
            request = ApprovalRequest(run_id=run.id, category=category or action, action=action,
                scope_json=value, scope_digest=digest, target=target or str(scope.get("target") or scope.get("task_id") or "run"),
                rationale=reason, risk=risk, artifact_refs=artifact_refs or [],
                requested_capability=requested_capability, required=required)
            if supersedes is not None:
                prior = run.approvals.get(supersedes)
                if not prior or prior.status == ApprovalStatus.SUPERSEDED:
                    raise GateError("Approval is missing or already superseded")
                active = [task.id for task in run.tasks.values()
                    if task.status in IN_FLIGHT_TASK_STATUSES
                    and any(gate.request_id == supersedes for gate in task.approval_gates)]
                if active:
                    raise GateError("Cannot supersede approval while bound work is active; fail and recover the task first")
                timestamp = now()
                run.approvals[supersedes] = prior.model_copy(update={"status": ApprovalStatus.SUPERSEDED,
                    "updated_at": timestamp, "superseded_at": timestamp, "replacement_id": request.id})
                self._event(run, events, "approval.superseded", approval_id=supersedes, replacement_id=request.id)
                for task in run.tasks.values():
                    for index, gate in enumerate(task.approval_gates):
                        if gate.request_id == supersedes:
                            replacement_gate = ApprovalGate(request_id=request.id, action=request.action,
                                scope_json=request.scope_json, scope_digest=request.scope_digest)
                            task.approval_gates[index] = replacement_gate
                            if task.status == TaskStatus.READY:
                                self._transition(run, events, task, TaskStatus.BLOCKED, "Replacement approval pending")
                            task.blocker = BlockerReason.APPROVAL_PENDING
                            self._event(run, events, "task.approval_gate_replaced", task_id=task.id,
                                prior_approval_id=supersedes, gate=replacement_gate.model_dump(mode="json"))
            run.approvals[request.id] = request
            self._event(run, events, "approval.required", request=request.model_dump(mode="json"))
            return request
        return self._mutate(run_id, operation)

    def recover_legacy_approval_gate(self, run_id: str, task_id: str, legacy_approval_id: str,
            approval_id: str, action: str, scope: dict):
        """Replace one unresolved schema-v1 bare approval ID with an exact typed gate."""
        def operation(run, events):
            task = run.tasks[task_id]
            if legacy_approval_id not in task.approval_ids or task.status not in {
                    TaskStatus.PLANNED, TaskStatus.READY, TaskStatus.BLOCKED}:
                raise GateError("Task has no recoverable legacy approval gate in a safe state")
            request = run.approvals.get(approval_id)
            value, digest = _scope(scope)
            if (not request or request.status == ApprovalStatus.SUPERSEDED or
                    request.action != action or request.scope_json != value or
                    request.scope_digest != digest):
                raise GateError("Recovery gate must match the exact approval request")
            if any(gate.request_id == approval_id for gate in task.approval_gates):
                raise GateError("Approval gate already attached")
            gate = ApprovalGate(request_id=approval_id, action=action,
                scope_json=value, scope_digest=digest)
            task.approval_gates.append(gate)
            task.approval_ids.remove(legacy_approval_id)
            if task.approval_ids:
                # Free text: the outstanding IDs are part of the operator-facing reason.
                task.blocker = (BlockerReason.LEGACY_APPROVAL_REGATE + ": " +
                    ", ".join(task.approval_ids))
            elif request.status == ApprovalStatus.APPROVED:
                task.blocker = (BlockerReason.CAPABILITY_PREREQUISITES_MISSING if
                    task.capability == CapabilityProfile.DEVELOPER_SANDBOX and not task.workspace_id
                    else BlockerReason.APPROVAL_PREREQUISITES_SATISFIED)
            else:
                task.blocker = (BlockerReason.APPROVAL_REJECTED if request.status == ApprovalStatus.REJECTED
                    else BlockerReason.APPROVAL_PENDING)
            self._event(run, events, "task.legacy_approval_gate_recovered", task_id=task_id,
                legacy_approval_id=legacy_approval_id, gate=gate.model_dump(mode="json"))
            self._refresh(run, events)
        self._mutate(run_id, operation)

    def gate_task(self, run_id: str, task_id: str, approval_id: str, action: str, scope: dict):
        """Bind a task gate to an exact typed approval action and scope."""
        def operation(run, events):
            task = run.tasks[task_id]
            if task.status not in {TaskStatus.PLANNED, TaskStatus.READY, TaskStatus.BLOCKED} or task.attempts:
                raise GateError("Approval gates must be attached before delegation")
            request = run.approvals.get(approval_id)
            value, digest = _scope(scope)
            if (not request or request.status == ApprovalStatus.SUPERSEDED or
                    request.action != action or request.scope_digest != digest or request.scope_json != value):
                raise GateError("Task gate must match the exact approval request")
            gate = ApprovalGate(request_id=approval_id, action=action, scope_json=value, scope_digest=digest)
            if any(existing.request_id == approval_id for existing in task.approval_gates):
                raise GateError("Approval gate already attached")
            task.approval_gates.append(gate)
            if task.status == TaskStatus.READY and request.status != ApprovalStatus.APPROVED:
                self._transition(run, events, task, TaskStatus.BLOCKED, BlockerReason.APPROVAL_PENDING)
                task.blocker = (BlockerReason.APPROVAL_REJECTED if request.status == ApprovalStatus.REJECTED
                    else BlockerReason.APPROVAL_PENDING)
            self._event(run, events, "task.approval_gated", task_id=task_id, gate=gate.model_dump(mode="json"))
            self._refresh(run, events)
        self._mutate(run_id, operation)

    def decide_approval(self, run_id: str, approval_id: str, approved: bool, human_id: str, reason: str):
        def operation(run, events):
            request = run.approvals.get(approval_id)
            if not request or request.status != ApprovalStatus.PENDING or approval_id in run.approval_decisions:
                raise GateError("Approval missing or already decided")
            status = ApprovalStatus.APPROVED if approved else ApprovalStatus.REJECTED
            decision = ApprovalDecision(request_id=approval_id, approved=approved, human_id=human_id, reason=reason, status=status)
            run.approval_decisions[approval_id] = decision
            timestamp = now()
            run.approvals[approval_id] = request.model_copy(update={"status": status, "updated_at": timestamp, "decided_at": timestamp})
            self._event(run, events, "approval.granted" if approved else "approval.rejected", decision=decision.model_dump())
            for task in run.tasks.values():
                if any(g.request_id == approval_id for g in task.approval_gates):
                    if approved and task.status == TaskStatus.BLOCKED and task.blocker == BlockerReason.APPROVAL_PENDING:
                        task.blocker = (BlockerReason.CAPABILITY_PREREQUISITES_MISSING if
                            task.capability == CapabilityProfile.DEVELOPER_SANDBOX and not task.workspace_id
                            else BlockerReason.APPROVAL_PREREQUISITES_SATISFIED)
                    elif not approved:
                        task.blocker = BlockerReason.APPROVAL_REJECTED
            self._refresh(run, events)
            return decision
        return self._mutate(run_id, operation)

    @staticmethod
    def _approved(run, approval_id, action, scope):
        request = run.approvals.get(approval_id)
        decision = run.approval_decisions.get(approval_id)
        value, digest = _scope(scope)
        if (not request or request.status != ApprovalStatus.APPROVED or not decision or not decision.approved or
                request.action != action or request.scope_digest != digest or request.scope_json != value):
            raise GateError("Exact scoped human approval required")

    def require_approval(self, run_id: str, approval_id: str, action: str, scope: dict):
        self._approved(self.get_run(run_id), approval_id, action, scope)

    def link_capability_approval(self, run_id: str, capability_request_id: str,
            approval_id: str, *, workspace_id: str | None = None):
        """Link a pending request to the exact change_capability approval scope."""
        def operation(run, events):
            capability_request = run.capability_requests[capability_request_id]
            if (capability_request.status != CapabilityRequestStatus.PENDING or
                    capability_request.approval_id is not None):
                raise GateError("Capability request is not pending and unlinked")
            approval = run.approvals.get(approval_id)
            if (capability_request.requested_capability in {
                    CapabilityProfile.REPO_READER, CapabilityProfile.DEVELOPER_SANDBOX} and
                    not workspace_id):
                raise GateError("Repository capabilities require an approved workspace binding")
            scope = {"task_id": capability_request.task_id,
                "capability": capability_request.requested_capability.value,
                "workspace_id": workspace_id}
            value, digest = _scope(scope)
            if (not approval or approval.status == ApprovalStatus.SUPERSEDED or
                    approval.action != "change_capability" or approval.scope_json != value or
                    approval.scope_digest != digest):
                raise GateError("Capability request requires an exact current approval request")
            capability_request.approval_id = approval_id
            capability_request.workspace_id = workspace_id
            capability_request.updated_at = now()
            self._event(run, events, "capability.approval_linked",
                capability_request_id=capability_request_id, approval_id=approval_id)
        self._mutate(run_id, operation)

    def apply_capability_escalation(self, run_id: str, capability_request_id: str,
            *, manager_id: str | None = None) -> CapabilityRequest:
        """Atomically apply an exact approved capability and workspace grant."""
        if manager_id is not None and manager_id != self.manager_id:
            raise GateError("Capability authority does not match configured Manager")
        current = self.get_run(run_id)
        existing = current.capability_requests[capability_request_id]
        if existing.status == CapabilityRequestStatus.ESCALATED:
            task = current.tasks[existing.task_id]
            if (task.capability != existing.requested_capability or
                    task.workspace_id != existing.workspace_id):
                raise GateError("Escalated capability request conflicts with durable task state")
            return existing.model_copy(deep=True)

        def operation(run, events):
            capability_request = run.capability_requests[capability_request_id]
            task = run.tasks[capability_request.task_id]
            if (capability_request.status not in {CapabilityRequestStatus.PENDING,
                    CapabilityRequestStatus.APPROVED} or not capability_request.approval_id):
                raise GateError("Capability request is not linked for escalation")
            if task.status not in {TaskStatus.BLOCKED, TaskStatus.READY, TaskStatus.REVISION_REQUIRED}:
                raise GateError("Capability can only be applied in a safe non-active task state")
            self._validate_capability_checks(task, capability_request.requested_capability)
            scope = {"task_id": task.id,
                "capability": capability_request.requested_capability.value,
                "workspace_id": capability_request.workspace_id}
            approval = run.approvals.get(capability_request.approval_id)
            decision = run.approval_decisions.get(capability_request.approval_id)
            if (not approval or approval.status != ApprovalStatus.APPROVED or
                    not decision or not decision.approved):
                raise GateError("Linked capability approval has not been granted")
            self._approved(run, capability_request.approval_id, "change_capability", scope)
            if (capability_request.requested_capability in {
                    CapabilityProfile.REPO_READER, CapabilityProfile.DEVELOPER_SANDBOX} and
                    not capability_request.workspace_id):
                raise GateError("Repository capabilities require an approved workspace binding")
            old_workspace_id = task.workspace_id
            task.capability = capability_request.requested_capability
            task.workspace_id = capability_request.workspace_id
            task.updated_at = now()
            if old_workspace_id != task.workspace_id:
                self._event(run, events, "workspace.replaced" if old_workspace_id else "workspace.bound",
                    task_id=task.id, old_workspace_id=old_workspace_id,
                    new_workspace_id=task.workspace_id,
                    reason="Approved capability escalation")
            if capability_request.status == CapabilityRequestStatus.PENDING:
                self._event(run, events, "capability.approved",
                    capability_request_id=capability_request.id,
                    approval_id=capability_request.approval_id)
            capability_request.status = CapabilityRequestStatus.ESCALATED
            capability_request.updated_at = now()
            self._event(run, events, "capability.escalated",
                capability_request_id=capability_request.id, task_id=task.id,
                capability=task.capability.value, workspace_id=task.workspace_id,
                approval_id=capability_request.approval_id)
            if task.status == TaskStatus.BLOCKED and task.blocker == BlockerReason.CAPABILITY_ESCALATION_PENDING:
                task.blocker = BlockerReason.APPROVAL_PREREQUISITES_SATISFIED
            self._refresh(run, events)
            return capability_request.model_copy(deep=True)
        return self._mutate(run_id, operation)

    def approve_capability_request(self, run_id: str, capability_request_id: str):
        """Reflect a linked exact human approval; this does not grant capability."""
        def operation(run, events):
            capability_request = run.capability_requests[capability_request_id]
            if capability_request.status != CapabilityRequestStatus.PENDING or not capability_request.approval_id:
                raise GateError("Capability request is not awaiting linked approval")
            approval = run.approvals.get(capability_request.approval_id)
            decision = run.approval_decisions.get(capability_request.approval_id)
            if (not approval or approval.status != ApprovalStatus.APPROVED or
                    not decision or not decision.approved):
                raise GateError("Linked capability approval has not been granted")
            capability_request.status = CapabilityRequestStatus.APPROVED
            capability_request.updated_at = now()
            self._event(run, events, "capability.approved",
                capability_request_id=capability_request_id,
                approval_id=capability_request.approval_id)
        self._mutate(run_id, operation)

    def deny_capability_request(self, run_id: str, capability_request_id: str, reason: str):
        def operation(run, events):
            capability_request = run.capability_requests[capability_request_id]
            if capability_request.status not in {CapabilityRequestStatus.PENDING,
                    CapabilityRequestStatus.APPROVED} or not reason.strip():
                raise GateError("Capability request cannot be denied")
            capability_request.status = CapabilityRequestStatus.DENIED
            capability_request.updated_at = now()
            self._event(run, events, "capability.denied",
                capability_request_id=capability_request_id, reason=reason)
        self._mutate(run_id, operation)

    def mark_capability_escalated(self, run_id: str, capability_request_id: str):
        """Close an approved request only after the requested profile is durable."""
        def operation(run, events):
            capability_request = run.capability_requests[capability_request_id]
            task = run.tasks[capability_request.task_id]
            if (capability_request.status != CapabilityRequestStatus.APPROVED or
                    task.capability != capability_request.requested_capability):
                raise GateError("Approved capability has not been applied to the task")
            capability_request.status = CapabilityRequestStatus.ESCALATED
            capability_request.updated_at = now()
            self._event(run, events, "capability.escalated",
                capability_request_id=capability_request_id, task_id=task.id,
                capability=task.capability.value, approval_id=capability_request.approval_id)
        self._mutate(run_id, operation)

    def change_capability(self, run_id: str, task_id: str, capability: CapabilityProfile, approval_id: str, *, workspace_id: str | None = None):
        capability = CapabilityProfile(capability)
        def operation(run, events):
            task = run.tasks[task_id]
            if task.status not in {TaskStatus.PLANNED, TaskStatus.READY, TaskStatus.REVISION_REQUIRED, TaskStatus.BLOCKED}:
                raise GateError("Cannot change capabilities of active or terminal task")
            self._validate_capability_checks(task, capability)
            scope = {"task_id":task_id, "capability":capability.value, "workspace_id":workspace_id}
            self._approved(run, approval_id, "change_capability", scope)
            task.capability, task.workspace_id = capability, workspace_id
            self._event(run, events, "capability.escalated", **scope, approval_id=approval_id)
            if task.status == TaskStatus.BLOCKED and task.blocker == BlockerReason.CAPABILITY_ESCALATION_PENDING:
                task.blocker = (BlockerReason.CAPABILITY_PREREQUISITES_MISSING if
                    capability == CapabilityProfile.DEVELOPER_SANDBOX and not workspace_id
                    else BlockerReason.APPROVAL_PREREQUISITES_SATISFIED)
            if task.status == TaskStatus.READY and not self._ready(run, task):
                self._transition(run, events, task, TaskStatus.BLOCKED,
                    BlockerReason.CAPABILITY_PREREQUISITES_MISSING)
                task.blocker = BlockerReason.CAPABILITY_PREREQUISITES_MISSING
            self._refresh(run, events)
        self._mutate(run_id, operation)

    def _replan_defects(self, run, proposal: ReplanProposal) -> list[str]:
        """Every structural reason this exact proposal could not be applied.

        Approval scope binds to the persisted proposal, so a proposal the kernel
        would refuse at apply time costs a full human approval round-trip before
        the Manager learns it is unworkable. Checking at authoring time removes
        that round-trip, and returning every defect at once lets the Manager
        write one corrected proposal instead of iterating through the gate.

        This is a projection: it never mutates durable state, so callers can use
        it to refuse a proposal before any approval request is recorded.
        """
        defects: list[str] = []
        known = set(run.tasks)
        for label, references in (("reopen", proposal.reopen), ("remove", proposal.remove),
                                  ("dependencies", list(proposal.dependencies))):
            for key in references:
                if key not in known:
                    defects.append(f"{label} references unknown task {key}")
        for key in sorted(set(proposal.remove) & set(proposal.reopen)):
            defects.append(f"task {key} is both removed and reopened")
        added: set[str] = set()
        for source in proposal.add:
            if source.id in added:
                defects.append(f"duplicate added task {source.id}")
            added.add(source.id)
            if not self._is_fresh_task(run, source):
                defects.append(f"added task {source.id} must be a fresh unique task")
                continue
            for check in (lambda: self._reject_identifier_collision(run, source.id),
                          lambda: self._validate_capability_checks(source)):
                try:
                    check()
                except GateError as exc:
                    defects.append(str(exc))
        if run.plan.revision >= run.plan.max_replans:
            defects.append(
                f"replan budget exhausted ({run.plan.revision}/{run.plan.max_replans}); "
                "the kernel cannot apply any further proposal to this plan")
        if defects:
            return defects
        # The resulting plan is what the kernel will actually run, so validate the
        # graph on a projection rather than on today's plan. _graph reads only
        # tasks and artifact keys, so deep-copy the tasks the projection mutates
        # and share the rest instead of copying every artifact's content.
        projection = run.model_copy(update={
            "tasks": {key: task.model_copy(deep=True) for key, task in run.tasks.items()}})
        for key in proposal.remove:
            projection.tasks[key].status = TaskStatus.CANCELLED
        for key, dependencies in proposal.dependencies.items():
            projection.tasks[key].packet.dependencies = list(dependencies)
        for source in proposal.add:
            projection.tasks[source.id] = source.model_copy(deep=True)
        try:
            self._graph(projection)
        except GateError as exc:
            defects.append(f"resulting plan is invalid: {exc}")
        return defects

    @staticmethod
    def _replan_affected(run, proposal: ReplanProposal) -> set[str]:
        """Every task this proposal would reset, including transitive consumers.

        Consumers are resolved exactly as ``_input_artifact_ids`` resolves them,
        so a task that names its upstream through ``required_inputs`` is
        invalidated too. Missing one leaves an ACCEPTED task whose accepted
        artifact cites superseded provenance.
        """
        affected = set(proposal.reopen + proposal.remove + list(proposal.dependencies))
        affected &= set(run.tasks)
        changed = True
        while changed:
            changed = False
            artifact_ids = {a for key in affected for a in run.tasks[key].artifact_ids}
            for key, task in run.tasks.items():
                consumed = set(task.packet.dependencies) | set(task.packet.required_inputs)
                if key not in affected and consumed & (affected | artifact_ids):
                    affected.add(key)
                    changed = True
        return affected

    def _replan_materiality(self, run, proposal: ReplanProposal) -> list[str]:
        """Trusted reasons this proposal needs a human, or [] if it needs none.

        Computed only from the persisted proposal's structure and current
        durable state. Model-supplied trigger, risk and evidence text is never
        consulted: a model that understates its own impact must not be able to
        talk its way past the gate (2026-09-27 decision).

        The narrow autonomous case is a proposal that only reopens work which
        already failed, discarding nothing anyone has accepted. Anything that
        adds or removes tasks, rewires the graph, or would supersede an
        ACCEPTED artifact stays gated, because those change what the human has
        already been shown or what authority the plan grants.
        """
        reasons: list[str] = []
        if proposal.add:
            reasons.append(f"adds {len(proposal.add)} task(s)")
        if proposal.remove:
            reasons.append(f"removes {len(proposal.remove)} task(s)")
        if proposal.dependencies:
            reasons.append("rewires task dependencies")
        if not (proposal.reopen or proposal.add or proposal.remove or proposal.dependencies):
            # A proposal that changes nothing still consumes a plan revision.
            # Keep it gated rather than silently applying a no-op.
            reasons.append("changes nothing")
        discarded = sorted(key for key in self._replan_affected(run, proposal)
                           if run.tasks[key].status == TaskStatus.ACCEPTED)
        if discarded:
            reasons.append("would supersede accepted work: " + ", ".join(discarded))
        return reasons

    def replan_materiality(self, run_id: str, proposal: ReplanProposal) -> list[str]:
        """Public projection: the reasons this proposal requires human approval."""
        return self._replan_materiality(self.get_run(run_id), proposal)

    def validate_replan(self, run_id: str, proposal: ReplanProposal) -> None:
        """Refuse an unworkable proposal before it can bind a human approval gate."""
        defects = self._replan_defects(self.get_run(run_id), proposal)
        if defects:
            raise GateError("Invalid replan proposal: " + "; ".join(defects))

    def propose_replan(self, run_id: str, proposal: ReplanProposal) -> ReplanProposal:
        def operation(run, events):
            if proposal.base_revision != run.plan.revision or proposal.id in run.replans:
                raise GateError("Stale or duplicate replan")
            defects = self._replan_defects(run, proposal)
            if defects:
                raise GateError("Invalid replan proposal: " + "; ".join(defects))
            run.replans[proposal.id] = proposal.model_copy(deep=True)
            self._event(run, events, "plan.replan_proposed", proposal=proposal.model_dump(mode="json"))
            return proposal.model_copy(deep=True)
        return self._mutate(run_id, operation)

    def apply_replan(self, run_id: str, proposal_id: str):
        def operation(run, events):
            proposal = run.replans[proposal_id]
            if proposal.base_revision != run.plan.revision:
                raise GateError("Stale proposal")
            # Re-check the shared structural rules against current state: the
            # proposal was validated at authoring time, but upstream facts can
            # move before the gate opens. This is the single enforcement point
            # for unknown references, freshness, collisions, capability checks,
            # budget, and graph validity; the mutations below rely on it.
            defects = self._replan_defects(run, proposal)
            if defects:
                raise GateError("Invalid replan proposal: " + "; ".join(defects))
            # The flag is the caller's declared authority. A trusted programmatic
            # caller may apply a low-impact proposal directly (doctrine/OPERATING_MODEL.md);
            # the model-facing path never sets this itself -- DurableController
            # derives it from _replan_materiality and re-derives it before
            # applying, so the model cannot route around the gate.
            if proposal.requires_approval:
                self._approved(run, proposal.approval_id, "replan", {"proposal_id":proposal.id, "base_revision":proposal.base_revision})
            affected = self._replan_affected(run, proposal)
            for key in affected:
                task = run.tasks[key]
                for artifact_id in task.artifact_ids:
                    run.artifacts[artifact_id].status = "superseded"
                    self._event(run, events, "artifact.superseded", artifact_id=artifact_id,
                        reason=proposal.trigger, proposal_id=proposal.id)
                    if artifact_id in run.accepted_artifacts:
                        run.accepted_artifacts.remove(artifact_id)
                task.status = TaskStatus.CANCELLED if key in proposal.remove else TaskStatus.PLANNED
                task.assignment = None
                task.result = None
                task.blocker = None
                if key not in proposal.remove:
                    # A reopened task must be executable again. Its assignment,
                    # result and blocker are already cleared, but the lifetime
                    # attempt/revision counters are audit history and must keep
                    # growing, so rebase the budgets instead of resetting them.
                    # Without this a task that exhausted its attempts reaches
                    # READY and delegate() refuses it forever, which makes the
                    # documented "materially different plan" recovery route a
                    # dead end. Total work stays bounded by the replan budget.
                    task.attempt_baseline = task.attempts
                    task.revision_baseline = task.revisions
                self._event(run, events, "task.invalidated", task_id=key, reason=proposal.trigger)
            for key, deps in proposal.dependencies.items():
                run.tasks[key].packet.dependencies = list(deps)
            for source in proposal.add:
                run.tasks[source.id] = source.model_copy(deep=True)
            # Validate the real mutated graph, not the projection _replan_defects used.
            self._graph(run)
            run.plan.task_ids = [key for key,t in run.tasks.items() if t.status != TaskStatus.CANCELLED]
            run.plan.revision += 1
            run.plan.revision_history.append(proposal.model_copy(deep=True))
            self._event(run, events, "plan.replan_applied", proposal_id=proposal.id, revision=run.plan.revision, invalidated=sorted(affected))
            self._refresh(run, events)
        self._mutate(run_id, operation)

    def complete(self, run_id: str, final_result: str, *, criterion_evidence: dict[str, list[str]]) -> Run:
        def operation(run, events):
            active = [t for t in run.tasks.values() if t.status != TaskStatus.CANCELLED]
            if not active or any(t.status != TaskStatus.ACCEPTED for t in active) or run.unresolved_issues:
                raise GateError("Required work remains unresolved")
            self._graph(run)
            checked = set()
            for task in active:
                if not task.artifact_ids or not self._ready(run, task):
                    raise GateError("Accepted task inputs are no longer valid")
                self._validate_accepted_artifact(run, task.artifact_ids[-1], checked=checked)
            if any(request.required and request.status == ApprovalStatus.PENDING for request in run.approvals.values()):
                raise GateError("Outstanding required approval gate")
            if set(criterion_evidence) != set(run.plan.completion_criteria) or any(not ids or not set(ids).issubset(run.accepted_artifacts) for ids in criterion_evidence.values()):
                # State the accepted shape explicitly: the kernel accepts IDs of
                # already-accepted artifacts, never prose, and a Manager that has
                # to guess the format burns turns discovering it (rehearsal finding).
                accepted = sorted(run.accepted_artifacts)
                criteria = sorted(run.plan.completion_criteria)
                example = {criteria[0]: (accepted[:1] or ["<artifact-id>"])} if criteria else {}
                raise GateError(
                    "criterion_evidence must be a JSON object whose keys are exactly the "
                    "completion criteria and whose values are nonempty arrays of accepted "
                    "artifact IDs (no prose). Required keys: "
                    f"{json.dumps(criteria)}. Accepted artifact IDs: {json.dumps(accepted)}. "
                    f"Example: {json.dumps(example)}")
            for ids in criterion_evidence.values():
                for artifact_id in ids:
                    self._validate_accepted_artifact(run, artifact_id, checked=checked)
            if not final_result.strip():
                raise GateError("Final result required")
            run.final_result, run.status, run.plan.status = final_result, "completed", "completed"
            self._event(run, events, "run.completed", criterion_evidence=criterion_evidence)
        self._mutate(run_id, operation)
        return self.get_run(run_id)

