"""Pure, metadata-only Project Pulse snapshots and deterministic digests.

No database, provider, file, or network access occurs here. Free-form objectives,
artifact contents, approval scopes, blocker evidence, and event payloads are never
copied into a snapshot. Identifiers are metadata, not a secret-detection boundary.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from .models import Event, Run, TaskStatus


def _get(value: Any, key: str, default: Any = None) -> Any:
    return value.get(key, default) if isinstance(value, Mapping) else getattr(value, key, default)


def _identifier(value: Any) -> str:
    """Keep metadata single-line and bounded; never stringify arbitrary objects."""
    return " ".join(value.split())[:128] if isinstance(value, str) else "unknown"


def _integer(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _records(value: Any) -> list[tuple[str, Any]]:
    if not isinstance(value, Mapping):
        return []
    return sorted(((str(key), record) for key, record in value.items()), key=lambda item: item[0])


def normalize_snapshot(run: Run | Mapping[str, Any], *,
                       events: Iterable[Event | Mapping[str, Any]] = ()) -> dict[str, Any]:
    """Copy only operational metadata from a Run or its JSON-like mapping.

    Unknown task states remain UNKNOWN instead of being counted as successful.
    Acceptance requires both membership in accepted_artifacts and an accepted
    artifact record. Event data, request rationale/scope, and task packet text
    are deliberately excluded. Source objects are never changed.
    """
    if not isinstance(run, (Run, Mapping)):
        raise TypeError("Expected a Run or mapping")
    states = {state.value for state in TaskStatus}
    tasks = []
    for key, task in _records(_get(run, "tasks", {})):
        status = _get(task, "status")
        tasks.append({"id": _identifier(key), "status": status if status in states else "UNKNOWN"})
    approvals = [{"id": _identifier(key)}
                 for key, record in _records(_get(run, "approvals", {}))
                 if _get(record, "status") == "pending"]
    capabilities = [{"id": _identifier(key), "task_id": _identifier(_get(record, "task_id"))}
                    for key, record in _records(_get(run, "capability_requests", {}))
                    if _get(record, "status") == "pending"]
    artifacts = _get(run, "artifacts", {})
    accepted = []
    seen = set()
    for artifact_id in _get(run, "accepted_artifacts", []) or []:
        if not isinstance(artifact_id, str) or artifact_id in seen:
            continue
        seen.add(artifact_id)
        artifact = _get(artifacts, artifact_id)
        if artifact is not None and _get(artifact, "status") == "accepted":
            accepted.append({"id": _identifier(artifact_id),
                             "task_id": _identifier(_get(artifact, "task_id"))})
    run_id = _get(run, "id")
    event_records = [{"sequence": _integer(_get(event, "sequence")),
                      "kind": _identifier(_get(event, "kind"))}
                     for event in events if _get(event, "run_id", run_id) == run_id]
    event_records.sort(key=lambda event: (event["sequence"], event["kind"]))
    status = _get(run, "status")
    return {"id": _identifier(run_id),
            "status": status if status in {"active", "completed", "abandoned"} else "unknown",
            "version": _integer(_get(run, "version")),
            "event_cursor": _integer(_get(run, "event_cursor")),
            "tasks": tasks, "pending_approvals": approvals,
            "pending_capability_requests": capabilities,
            "accepted_artifacts": accepted, "events": event_records}


def summarize_run(run: Run | Mapping[str, Any], *,
                  events: Iterable[Event | Mapping[str, Any]] = (),
                  previous: Run | Mapping[str, Any] | None = None,
                  recent_limit: int = 5) -> dict[str, Any]:
    """Summarize one run; optionally compare with an earlier raw run snapshot.

    Recent artifacts follow the authoritative acceptance-list order (newest
    first), not artifact creation time. Recent events use descending sequence.
    has_changes is None without a baseline; comparisons include durable version
    and cursor, so a payload-only update is still reported as a change.
    """
    if isinstance(recent_limit, bool) or not isinstance(recent_limit, int) or recent_limit < 0:
        raise ValueError("recent_limit must be a nonnegative integer")
    snapshot = normalize_snapshot(run, events=events)
    baseline = normalize_snapshot(previous) if previous is not None else None
    if baseline is not None and baseline["id"] != snapshot["id"]:
        raise ValueError("Previous snapshot belongs to a different run")
    counts = {state.value: 0 for state in TaskStatus}
    unknown = []
    for task in snapshot["tasks"]:
        if task["status"] in counts:
            counts[task["status"]] += 1
        else:
            unknown.append(task["id"])
    comparable = {key: value for key, value in snapshot.items() if key != "events"}
    prior = {key: value for key, value in baseline.items() if key != "events"} if baseline else None
    return {"run_id": snapshot["id"], "status": snapshot["status"],
            "task_counts": counts, "total_tasks": len(snapshot["tasks"]),
            "unknown_tasks": unknown,
            "blocked_tasks": [task["id"] for task in snapshot["tasks"] if task["status"] == "BLOCKED"],
            "failed_tasks": [task["id"] for task in snapshot["tasks"] if task["status"] == "FAILED"],
            "pending_approvals": snapshot["pending_approvals"],
            "pending_capability_requests": snapshot["pending_capability_requests"],
            "accepted_artifact_count": len(snapshot["accepted_artifacts"]),
            "recent_accepted_artifacts": list(reversed(snapshot["accepted_artifacts"]))[:recent_limit],
            "recent_events": list(reversed(snapshot["events"]))[:recent_limit],
            "has_changes": comparable != prior if baseline is not None else None}


def render_digest(summary: Mapping[str, Any]) -> str:
    """Render a summary returned by summarize_run without inferring completion."""
    counts = summary["task_counts"]
    task_text = ", ".join(f"{state.value} {counts[state.value]}" for state in TaskStatus if counts[state.value])
    lines = [f"Project Pulse: {summary['run_id']} ({summary['status']})",
             f"Tasks: {task_text or 'none'}"]
    if summary["has_changes"] is False:
        lines.append("No changes since previous snapshot.")
    for label, key in (("Blocked", "blocked_tasks"), ("Failed", "failed_tasks"), ("Unknown task states", "unknown_tasks")):
        if summary[key]:
            lines.append(f"{label}: {', '.join(summary[key])}")
    lines.append(f"Pending: {len(summary['pending_approvals'])} approvals, "
                 f"{len(summary['pending_capability_requests'])} capability requests")
    accepted = summary["recent_accepted_artifacts"]
    lines.append("Recent accepted artifacts: " + (", ".join(item["id"] for item in accepted) or "none"))
    if summary["recent_events"]:
        lines.append("Recent events: " + ", ".join(
            f"#{event['sequence']} {event['kind']}" for event in summary["recent_events"]))
    return "\n".join(lines)
