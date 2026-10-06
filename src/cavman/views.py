"""Read-only, user-facing projections of durable orchestration state.

Everything here is derived from the core's snapshot, its event log and the
platform's job rows. Nothing is invented: no percentages, no estimated costs,
no assumed test results. Where the core has no record, the projection says so.
"""
from __future__ import annotations

import json
from typing import Any

from walter.models import (ApprovalStatus, Artifact, CapabilityRequestStatus, Event, FailureClass,
                           Run, TaskNode, TaskStatus)
from walter.usage import cached_tokens, usage_cost

from .platform_store import Job, RunRecord

CHECK_LABELS = {
    "result_schema": "Output structure",
    "compile": "Python compile",
    "pytest": "Candidate tests",
    "pytest_candidate": "Candidate tests",
    "pytest_regression": "Regression tests",
    "node_test": "Node tests",
    "tsc": "TypeScript typecheck",
    "npm_build": "Project build",
}

FAILURE_EXPLANATIONS = {
    FailureClass.BAD_OUTPUT: "The specialist's output did not meet the task's acceptance criteria.",
    FailureClass.MISSING_EVIDENCE: "The specialist could not show evidence that the work is correct.",
    FailureClass.CONSTRAINT_VIOLATION: "The work broke one of the task's constraints.",
    FailureClass.TASK_AMBIGUITY: "The task was too ambiguous to complete as written.",
    FailureClass.DEPENDENCY_FAILURE: "Work this task depends on is not available.",
    FailureClass.TOOL_FAILURE: "A tool or the execution environment failed.",
    FailureClass.PROVIDER_FAILURE: "The model provider failed to respond.",
    FailureClass.TIMEOUT: "The work was interrupted before it finished.",
    FailureClass.CAPABILITY_UNAVAILABLE: "The task needs a capability it was not granted.",
    FailureClass.UNSUPPORTED_CAPABILITY: "The task needs a capability Cavman does not support yet.",
    FailureClass.REPEATED_BAD_OUTPUT: "The specialist repeatedly produced unacceptable output.",
    FailureClass.STALE_BASE: ("Other accepted work was merged into the project after this candidate was "
                              "built, so it is rebuilt on the latest code before it can be accepted."),
}

RECOVERY_LABELS = {
    "RETRY": "Retry with a fresh attempt",
    "REVISE": "Revision requested",
    "REPLACE": "Specialist replaced",
    "ESCALATE": "Escalated for a decision",
    "REPLAN": "Replan required",
}

CAPABILITY_LABELS = {
    "model_only": "Reasoning only",
    "researcher": "Research",
    "repo_reader": "Read-only repository access",
    "developer_sandbox": "Sandboxed development",
    "reviewer": "Review",
}

JOB_OUTCOME_TEXT = {
    "succeeded": "The Manager finished its turn.",
    "terminal": "The run had already finished.",
    "budget_exceeded": ("The run reached its spending or usage limit, or your account's monthly limit, "
                        "and was paused safely."),
    "turn_limit": "The Manager reached its per-session turn limit and paused.",
    "cancelled": "Execution was stopped by you.",
    "config_error": "Cavman's model provider is not configured, so the run could not start.",
    "interrupted": "The worker running this build stopped unexpectedly.",
    "error": "Execution stopped because of an unexpected error.",
}


def _latest_artifact(run: Run, task: TaskNode) -> Artifact | None:
    return run.artifacts.get(task.artifact_ids[-1]) if task.artifact_ids else None


