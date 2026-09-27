"""Serializable operational state, independent of conversation history."""
from __future__ import annotations
from datetime import datetime, timezone
from enum import StrEnum
from uuid import uuid4
from pydantic import BaseModel, ConfigDict, Field
from .contracts import TaskPacket, WorkerResult


def uid() -> str:
    return uuid4().hex


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class TaskStatus(StrEnum):
    PLANNED="PLANNED"
    READY="READY"
    DELEGATED="DELEGATED"
    RUNNING="RUNNING"
    SUBMITTED="SUBMITTED"
    REVIEWING="REVIEWING"
    REVISION_REQUIRED="REVISION_REQUIRED"
    ACCEPTED="ACCEPTED"
    REPLACED="REPLACED"
    BLOCKED="BLOCKED"
    FAILED="FAILED"
    CANCELLED="CANCELLED"


class CapabilityProfile(StrEnum):
    MODEL_ONLY="model_only"
    RESEARCHER="researcher"
    REPO_READER="repo_reader"
    DEVELOPER_SANDBOX="developer_sandbox"
    REVIEWER="reviewer"


class FailureClass(StrEnum):
    BAD_OUTPUT="BAD_OUTPUT"
    MISSING_EVIDENCE="MISSING_EVIDENCE"
    CONSTRAINT_VIOLATION="CONSTRAINT_VIOLATION"
    TASK_AMBIGUITY="TASK_AMBIGUITY"
    DEPENDENCY_FAILURE="DEPENDENCY_FAILURE"
    TOOL_FAILURE="TOOL_FAILURE"
    PROVIDER_FAILURE="PROVIDER_FAILURE"
    TIMEOUT="TIMEOUT"
    CAPABILITY_UNAVAILABLE="CAPABILITY_UNAVAILABLE"
    UNSUPPORTED_CAPABILITY="UNSUPPORTED_CAPABILITY"
    REPEATED_BAD_OUTPUT="REPEATED_BAD_OUTPUT"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class CapabilityRequestStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    ESCALATED = "escalated"


class BlockerReason(StrEnum):
    """Fixed, state-machine-significant values for ``TaskNode.blocker``.

    ``_refresh``, ``bind_workspace``, ``decide_approval`` and the capability
    escalation paths compare against these exact values to decide transitions,
    so they are control flow rather than display text. Values are byte
    identical to the literals they replace: persisted snapshots keep their
    meaning, and because these are ``StrEnum`` members they compare equal to
    the plain strings a reloaded snapshot yields.

    Genuinely free-text blockers stay plain strings and must not be added
    here: worker-supplied summaries, classified failure evidence, and the
    legacy-gate reason that appends outstanding approval IDs.
    """

    CAPABILITY_PREREQUISITES_MISSING = "Capability prerequisites missing"
    APPROVAL_PREREQUISITES_SATISFIED = "Approval prerequisites satisfied"
    APPROVAL_PENDING = "Required approval pending"
    APPROVAL_REJECTED = "Required approval rejected"
    CAPABILITY_ESCALATION_PENDING = "Capability escalation pending"
    UNSUPPORTED_CAPABILITY = "Unsupported capability requires Manager escalation"
    TOOL_FAILURE = "Tool failure requires Manager escalation"
    MANAGER_REPLAN_REQUIRED = "Manager replan required"
    WORKER_REPLACEMENT_REQUIRED = "Worker replacement required"
    # Prefix only; callers append the outstanding approval IDs.
    LEGACY_APPROVAL_REGATE = "Legacy approval gate requires explicit Manager re-gating"
    # Prefix only; callers append the precise readiness reason. Set when a READY
    # task loses readiness, and treated as refreshable so the task returns to
    # READY automatically once the underlying gate is satisfied again.
    READINESS_LAPSED = "Readiness lapsed"


class ApprovalGate(Model):
    """The exact approval request and semantics required by a task."""
    request_id: str
    action: str = Field(min_length=1)
    scope_json: str = Field(min_length=2)
    scope_digest: str = Field(min_length=64, max_length=64)


class WorkerAssignment(Model):
    id: str = Field(default_factory=uid)
    worker_id: str
    task_id: str
    capability: CapabilityProfile
    workspace_id: str | None = None
    created_at: str = Field(default_factory=now)


