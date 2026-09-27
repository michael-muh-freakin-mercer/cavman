from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


ToolPolicy = Literal["model_only", "web_search"]
CapabilityName = Literal["model_only", "researcher", "repo_reader", "developer_sandbox", "reviewer"]


class TaskPacket(BaseModel):
    """The minimum-sufficient assignment Caveman gives one temporary specialist."""

    task_id: str = Field(description="Stable identifier assigned by Caveman for this task.")
    role: str = Field(description="Narrow specialist role for this single lane of work.")
    objective: str = Field(description="The one objective this worker must accomplish.")
    deliverable: str = Field(description="The inspectable output the worker must return.")
    context: str = Field(
        default="",
        description="Only the goal context and accepted upstream information needed for this lane.",
    )
    required_inputs: list[str] = Field(
        default_factory=list,
        description="Inputs the worker must use or account for.",
    )
    constraints: list[str] = Field(
        default_factory=list,
        description="Scope, policy, format, or implementation constraints for this task.",
    )
    dependencies: list[str] = Field(
        default_factory=list,
        description="Accepted upstream task or artifact identifiers this task depends on.",
    )
    acceptance_criteria: list[str] = Field(
        min_length=1,
        description="Criteria Caveman will use to accept or reject the deliverable.",
    )
    stop_condition: str = Field(
        description="Condition telling the worker when its lane is complete or genuinely blocked."
    )
    tool_policy: ToolPolicy = Field(
        default="model_only",
        description=(
            "Least-privilege capability set granted to this worker. Use web_search only when "
            "fresh external evidence is required; otherwise use model_only."
        ),
    )


class CriterionCheck(BaseModel):
    criterion: str
    passed: bool
    note: str = ""


class SourceReference(BaseModel):
    """A source a specialist actually used to support its deliverable."""

    title: str = ""
    url: str
    note: str = ""


class CapabilityRequestPayload(BaseModel):
    """Untrusted worker proposal for a capability the Manager may evaluate."""

    requested_capability: CapabilityName
    reason: str = Field(min_length=1)
    risk: str = Field(min_length=1)


class WorkerResult(BaseModel):
    """Structured, provisional worker output returned to Caveman for acceptance review."""

    task_id: str
    status: Literal["completed", "blocked", "needs_revision"]
    summary: str
    deliverable: str
    evidence: list[str] = Field(default_factory=list)
    sources: list[SourceReference] = Field(
        default_factory=list,
        description="External sources actually used. Empty for model-only work.",
    )
    assumptions: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    acceptance_check: list[CriterionCheck] = Field(default_factory=list)
    blocker: str | None = None
    capability_request: CapabilityRequestPayload | None = None