def task_display_state(run: Run, task: TaskNode) -> str:
    status = task.status
    if status == TaskStatus.PLANNED:
        return "Waiting"
    if status == TaskStatus.READY:
        return "Ready"
    if status in {TaskStatus.DELEGATED, TaskStatus.RUNNING}:
        return "Running"
    if status == TaskStatus.SUBMITTED:
        return "Validating"
    if status == TaskStatus.REVIEWING:
        artifact = _latest_artifact(run, task)
        recorded = {v.check for v in artifact.validations} if artifact else set()
        return "Validating" if set(task.required_checks) - recorded else "Reviewing"
    if status == TaskStatus.REVISION_REQUIRED:
        return "Revision Needed"
    if status == TaskStatus.BLOCKED:
        if task.blocker and "approval" in task.blocker.lower() and "pending" in task.blocker.lower():
            return "Needs Approval"
        return "Blocked"
    return {TaskStatus.ACCEPTED: "Accepted", TaskStatus.FAILED: "Failed",
            TaskStatus.REPLACED: "Replaced", TaskStatus.CANCELLED: "Cancelled"}[status]


def _title(task: TaskNode) -> str:
    objective = " ".join(task.packet.objective.split())
    return objective if len(objective) <= 120 else objective[:117] + "..."


def _specialist(task: TaskNode) -> str:
    role = " ".join(task.packet.role.split())
    return role[:80] or "Specialist"


def _parse_evidence(evidence: str) -> dict[str, Any] | None:
    try:
        value = json.loads(evidence)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _manifest(content: str) -> tuple[str, dict | None]:
    marker = "\n\nWORKSPACE_MANIFEST="
    if marker not in content:
        return content, None
    body, _, raw = content.rpartition(marker)
    try:
        manifest = json.loads(raw)
    except ValueError:
        return content, None
    return body, manifest if isinstance(manifest, dict) else None


def changed_files_from_diff(diff: str) -> list[str]:
    files = []
    for line in diff.splitlines():
        if line.startswith("diff --git a/"):
            name = line.split(" b/", 1)[-1]
            if name not in files:
                files.append(name)
    return files