class TaskNode(Model):
    packet: TaskPacket
    capability: CapabilityProfile = CapabilityProfile.MODEL_ONLY
    workspace_id: str | None = None
    required_checks: list[str] = Field(default_factory=list)
    review_required: bool = True
    high_risk: bool = False
    status: TaskStatus = TaskStatus.PLANNED
    attempts: int = 0
    revisions: int = 0
    # Lifetime counters above are append-only audit history. A replan that
    # reopens a task rebases these baselines so the task gets a fresh budget in
    # the new plan revision without rewriting its history. Remaining budget is
    # always attempts - attempt_baseline against max_attempts.
    attempt_baseline: int = Field(default=0, ge=0)
    revision_baseline: int = Field(default=0, ge=0)
    max_attempts: int = Field(default=3, ge=1)
    max_revisions: int = Field(default=2, ge=0)
    assignment: WorkerAssignment | None = None
    assignment_history: list[WorkerAssignment] = Field(default_factory=list)
    artifact_ids: list[str] = Field(default_factory=list)
    result: WorkerResult | None = None
    blocker: str | None = None
    approval_gates: list[ApprovalGate] = Field(default_factory=list)
    # Retained only for loading early bootstrap snapshots. Bare IDs carry no
    # action/scope semantics and therefore never satisfy readiness.
    approval_ids: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=now)
    updated_at: str = Field(default_factory=now)

    @property
    def id(self) -> str:
        return self.packet.task_id


class ArtifactValidation(Model):
    id: str = Field(default_factory=uid)
    artifact_id: str
    content_digest: str
    check: str
    passed: bool
    evidence: str = Field(min_length=1)
    validator_id: str = Field(min_length=1)
    created_at: str = Field(default_factory=now)


class Review(Model):
    id: str = Field(default_factory=uid)
    artifact_id: str
    content_digest: str
    reviewer_id: str = Field(min_length=1)
    passed: bool
    evidence: str = Field(min_length=1)
    created_at: str = Field(default_factory=now)


class Artifact(Model):
    id: str = Field(default_factory=uid)
    run_id: str
    task_id: str
    worker_id: str
    kind: str = "worker_result"
    content: str
    content_digest: str
    workspace_fingerprint: str | None = None
    version: int
    predecessor_id: str | None = None
    input_artifact_ids: list[str] = Field(default_factory=list)
    status: str = "candidate"
    validations: list[ArtifactValidation] = Field(default_factory=list)
    reviews: list[Review] = Field(default_factory=list)
    created_at: str = Field(default_factory=now)


class ProjectVerification(Model):
    """Trusted evidence that the assembled product actually runs.

    Distinct from ``ArtifactValidation``, which proves one candidate satisfied
    its own declared checks. A run can hold nothing but passing validations and
    still deliver a product that will not start, so completion additionally
    requires verification recorded against the integrated workspace. Produced
    only by trusted sandbox execution, never from a model's claim.
    """
    id: str = Field(default_factory=uid)
    kind: str = Field(min_length=1)
    task_id: str
    workspace_id: str
    workspace_fingerprint: str | None = None
    argv: list[str] = Field(default_factory=list)
    passed: bool
    returncode: int | None = None
    evidence: str = Field(min_length=1)
    verifier_id: str = Field(min_length=1)
    created_at: str = Field(default_factory=now)


class Decision(Model):
    id: str = Field(default_factory=uid)
    action: str
    reason: str = Field(min_length=1)
    actor_id: str
    context: str = ""
    options_considered: list[str] = Field(default_factory=list)
    consequences: list[str] = Field(default_factory=list)
    affected_ids: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=now)


class AcceptanceDecision(Decision):
    artifact_id: str


class WorkerFailure(Model):
    id: str = Field(default_factory=uid)
    task_id: str
    classification: FailureClass
    evidence: str = Field(min_length=1)
    created_at: str = Field(default_factory=now)


class CapabilityRequest(Model):
    id: str = Field(default_factory=uid)
    run_id: str
    task_id: str
    assignment_id: str
    worker_id: str
    requested_capability: CapabilityProfile
    reason: str = Field(min_length=1)
    risk: str = Field(min_length=1)
    status: CapabilityRequestStatus = CapabilityRequestStatus.PENDING
    approval_id: str | None = None
    workspace_id: str | None = None
    created_at: str = Field(default_factory=now)
    updated_at: str = Field(default_factory=now)


