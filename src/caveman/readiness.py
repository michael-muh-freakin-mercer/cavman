"""Offline milestone-six demonstration of Caveman's self-build safety boundary.

The fixture uses the real durable kernel and workspace executor.  Its author and
reviewer are deterministic local fixtures, so proving orchestration correctness
never consumes provider credits.  It intentionally ends with an undecided,
candidate-specific human promotion request.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, Field

from .contracts import TaskPacket, WorkerResult
from .models import ApprovalStatus, CapabilityProfile, TaskNode
from .orchestration import Orchestrator
from .sandbox import WorkspaceManager
from .store import SQLiteStore


COMPILE_CHECK = [
    "python3", "-c",
    "import ast,pathlib; files=list(pathlib.Path('.').rglob('*.py')); "
    "assert files, 'No Python sources'; "
    "[ast.parse(p.read_text(), filename=str(p)) for p in files]",
]


class ReadinessReport(BaseModel):
    run_id: str
    task_id: str
    artifact_id: str
    workspace_id: str
    candidate_branch: str
    candidate_fingerprint: str
    candidate_diff_digest: str
    author_id: str
    reviewer_id: str
    validation_ids: list[str] = Field(min_length=1)
    review_id: str
    acceptance_id: str
    approval_id: str
    approval_scope: dict
    event_count: int
    reloaded: bool
    pending_human_approval: bool
    sandbox_validation: dict


class _OfflineReviewer:
    """A fresh, read-only fixture instance used instead of a provider call."""

    def __init__(self):
        self.id = "offline-reviewer-" + uuid4().hex

    def inspect(self, manager: WorkspaceManager, workspace_id: str, marker: str) -> tuple[bool, str]:
        readme = manager.read_file(workspace_id, "README.md", worker_id=self.id)
        diff = manager.diff(workspace_id, worker_id=self.id)
        passed = marker in readme and marker in diff
        evidence = {
            "fixture": "fresh deterministic read-only reviewer",
            "inspected": ["README.md", "candidate diff"],
            "marker_present": marker in readme,
            "diff_present": marker in diff,
        }
        return passed, json.dumps(evidence, sort_keys=True)


def _default_store_path(repository: Path) -> Path:
    directory = repository / ".local"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / "caveman-operations.db"


def run_readiness_demo(
    repository: str | Path,
    *,
    store_path: str | Path | None = None,
    workspaces: WorkspaceManager | None = None,
) -> ReadinessReport:
    """Run the harmless offline readiness fixture and retain its reviewable worktree.

    ``SandboxUnavailable`` is deliberately allowed to propagate.  A host-process
    fallback would make a successful report misleading.
    """
    repository = Path(repository).resolve(strict=True)
    database = Path(store_path) if store_path is not None else _default_store_path(repository)
    workspace_manager = workspaces or WorkspaceManager(repository)
    store = SQLiteStore(database)
    try:
        core = Orchestrator(store, manager_id="offline-readiness-manager")
        criterion = "A harmless isolated candidate passes trusted validation and independent review"
        run = core.create_run(
            "Demonstrate Caveman self-build readiness without promoting candidate code",
            [criterion],
            constraints=[
                "offline provider fixture only",
                "no merge, push, deployment, or host execution fallback",
                "stop at exact scoped human promotion approval",
            ],
        )
        task_id = "readiness-candidate"
        packet = TaskPacket(
            task_id=task_id,
            role="fixture developer",
            objective="Make one harmless, inspectable candidate-only documentation change",
            deliverable="A candidate README marker in an isolated workspace",
            constraints=["Do not modify the live checkout", "Do not promote the candidate"],
            acceptance_criteria=[
                "README contains the fixture marker",
                "Python sources pass isolated syntax validation",
                "A fresh read-only reviewer confirms the candidate diff",
            ],
            stop_condition="Candidate is submitted for trusted validation and review",
        )
        core.add_tasks(run.id, [TaskNode(
            packet=packet,
            capability=CapabilityProfile.DEVELOPER_SANDBOX,
            required_checks=["compile"],
            review_required=True,
            high_risk=True,
        )])

        author_id = "offline-author-" + uuid4().hex
        grant = workspace_manager.create_candidate(run.id, task_id, author_id)
        core.bind_workspace(run.id, task_id, grant.id)
        assignment = core.delegate(run.id, task_id, author_id)
        core.start(run.id, task_id)

        marker = f"\n<!-- caveman-readiness-fixture:{run.id} -->\n"
        original = workspace_manager.read_file(grant.id, "README.md", worker_id=author_id)
        workspace_manager.write_file(grant.id, "README.md", original.rstrip() + marker,
                                     worker_id=author_id)
        candidate_diff = workspace_manager.diff(grant.id, worker_id=author_id)
        if marker.strip() not in candidate_diff:
            raise RuntimeError("Readiness candidate change is not inspectable")
        fingerprint = workspace_manager.freeze(grant.id)
        artifact = core.submit(
            run.id,
            task_id,
            assignment.id,
            author_id,
            WorkerResult(
                task_id=task_id,
                status="completed",
                summary="Harmless isolated readiness marker added",
                deliverable=json.dumps({
                    "workspace_id": grant.id,
                    "branch": grant.branch,
                    "fingerprint": fingerprint,
                    "diff_sha256": hashlib.sha256(candidate_diff.encode()).hexdigest(),
                }, sort_keys=True),
                evidence=["Candidate diff captured by the trusted workspace manager"],
            ),
            workspace_fingerprint=fingerprint,
        )

        executor_id = "offline-executor-" + uuid4().hex
        executor_grant = workspace_manager.reviewer_grant(grant.id, executor_id)
        output = workspace_manager.run_command(
            executor_grant.id, "test", COMPILE_CHECK, worker_id=executor_id
        )
        validation = core.validate(
            run.id,
            artifact.id,
            "compile",
            output.returncode == 0,
            json.dumps({
                "argv": COMPILE_CHECK,
                "returncode": output.returncode,
                "stdout": output.stdout,
                "stderr": output.stderr,
            }, sort_keys=True),
            validator_id=executor_id,
            workspace_fingerprint=fingerprint,
        )

        reviewer = _OfflineReviewer()
        reviewer_grant = workspace_manager.reviewer_grant(grant.id, reviewer.id)
        review_passed, review_evidence = reviewer.inspect(
            workspace_manager, reviewer_grant.id, marker.strip()
        )
        review = core.review(
            run.id,
            artifact.id,
            reviewer.id,
            review_passed,
            review_evidence,
            workspace_fingerprint=fingerprint,
        )
        acceptance = core.accept(
            run.id,
            task_id,
            reason="Trusted sandbox validation and fresh independent review passed",
            workspace_fingerprint=fingerprint,
        )

        scope = {
            "run_id": run.id,
            "task_id": task_id,
            "artifact_id": artifact.id,
            "artifact_digest": artifact.content_digest,
            "workspace_id": grant.id,
            "workspace_fingerprint": fingerprint,
            "candidate_branch": grant.branch,
            "base_revision": grant.base_revision,
            "candidate_diff_digest": hashlib.sha256(candidate_diff.encode()).hexdigest(),
            "target": "stable/main",
        }
        approval = core.request_approval(
            run.id,
            "promote_candidate",
            scope,
            "Human must inspect and authorize this exact candidate before promotion",
            category="candidate_promotion",
            target="stable/main",
            risk="Promotion would modify canonical Caveman project state",
            artifact_refs=[artifact.id],
        )
    finally:
        store.close()

    # Prove operational truth survives a fresh store/controller instance.
    reloaded_store = SQLiteStore(database)
    try:
        reloaded = reloaded_store.load(run.id)
        events = reloaded_store.events(run.id)
        candidate = reloaded.artifacts[artifact.id]
        expected_events = {
            "run.created", "task.created", "workspace.bound", "task.delegated",
            "task.running", "artifact.submitted", "artifact.validation_completed",
            "artifact.reviewed", "artifact.accepted", "approval.required",
        }
        durable = (
            reloaded.tasks[task_id].status == "ACCEPTED"
            and candidate.status == "accepted"
            and validation.id in {item.id for item in candidate.validations}
            and review.id in {item.id for item in candidate.reviews}
            and reviewer.id != author_id
            and approval.id in reloaded.approvals
            and approval.id not in reloaded.approval_decisions
            and reloaded.approvals[approval.id].status == ApprovalStatus.PENDING
            and expected_events.issubset({event.kind for event in events})
        )
        if not durable:
            raise RuntimeError("Reloaded readiness evidence is incomplete")
        return ReadinessReport(
            run_id=run.id,
            task_id=task_id,
            artifact_id=artifact.id,
            workspace_id=grant.id,
            candidate_branch=grant.branch,
            candidate_fingerprint=fingerprint,
            candidate_diff_digest=scope["candidate_diff_digest"],
            author_id=author_id,
            reviewer_id=reviewer.id,
            validation_ids=[validation.id],
            review_id=review.id,
            acceptance_id=acceptance.id,
            approval_id=approval.id,
            approval_scope=scope,
            event_count=len(events),
            reloaded=True,
            pending_human_approval=True,
            sandbox_validation={
                "argv": COMPILE_CHECK,
                "returncode": output.returncode,
                "stdout": output.stdout,
                "stderr": output.stderr,
            },
        )
    finally:
        reloaded_store.close()