class Projector:
    def __init__(self, redact, settings):
        self.redact = redact
        self.settings = settings

    # Validation, review and artifacts --------------------------------

    def validation(self, record) -> dict:
        detail = _parse_evidence(record.evidence)
        output = ""
        returncode = None
        command = None
        if detail is not None:
            returncode = detail.get("returncode")
            argv = detail.get("argv")
            if isinstance(argv, list):
                command = " ".join(str(part) for part in argv)
                if len(command) > 160:
                    command = command[:157] + "..."
            output = "\n".join(part for part in (str(detail.get("stdout") or "").strip(),
                                                  str(detail.get("stderr") or "").strip()) if part)
        else:
            output = record.evidence
        return {
            "id": record.id,
            "check": record.check,
            "label": CHECK_LABELS.get(record.check, record.check),
            "status": "passed" if record.passed else "failed",
            "trusted": True,
            "executor": record.validator_id.split("-", 1)[0],
            "command": self.redact(command) if command else None,
            "returncode": returncode,
            "output": self.redact(output, limit=6000),
            "content_digest": record.content_digest,
            "created_at": record.created_at,
        }

    def review(self, record) -> dict:
        detail = _parse_evidence(record.evidence) or {}
        evidence = detail.get("evidence") if isinstance(detail.get("evidence"), list) else []
        return {
            "id": record.id,
            "status": "passed" if record.passed else "changes_requested",
            "reviewer": "Independent reviewer",
            "reason": self.redact(str(detail.get("reason") or ""), limit=2000),
            "evidence": [self.redact(str(item), limit=600) for item in evidence[:20]],
            "content_digest": record.content_digest,
            "created_at": record.created_at,
        }

    def required_checks(self, task: TaskNode, artifact: Artifact | None) -> list[dict]:
        """Every predeclared check with its trusted status; never assumes a pass."""
        latest = {}
        if artifact is not None:
            for record in artifact.validations:
                if record.content_digest == artifact.content_digest:
                    latest[record.check] = record
        checks = []
        for check in task.required_checks:
            record = latest.get(check)
            checks.append({
                "check": check,
                "label": CHECK_LABELS.get(check, check),
                "status": ("passed" if record.passed else "failed") if record else "not_run",
            })
        return checks

    def artifact(self, run: Run, artifact: Artifact, *, content: bool = False) -> dict:
        task = run.tasks.get(artifact.task_id)
        body, manifest = _manifest(artifact.content)
        files = changed_files_from_diff(str(manifest.get("diff", ""))) if manifest else []
        reviews = [self.review(r) for r in artifact.reviews]
        validations = [self.validation(v) for v in artifact.validations]
        failed = any(v["status"] == "failed" for v in validations)
        validation_state = ("failed" if failed else
                            "passed" if task and task.required_checks and all(
                                c["status"] == "passed" for c in self.required_checks(task, artifact))
                            else "not_run" if not validations else "partial")
        review_state = (reviews[-1]["status"] if reviews else "not_reviewed")
        data = {
            "id": artifact.id,
            "task_id": artifact.task_id,
            "task_title": _title(task) if task else artifact.task_id,
            "specialist": _specialist(task) if task else None,
            "kind": "code_change" if manifest else "document",
            "status": artifact.status,
            "version": artifact.version,
            "predecessor_id": artifact.predecessor_id,
            "input_artifact_ids": artifact.input_artifact_ids,
            "content_digest": artifact.content_digest,
            "workspace_fingerprint": artifact.workspace_fingerprint,
            "workspace_id": manifest.get("workspace_id") if manifest else None,
            "integrated_commit": artifact.integrated_commit,
            "changed_files": files,
            "validation_state": validation_state,
            "review_state": review_state,
            "validations": validations,
            "reviews": reviews,
            "summary": self.redact(body.strip().splitlines()[0] if body.strip() else "", limit=300),
            "content_chars": len(body),
            "created_at": artifact.created_at,
        }
        if content:
            data["content"] = self.redact(body, limit=200_000)
            data["diff"] = self.redact(str(manifest.get("diff", "")), limit=400_000) if manifest else None
        return data

    # Tasks ------------------------------------------------------------

    def task(self, run: Run, task: TaskNode, events_by_task: dict[str, dict] | None = None,
             usage_by_task: dict[str, dict] | None = None) -> dict:
        artifact = _latest_artifact(run, task)
        failures = [f for f in run.failures if f.task_id == task.id]
        return {
            "id": task.id,
            "title": _title(task),
            "specialist": _specialist(task),
            "deliverable": self.redact(task.packet.deliverable, limit=600),
            "acceptance_criteria": [self.redact(c, limit=400) for c in task.packet.acceptance_criteria],
            "status": task.status.value,
            "state": task_display_state(run, task),
            "capability": task.capability.value,
            "capability_label": CAPABILITY_LABELS.get(task.capability.value, task.capability.value),
            "dependencies": list(task.packet.dependencies),
            "attempt": task.attempts,
            "max_attempts": task.max_attempts + task.attempt_baseline,
            "revisions": task.revisions,
            "blocker": self.redact(task.blocker, limit=600) or None,
            "required_checks": self.required_checks(task, artifact),
            "review_required": bool(task.review_required or task.high_risk
                                    or task.capability.value == "developer_sandbox"),
            "artifact_ids": list(task.artifact_ids),
            "latest_artifact_id": artifact.id if artifact else None,
            "failure_count": len(failures),
            "latest_event": (events_by_task or {}).get(task.id),
            "usage": (usage_by_task or {}).get(task.id),
            "updated_at": task.updated_at,
        }

    # Approvals --------------------------------------------------------

    def approval(self, run: Run, request) -> dict:
        try:
            scope = json.loads(request.scope_json)
        except ValueError:
            scope = {}
        changes = self._approval_changes(run, request.action, scope)
        decision = run.approval_decisions.get(request.id)
        return {
            "id": request.id,
            "action": request.action,
            "category": request.category,
            "title": self._approval_title(request.action, request.category),
            "what": changes["what"],
            "why": self.redact(request.rationale, limit=2000),
            "changes": changes["items"],
            "risk": self.redact(request.risk, limit=1000),
            "target": self.redact(request.target, limit=300),
            "scope": scope,
            "scope_digest": request.scope_digest,
            "artifact_refs": list(request.artifact_refs),
            "required": request.required,
            "status": request.status.value,
            "created_at": request.created_at,
            "decided_at": request.decided_at,
            "decision": ({"approved": decision.approved, "reason": self.redact(decision.reason, limit=1000),
                          "decided_by": decision.human_id} if decision else None),
        }

    @staticmethod
    def _approval_title(action: str, category: str) -> str:
        return {
            "replan": "Change the plan",
            "change_capability": "Grant a capability",
        }.get(action, "Approve: " + action.replace("_", " "))

    def _approval_changes(self, run: Run, action: str, scope: dict) -> dict:
        if action == "replan":
            proposal = run.replans.get(str(scope.get("proposal_id")))
            if proposal is None:
                return {"what": "Apply a revised plan.", "items": []}
            items = [f"Add task: {_title(task)}" for task in proposal.add]
            items += [f"Remove task: {_title(run.tasks[key]) if key in run.tasks else key}"
                      for key in proposal.remove]
            items += [f"Redo task: {_title(run.tasks[key]) if key in run.tasks else key}"
                      for key in proposal.reopen]
            items += [f"Change dependencies of {key}" for key in proposal.dependencies]
            return {"what": "Apply plan revision " + str(proposal.base_revision + 1) + ": "
                    + self.redact(proposal.trigger, limit=300), "items": items}
        if action == "change_capability":
            task = run.tasks.get(str(scope.get("task_id")))
            capability = CAPABILITY_LABELS.get(str(scope.get("capability")), str(scope.get("capability")))
            name = _title(task) if task else str(scope.get("task_id"))
            return {"what": f"Give the task “{name}” the capability: {capability}.",
                    "items": [f"Capability: {capability}", f"Task: {name}"]
                    + ([f"Isolated workspace: {scope['workspace_id']}"] if scope.get("workspace_id") else [])}
        items = [f"{key.replace('_', ' ')}: {self.redact(str(value), limit=200)}"
                 for key, value in sorted(scope.items())]
        return {"what": f"Authorize the action “{action.replace('_', ' ')}” with exactly this scope.",
                "items": items}

    # Failures, recovery and replans ------------------------------------

    def failures(self, run: Run) -> list[dict]:
        recoveries = {r.failure_id: r for r in run.recoveries}
        items = []
        for failure in run.failures:
            task = run.tasks.get(failure.task_id)
            recovery = recoveries.get(failure.id)
            items.append({
                "id": failure.id,
                "task_id": failure.task_id,
                "task_title": _title(task) if task else failure.task_id,
                "classification": failure.classification.value,
                "explanation": FAILURE_EXPLANATIONS.get(failure.classification, "The task failed."),
                "evidence": self.redact(failure.evidence, limit=3000),
                "created_at": failure.created_at,
                "recovery": ({"action": recovery.action,
                              "label": RECOVERY_LABELS.get(recovery.action, recovery.action),
                              "reason": self.redact(recovery.reason, limit=1000),
                              "created_at": recovery.created_at} if recovery else None),
            })
        return items

    def replans(self, run: Run) -> dict:
        applied = {p.id for p in run.plan.revision_history}
        proposals = []
        for proposal in run.replans.values():
            approval = run.approvals.get(proposal.approval_id) if proposal.approval_id else None
            proposals.append({
                "id": proposal.id,
                "trigger": self.redact(proposal.trigger, limit=600),
                "base_revision": proposal.base_revision,
                "adds": len(proposal.add), "removes": len(proposal.remove),
                "reopens": len(proposal.reopen),
                "requires_approval": proposal.requires_approval,
                "status": ("applied" if proposal.id in applied else
                           "awaiting_approval" if approval and approval.status == ApprovalStatus.PENDING
                           else "rejected" if approval and approval.status == ApprovalStatus.REJECTED
                           else "proposed"),
            })
        return {"revision": run.plan.revision,
                "replans_remaining": max(0, run.plan.max_replans - run.plan.revision),
                "proposals": proposals}

    # Usage and cost -----------------------------------------------------

    def usage(self, run: Run, record: RunRecord | None) -> dict:
        records = []
        total_cost = 0.0
        unknown_cost = 0
        by_model: dict[str, dict] = {}
        by_role: dict[str, dict] = {}
        totals = {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0, "total_tokens": 0}
        for usage in run.usage_records:
            cost = usage_cost(usage.raw_usage)
            cached = cached_tokens(usage.raw_usage)
            if cost is None:
                unknown_cost += 1
            else:
                total_cost += cost
            for key, value in (("input_tokens", usage.input_tokens), ("output_tokens", usage.output_tokens),
                               ("cached_tokens", cached), ("total_tokens", usage.total_tokens)):
                totals[key] += value or 0
            for bucket, key in ((by_model, f"{usage.provider}/{usage.model}"), (by_role, usage.role)):
                entry = bucket.setdefault(key, {"calls": 0, "total_tokens": 0, "cost_usd": 0.0,
                                                "calls_without_cost": 0})
                entry["calls"] += 1
                entry["total_tokens"] += usage.total_tokens or 0
                if cost is None:
                    entry["calls_without_cost"] += 1
                else:
                    entry["cost_usd"] += cost
            records.append({
                "id": usage.id, "provider": usage.provider, "model": usage.model, "role": usage.role,
                "task_id": usage.task_id, "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens, "cached_tokens": cached,
                "total_tokens": usage.total_tokens, "cost_usd": cost,
                "usage_known": usage.usage_known, "created_at": usage.created_at,
            })
        budget = None
        if record is not None:
            ratio = total_cost / record.budget_usd if record.budget_usd else 0.0
            calls_ratio = len(records) / record.max_model_calls if record.max_model_calls else 0.0
            budget = {
                "limit_usd": record.budget_usd,
                "spent_usd": round(total_cost, 6),
                "remaining_usd": max(0.0, round(record.budget_usd - total_cost, 6)),
                "max_model_calls": record.max_model_calls,
                "model_calls": len(records),
                "warning": max(ratio, calls_ratio) >= self.settings.budget_warning_ratio,
                "exceeded": ratio >= 1.0 or calls_ratio >= 1.0,
            }
        return {
            "calls": len(records),
            "cost_usd": round(total_cost, 6),
            "cost_complete": unknown_cost == 0,
            "calls_without_cost": unknown_cost,
            "tokens": totals,
            "by_model": by_model,
            "by_role": by_role,
            "budget": budget,
            "records": records[-200:],
        }

    # Events -------------------------------------------------------------

    def event(self, run: Run, event: Event) -> dict:
        data = event.data
        task_id = data.get("task_id")
        if not task_id and isinstance(data.get("assignment"), dict):
            task_id = data["assignment"].get("task_id")
        if not task_id and isinstance(data.get("failure"), dict):
            task_id = data["failure"].get("task_id")
        task = run.tasks.get(str(task_id)) if task_id else None
        name = _title(task) if task else None
        kind = event.kind
        level = "info"
        title = kind.replace(".", " ").replace("_", " ").capitalize()
        detail = None
        debug = False
        if kind == "run.created":
            title = "Build requested"
        elif kind == "plan.criteria_defined":
            title, detail = "Success criteria defined", f"{len(data.get('criteria', []))} criteria"
        elif kind == "task.created":
            title = f"Planned: {name or data.get('task_id')}"
        elif kind == "task.ready":
            title = f"Ready: {name}"
        elif kind == "assignment.created":
            specialist = _specialist(task) if task else "specialist"
            title = f"Delegated to {specialist}"
            detail = f"{name} — attempt {data.get('attempt')}"
        elif kind == "assignment.started":
            title, detail = "Specialist started", name
        elif kind == "artifact.submitted":
            title, detail = f"Candidate submitted (v{data.get('version')})", name
        elif kind == "artifact.validation_completed":
            record = data.get("validation", {})
            passed = bool(record.get("passed"))
            label = CHECK_LABELS.get(record.get("check"), record.get("check"))
            artifact = run.artifacts.get(record.get("artifact_id", ""))
            owner = run.tasks.get(artifact.task_id) if artifact else None
            title = f"Validation {'passed' if passed else 'failed'}: {label}"
            detail = _title(owner) if owner else None
            level = "success" if passed else "error"
        elif kind == "artifact.reviewed":
            record = data.get("review", {})
            passed = bool(record.get("passed"))
            artifact = run.artifacts.get(record.get("artifact_id", ""))
            owner = run.tasks.get(artifact.task_id) if artifact else None
            title = "Review passed" if passed else "Reviewer requested changes"
            detail = _title(owner) if owner else None
            level = "success" if passed else "warning"
        elif kind == "artifact.accepted":
            artifact = run.artifacts.get(data.get("artifact_id", ""))
            owner = run.tasks.get(artifact.task_id) if artifact else None
            title, detail, level = "Accepted", _title(owner) if owner else None, "success"
        elif kind == "artifact.integrated":
            artifact = run.artifacts.get(data.get("artifact_id", ""))
            owner = run.tasks.get(artifact.task_id) if artifact else None
            title = f"Merged into the project ({str(data.get('commit', ''))[:7]})"
            detail, level = (_title(owner) if owner else None), "success"
        elif kind == "artifact.rejected":
            title, level = "Candidate rejected", "warning"
        elif kind == "failure.classified":
            failure = data.get("failure", {})
            owner = run.tasks.get(failure.get("task_id", ""))
            title = "Task failed: " + str(failure.get("classification", "")).replace("_", " ").lower()
            detail, level = (_title(owner) if owner else None), "error"
        elif kind == "recovery.decided":
            decision = data.get("decision", {})
            title = "Recovery: " + RECOVERY_LABELS.get(decision.get("action"), str(decision.get("action")))
            level = "warning"
        elif kind == "retry.scheduled":
            title, detail, level = "Retry started", name, "warning"
        elif kind == "worker.replaced":
            title, detail, level = "Specialist replaced", name, "warning"
        elif kind == "plan.replan_proposed":
            title, level = "Replan proposed", "warning"
        elif kind == "plan.replan_applied":
            title, level = f"Replan applied (revision {data.get('revision')})", "warning"
        elif kind == "approval.required":
            request = data.get("request", {})
            title = "Approval needed: " + str(request.get("action", "")).replace("_", " ")
            level = "attention"
        elif kind == "approval.granted":
            title, level = "Approval granted", "success"
        elif kind == "approval.rejected":
            title, level = "Approval rejected", "warning"
        elif kind == "approval.superseded":
            title = "Approval request replaced"
        elif kind == "capability.requested":
            title, level = "Specialist requested more capability", "attention"
        elif kind == "run.completed":
            title, level = "Build complete", "success"
        elif kind == "run.abandoned":
            title, level = "Run closed", "warning"
        elif kind == "run.resumed":
            title, level = "Recovered after interruption", "warning"
        elif kind == "workspace.bound":
            title, detail = "Isolated workspace created", name
        elif kind == "model.usage.recorded":
            usage = data.get("usage", {})
            title = f"Model call: {usage.get('role')}"
            detail = f"{usage.get('provider')}/{usage.get('model')}"
            debug = True
        elif kind.startswith("task."):
            debug = kind not in {"task.failed", "task.blocked", "task.revision_required"}
            title = f"{kind.split('.', 1)[1].replace('_', ' ').capitalize()}: {name}"
            if kind == "task.blocked":
                level = "warning"
        elif kind.startswith("assignment.") or kind.startswith("artifact.") or kind == "input.registered":
            debug = True
        return {"sequence": event.sequence, "kind": kind, "title": title,
                "detail": self.redact(detail, limit=300) or None, "level": level,
                "task_id": task_id if isinstance(task_id, str) else None,
                "debug": debug, "created_at": event.created_at}

    # Run ------------------------------------------------------------------

    def run_state(self, run: Run, jobs: list[Job]) -> dict:
        """Overall state derived only from durable records."""
        active = next((j for j in jobs if j.status in ("queued", "running")), None)
        last = jobs[-1] if jobs else None
        pending = [a for a in run.approvals.values() if a.status == ApprovalStatus.PENDING]
        pending_caps = [c for c in run.capability_requests.values()
                        if c.status == CapabilityRequestStatus.PENDING]
        tasks = list(run.tasks.values())
        if run.status == "completed":
            return {"state": "complete", "label": "Complete", "explanation": "Every task was accepted and "
                    "the completion gate confirmed each success criterion."}
        if run.status == "abandoned":
            return {"state": "cancelled", "label": "Closed", "explanation": "This run was closed. Its "
                    "history is preserved."}
        if active is not None:
            if active.kind == "recover":
                return {"state": "recovering", "label": "Recovering",
                        "explanation": "Cavman is recovering interrupted work before continuing."}
            if active.status == "queued":
                return {"state": "starting", "label": "Starting",
                        "explanation": "Waiting for a Cavman worker to pick up this build."}
            if active.cancel_requested:
                return {"state": "stopping", "label": "Stopping",
                        "explanation": "Stopping after the current step."}
            if not tasks:
                return {"state": "planning", "label": "Planning",
                        "explanation": "Cavman is working out what needs to be built."}
            if any(t.status == TaskStatus.REVISION_REQUIRED for t in tasks):
                return {"state": "revision_required", "label": "Revising",
                        "explanation": "A reviewer or check asked for changes; Cavman is revising."}
            return {"state": "running", "label": "Building",
                    "explanation": "Specialists are working through the plan."}
        if pending or pending_caps:
            return {"state": "approval_needed", "label": "Approval needed",
                    "explanation": "Cavman is waiting for your decision before continuing."}
        if last is not None and last.status in ("failed", "cancelled"):
            state = {"budget_exceeded": "budget_reached", "cancelled": "paused",
                     "interrupted": "failed", "config_error": "failed", "error": "failed",
                     "turn_limit": "paused"}.get(last.outcome or "", "failed")
            label = {"budget_reached": "Budget reached", "paused": "Paused", "failed": "Needs attention"}[state]
            return {"state": state, "label": label,
                    "explanation": JOB_OUTCOME_TEXT.get(last.outcome or "", "Execution stopped.")}
        if any(t.status == TaskStatus.BLOCKED for t in tasks):
            return {"state": "blocked", "label": "Blocked",
                    "explanation": "Some work is blocked. Continue the run to let Cavman decide how to recover."}
        if last is None:
            return {"state": "starting", "label": "Starting", "explanation": "Queued."}
        return {"state": "waiting", "label": "Waiting for you",
                "explanation": "Cavman paused and is waiting for your direction. Read its latest message, "
                               "then continue the run."}

    @staticmethod
    def stage(run: Run) -> str:
        tasks = [t for t in run.tasks.values() if t.status not in {TaskStatus.CANCELLED, TaskStatus.REPLACED}]
        if run.status == "completed":
            return "deliver"
        from walter.adapter import INITIAL_COMPLETION_CRITERION
        if run.plan.completion_criteria == [INITIAL_COMPLETION_CRITERION]:
            return "understand"
        if not tasks:
            return "plan"
        if all(t.status == TaskStatus.ACCEPTED for t in tasks):
            return "deliver"
        if any(t.status in {TaskStatus.SUBMITTED, TaskStatus.REVIEWING} for t in tasks):
            return "review"
        return "build"

    def job(self, job: Job) -> dict:
        return {"id": job.id, "kind": job.kind, "status": job.status, "outcome": job.outcome,
                "outcome_text": JOB_OUTCOME_TEXT.get(job.outcome or "") if job.outcome else None,
                "message": self.redact(job.detail, limit=4000) if job.detail else None,
                "cancel_requested": job.cancel_requested, "created_at": job.created_at,
                "started_at": job.started_at, "finished_at": job.finished_at}

    def run_summary(self, run: Run, record: RunRecord, jobs: list[Job], project_name: str) -> dict:
        tasks = [t for t in run.tasks.values() if t.status not in {TaskStatus.CANCELLED, TaskStatus.REPLACED}]
        counts: dict[str, int] = {}
        for task in tasks:
            state = task_display_state(run, task)
            counts[state] = counts.get(state, 0) + 1
        usage = self.usage(run, record)
        return {
            "id": run.id,
            "project_id": record.project_id,
            "project_name": project_name,
            "prompt": self.redact(record.prompt, limit=4000),
            "executor": record.executor,
            "model_mode": record.model_mode,
            "status": run.status,
            **self.run_state(run, jobs),
            "stage": self.stage(run),
            "task_counts": counts,
            "tasks_total": len(tasks),
            "tasks_accepted": sum(1 for t in tasks if t.status == TaskStatus.ACCEPTED),
            "pending_approvals": sum(1 for a in run.approvals.values() if a.status == ApprovalStatus.PENDING),
            "failures": len(run.failures),
            "cost_usd": usage["cost_usd"],
            "cost_complete": usage["cost_complete"],
            "model_calls": usage["calls"],
            "created_at": record.created_at,
            "updated_at": run.updated_at,
        }

    def run_detail(self, run: Run, record: RunRecord, jobs: list[Job], events: list[Event],
                   project_name: str, delivery: dict | None) -> dict:
        from walter.adapter import INITIAL_COMPLETION_CRITERION

        latest_by_task: dict[str, dict] = {}
        described = [self.event(run, e) for e in events]
        for item in described:
            if item["task_id"] and not item["debug"]:
                latest_by_task[item["task_id"]] = {"title": item["title"], "created_at": item["created_at"]}
        usage = self.usage(run, record)
        usage_by_task: dict[str, dict] = {}
        for entry in usage["records"]:
            if entry["task_id"]:
                bucket = usage_by_task.setdefault(entry["task_id"], {"calls": 0, "models": [],
                                                                     "cost_usd": 0.0})
                bucket["calls"] += 1
                model = f"{entry['provider']}/{entry['model']}"
                if model not in bucket["models"]:
                    bucket["models"].append(model)
                bucket["cost_usd"] += entry["cost_usd"] or 0.0
        criteria = run.plan.completion_criteria
        order = list(run.plan.task_ids) + [k for k in run.tasks if k not in run.plan.task_ids]
        return {
            **self.run_summary(run, record, jobs, project_name),
            "objective": self.redact(run.objective, limit=4000),
            "constraints": [self.redact(c, limit=500) for c in run.constraints],
            "criteria": [] if criteria == [INITIAL_COMPLETION_CRITERION] else
                        [self.redact(c, limit=500) for c in criteria],
            "tasks": [self.task(run, run.tasks[k], latest_by_task, usage_by_task) for k in order],
            "artifacts": [self.artifact(run, a) for a in sorted(run.artifacts.values(),
                                                                 key=lambda a: a.created_at)],
            "accepted_artifact_ids": list(run.accepted_artifacts),
            "approvals": [self.approval(run, a) for a in sorted(run.approvals.values(),
                                                                 key=lambda a: a.created_at)],
            "capability_requests": [{
                "id": c.id, "task_id": c.task_id, "requested_capability": c.requested_capability.value,
                "reason": self.redact(c.reason, limit=1000), "risk": self.redact(c.risk, limit=1000),
                "status": c.status.value, "created_at": c.created_at,
            } for c in run.capability_requests.values()],
            "failure_details": self.failures(run),
            "recovery": self.replans(run),
            "unresolved_issues": [self.redact(i, limit=500) for i in run.unresolved_issues],
            "usage": usage,
            "jobs": [self.job(j) for j in jobs],
            "event_cursor": run.event_cursor,
            "timeline": [e for e in described if not e["debug"]][-200:],
            "final_result": self.redact(run.final_result, limit=20_000) if run.final_result else None,
            "delivery": delivery,
        }