class RecoveryDecision(Decision):
    failure_id: str


class ApprovalRequest(Model):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(default_factory=uid)
    run_id: str = Field(min_length=1)
    category: str = Field(min_length=1)
    action: str = Field(min_length=1)
    scope_json: str = Field(min_length=2)
    scope_digest: str = Field(min_length=64, max_length=64)
    target: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    risk: str = Field(min_length=1)
    artifact_refs: list[str] = Field(default_factory=list)
    requested_capability: CapabilityProfile | None = None
    required: bool = True
    status: ApprovalStatus = ApprovalStatus.PENDING
    created_at: str = Field(default_factory=now)
    updated_at: str = Field(default_factory=now)
    decided_at: str | None = None
    superseded_at: str | None = None
    replacement_id: str | None = None


class ApprovalDecision(Model):
    model_config = ConfigDict(extra="forbid", frozen=True)
    request_id: str
    approved: bool
    human_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    status: ApprovalStatus
    created_at: str = Field(default_factory=now)


class ModelUsageRecord(Model):
    id: str = Field(default_factory=uid)
    run_id: str
    task_id: str | None = None
    assignment_id: str | None = None
    worker_id: str | None = None
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    role: str = Field(min_length=1)
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    usage_known: bool = True
    unknown_reason: str | None = None
    raw_usage: dict[str, object] = Field(default_factory=dict)
    created_at: str = Field(default_factory=now)


class ReplanProposal(Model):
    id: str = Field(default_factory=uid)
    base_revision: int
    trigger: str = Field(min_length=1)
    evidence: list[str] = Field(min_length=1)
    add: list[TaskNode] = Field(default_factory=list)
    remove: list[str] = Field(default_factory=list)
    reopen: list[str] = Field(default_factory=list)
    dependencies: dict[str, list[str]] = Field(default_factory=dict)
    risks: list[str] = Field(default_factory=list)
    approval_id: str | None = None
    requires_approval: bool = False


class WorkPlan(Model):
    objective: str
    completion_criteria: list[str] = Field(min_length=1)
    task_ids: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    status: str = "active"
    revision: int = 0
    max_replans: int = Field(default=3, ge=0)
    revision_history: list[ReplanProposal] = Field(default_factory=list)
    created_at: str = Field(default_factory=now)


class Event(Model):
    id: str = Field(default_factory=uid)
    run_id: str
    sequence: int = 0
    kind: str
    data: dict = Field(default_factory=dict)
    created_at: str = Field(default_factory=now)


class Run(Model):
    schema_version: int = 2
    id: str = Field(default_factory=uid)
    version: int = 0
    objective: str
    constraints: list[str] = Field(default_factory=list)
    status: str = "active"
    plan: WorkPlan
    tasks: dict[str, TaskNode] = Field(default_factory=dict)
    artifacts: dict[str, Artifact] = Field(default_factory=dict)
    accepted_artifacts: list[str] = Field(default_factory=list)
    available_inputs: dict[str, str] = Field(default_factory=dict)
    decisions: list[Decision] = Field(default_factory=list)
    usage_records: list[ModelUsageRecord] = Field(default_factory=list)
    acceptances: list[AcceptanceDecision] = Field(default_factory=list)
    verifications: list[ProjectVerification] = Field(default_factory=list)
    failures: list[WorkerFailure] = Field(default_factory=list)
    recoveries: list[RecoveryDecision] = Field(default_factory=list)
    approvals: dict[str, ApprovalRequest] = Field(default_factory=dict)
    approval_decisions: dict[str, ApprovalDecision] = Field(default_factory=dict)
    capability_requests: dict[str, CapabilityRequest] = Field(default_factory=dict)
    replans: dict[str, ReplanProposal] = Field(default_factory=dict)
    unresolved_issues: list[str] = Field(default_factory=list)
    event_cursor: int = 0
    final_result: str | None = None
    created_at: str = Field(default_factory=now)
    updated_at: str = Field(default_factory=now)

