"""Scripted test executor: drives the real kernel with scripted models.

Enabled only when ``CAVMAN_EXECUTOR=scripted`` (refused in production). It
exists so end-to-end tests can exercise the whole product -- durable jobs, the
real workflow driver, real Bubblewrap validation, real review and
acceptance gates, real approvals -- without spending provider credits.

Only the *model* is scripted. Every state change still goes through the
orchestration kernel, and every check result comes from trusted execution.
Runs created this way are labelled with executor ``scripted`` everywhere they
are shown.

Scenarios are selected by a tag in the build request:

- default          plan, build and verify two tasks, then finish
- ``#approval``    finish a task, then wait for a human approval before completing
- ``#fail-validation``  first candidate fails its tests; recovery revises it
- ``#dependent``   a second code task imports the first task's accepted code
- ``#parallel``    two independent code tasks; the second is rebuilt on the
                   integrated first (stale base, carried-over retry)
- ``#node``        a TypeScript module checked with Node's test runner
- ``#follow-up``   a later run in an existing project extends the booking core
                   from an earlier run; its tests fail unless that code is present
"""
from __future__ import annotations

import asyncio
import json
import re
import threading
from collections.abc import Callable

from agents.models.interface import Model
from agents.testing import ScriptedModel, assistant_message, function_call
from agents.usage import Usage

from walter import runtime

PROVIDER = "cavman-scripted"
_RAW_USAGE = {"prompt_tokens": 120, "completion_tokens": 40, "total_tokens": 160}
_USAGE = Usage(requests=1, input_tokens=120, output_tokens=40, total_tokens=160)

SPEC_CRITERION = "Product specification accepted after independent review"
CORE_CRITERION = "Booking core implemented with passing sandboxed tests"

BOOKING_MODULE = '''"""Appointment booking core for a small studio."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Slot:
    day: str
    hour: int


class Calendar:
    def __init__(self, open_hour: int = 10, close_hour: int = 18):
        self.open_hour = open_hour
        self.close_hour = close_hour
        self._booked: dict[Slot, str] = {}

    def available(self, day: str) -> list[Slot]:
        return [Slot(day, hour) for hour in range(self.open_hour, self.close_hour)
                if Slot(day, hour) not in self._booked]

    def book(self, slot: Slot, client: str) -> None:
        if not self.open_hour <= slot.hour < self.close_hour:
            raise ValueError("Outside opening hours")
        if slot in self._booked:
            raise ValueError("Slot already booked")
        self._booked[slot] = client
'''

BOOKING_TESTS = '''import pytest

from booking import Calendar, Slot


def test_booking_removes_slot_from_availability():
    calendar = Calendar()
    calendar.book(Slot("mon", 11), "Ada")
    assert Slot("mon", 11) not in calendar.available("mon")
    assert len(calendar.available("mon")) == 7


def test_double_booking_is_refused():
    calendar = Calendar()
    calendar.book(Slot("mon", 12), "Ada")
    with pytest.raises(ValueError):
        calendar.book(Slot("mon", 12), "Grace")
'''

BROKEN_MODULE = BOOKING_MODULE.replace("if slot in self._booked:", "if False:")

_SPEC_PACKET = {
    "task_id": "spec",
    "role": "Product specialist",
    "objective": "Write the product specification for the booking app",
    "deliverable": "A concise specification covering users, booking rules and screens",
    "acceptance_criteria": ["Specification defines booking rules and double-booking behaviour"],
    "stop_condition": "Specification returned or genuinely blocked",
}

_CORE_PACKET = {
    "task_id": "core",
    "role": "Backend specialist",
    "objective": "Implement the booking core with tests",
    "deliverable": "booking.py with a Calendar supporting availability and booking, plus tests",
    "acceptance_criteria": ["Double booking is refused", "Tests cover availability and booking"],
    "stop_condition": "Module and tests written and self-checked, or genuinely blocked",
}


def _tool(name: str, arguments: dict, call_id: str) -> dict:
    return {"output": [function_call(name, arguments, call_id=call_id)], "raw_usage": dict(_RAW_USAGE)}


def _message(text: str) -> dict:
    return {"output": [assistant_message(text)], "raw_usage": dict(_RAW_USAGE)}


def _worker_result(task_id: str, deliverable: str, summary: str) -> dict:
    return _message(json.dumps({
        "task_id": task_id, "status": "completed", "summary": summary, "deliverable": deliverable,
        "evidence": ["Produced inside the assigned lane"],
    }))


def _plan_item_numbers(call_input) -> list[int]:
    """The numbered plan items a reviewer was asked to rule on."""
    for entry in [call_input] if isinstance(call_input, str) else call_input:
        content = entry if isinstance(entry, str) else entry.get("content")
        if isinstance(content, list):
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        try:
            return [item["item"] for item in json.loads(content)["plan_items"]]
        except (TypeError, ValueError, KeyError):
            continue
    return []


def _review(passed: bool, reason: str) -> dict:
    def responder(call):
        verdicts = [{"item": number, "met": passed, "evidence": reason}
                    for number in _plan_item_numbers(call.input)]
        return _message(json.dumps({"passed": passed, "evidence": [reason], "reason": reason,
                                    "verdicts": verdicts}))
    return {"responder": responder}


API_MODULE = '''"""Booking endpoint logic built on the accepted booking core."""
from booking import Calendar, Slot


def book(calendar: Calendar, day: str, hour: int, client: str) -> dict:
    try:
        calendar.book(Slot(day, hour), client)
    except ValueError as error:
        return {"ok": False, "error": str(error)}
    return {"ok": True}
'''

API_TESTS = '''from api import book
from booking import Calendar


def test_book_reports_conflicts():
    calendar = Calendar()
    assert book(calendar, "tue", 11, "Ada") == {"ok": True}
    assert book(calendar, "tue", 11, "Grace")["ok"] is False
'''

CANCEL_MODULE = '''"""Cancellation, built on the booking core delivered by an earlier run."""
from booking import Calendar, Slot


def cancel(calendar: Calendar, slot: Slot) -> bool:
    return calendar._booked.pop(slot, None) is not None
'''

CANCEL_TESTS = '''from booking import Calendar, Slot
from cancel import cancel


def test_cancel_frees_the_slot():
    calendar = Calendar()
    calendar.book(Slot("wed", 12), "Ada")
    assert cancel(calendar, Slot("wed", 12)) is True
    assert Slot("wed", 12) in calendar.available("wed")
    assert cancel(calendar, Slot("wed", 12)) is False
'''

_CANCEL_PACKET = {
    "task_id": "cancel",
    "role": "Backend specialist",
    "objective": "Add cancellation to the existing booking core",
    "deliverable": "cancel.py with a cancel() function using the existing Calendar, plus tests",
    "acceptance_criteria": ["A cancelled slot becomes available again"],
    "stop_condition": "Module and tests written and self-checked, or genuinely blocked",
}

NOTIFY_MODULE = '''"""Reminder text for upcoming appointments."""


def reminder(client: str, day: str, hour: int) -> str:
    return f"Hi {client}, see you {day} at {hour}:00."
'''

NOTIFY_TESTS = '''from notify import reminder


def test_reminder_mentions_time():
    assert reminder("Ada", "mon", 11) == "Hi Ada, see you mon at 11:00."
'''

_API_PACKET = {
    "task_id": "api",
    "role": "API specialist",
    "objective": "Add booking endpoint logic on top of the booking core",
    "deliverable": "api.py with a book() function using the Calendar, plus tests",
    "acceptance_criteria": ["Conflicting bookings are reported, not raised"],
    "stop_condition": "Module and tests written and self-checked, or genuinely blocked",
}

_NOTIFY_PACKET = {
    "task_id": "notify",
    "role": "Messaging specialist",
    "objective": "Write appointment reminder text",
    "deliverable": "notify.py with a reminder() function, plus tests",
    "acceptance_criteria": ["Reminder names the client, day and hour"],
    "stop_condition": "Module and tests written and self-checked, or genuinely blocked",
}


SLOTS_MODULE = '''export type Slot = { day: string; hour: number };

export function openSlots(day: string, booked: Slot[], open = 10, close = 18): Slot[] {
  const taken = new Set(booked.filter((s) => s.day === day).map((s) => s.hour));
  const slots: Slot[] = [];
  for (let hour = open; hour < close; hour++) if (!taken.has(hour)) slots.push({ day, hour });
  return slots;
}
'''

SLOTS_TESTS = '''import { test } from "node:test";
import assert from "node:assert/strict";
import { openSlots } from "./slots.ts";

test("booked hours are not offered", () => {
  const slots = openSlots("mon", [{ day: "mon", hour: 11 }]);
  assert.equal(slots.length, 7);
  assert.ok(!slots.some((s) => s.hour === 11));
});
'''

_SLOTS_PACKET = {
    "task_id": "slots",
    "role": "Frontend specialist",
    "objective": "Implement the TypeScript availability helper for the booking UI",
    "deliverable": "slots.ts exporting openSlots(), with node:test tests in slots.test.ts",
    "acceptance_criteria": ["Booked hours are never offered", "Tests run with node --test"],
    "stop_condition": "Module and tests written and self-checked, or genuinely blocked",
}
NODE_CRITERION = "TypeScript availability helper implemented with passing Node tests"


def _write(task_id: str, prefix: str, files: dict[str, str], summary: str) -> list[dict]:
    steps = [_tool("write_file", {"path": path, "content": content}, f"{prefix}-{index}")
             for index, (path, content) in enumerate(files.items())]
    return steps + [_worker_result(task_id, "Wrote " + ", ".join(files) + ".", summary)]


def _write_code(prefix: str, module: str) -> list[dict]:
    return [
        _tool("write_file", {"path": "booking.py", "content": module}, f"{prefix}-module"),
        _tool("write_file", {"path": "test_booking.py", "content": BOOKING_TESTS}, f"{prefix}-tests"),
        _worker_result("core", "Implemented booking.py and test_booking.py.", "Booking core implemented"),
    ]


def scenario_for(objective: str) -> str:
    text = objective.lower()
    if "#approval" in text:
        return "approval"
    if "#fail-validation" in text:
        return "fail-validation"
    if "#dependent" in text:
        return "dependent"
    if "#parallel" in text:
        return "parallel"
    if "#node" in text:
        return "node"
    if "#follow-up" in text:
        return "follow-up"
    return "complete"


class _PacedModel(ScriptedModel):
    """A scripted model that takes a little time per step, like a real provider."""

    def __init__(self, steps, delay: float):
        super().__init__(steps, default_usage=_USAGE)
        self._delay = delay

    async def get_response(self, *args, **kwargs):
        if self._delay:
            await asyncio.sleep(self._delay)
        return await super().get_response(*args, **kwargs)


# Workflow-mode scripts -------------------------------------------------------
#
# The planner model only plans, and specialists for
# independent tasks run concurrently, so one ordered script cannot describe
# them. Specialist steps are therefore keyed by (task_id, role) and routed by
# the task named in each call's input.

_TASK_ID = re.compile(r'\\?"task_id\\?":\s*\\?"([A-Za-z0-9_.:-]+)')


class _RoutedModel(Model):
    def __init__(self, scripts: dict[tuple[str, str], list], delay: float):
        self._models = {key: _PacedModel(steps, delay) for key, steps in scripts.items()}

    def _route(self, system_instructions, input) -> _PacedModel:
        role = "reviewer" if (system_instructions or "").startswith("You are a fresh independent reviewer") else "worker"
        match = _TASK_ID.search(input if isinstance(input, str) else json.dumps(input, default=str))
        key = (match.group(1) if match else "", role)
        if key not in self._models:
            raise RuntimeError(f"No scripted steps for {key}")
        return self._models[key]

    async def get_response(self, system_instructions, input, *args, **kwargs):
        return await self._route(system_instructions, input).get_response(system_instructions, input, *args, **kwargs)

    async def stream_response(self, *args, **kwargs):
        raise NotImplementedError("Scripted workflow models do not stream")
        yield  # pragma: no cover

    def get_retry_advice(self, request):
        return None

    async def close(self):
        return None


def _plan(criteria: list[str], tasks: list[tuple[dict, str, list[str], list[int]]]) -> dict:
    return _message(json.dumps({"criteria": criteria, "tasks": [
        {"packet": packet, "capability": capability, "checks": checks, "covers": covers}
        for packet, capability, checks, covers in tasks]}))


def build_workflow_scripts(scenario: str, kind: str) -> tuple[list, dict]:
    """Return (planner_steps, {(task_id, role): steps}) for one job of a scenario."""
    review_pass = _review(True, "Candidate satisfies every acceptance criterion")
    reviewer = [_tool("read_file", {"path": "booking.py"}, "review-read"), review_pass]
    retry = lambda task_id: [_tool("inspect_diff", {}, f"{task_id}-retry-diff"),  # noqa: E731
                             _worker_result(task_id, "Verified the carried-over work on the latest code.",
                                            "Rebased onto integrated code")]
    if scenario == "complete":
        return [_plan([SPEC_CRITERION, CORE_CRITERION], [
            (_SPEC_PACKET, "model_only", ["result_schema"], [0]),
            ({**_CORE_PACKET, "dependencies": ["spec"]}, "developer_sandbox", ["compile", "pytest"], [1])])], {
            ("spec", "worker"): [_worker_result(
                "spec", "Specification: clients pick an open hourly slot between 10:00 and 18:00; a slot can "
                "be booked once; double booking is refused.", "Specification written")],
            ("spec", "reviewer"): [review_pass],
            ("core", "worker"): _write_code("core", BOOKING_MODULE),
            ("core", "reviewer"): list(reviewer)}
    if scenario == "fail-validation":
        return [_plan([CORE_CRITERION], [(_CORE_PACKET, "developer_sandbox", ["pytest"], [0])])], {
            ("core", "worker"): [*_write_code("v1", BROKEN_MODULE), *_write_code("v2", BOOKING_MODULE)],
            ("core", "reviewer"): list(reviewer)}
    if scenario == "dependent":
        api_criterion = "Booking endpoint logic builds on the accepted core with passing tests"
        return [_plan([CORE_CRITERION, api_criterion], [
            (_CORE_PACKET, "developer_sandbox", ["pytest"], [0]),
            ({**_API_PACKET, "dependencies": ["core"]}, "developer_sandbox", ["pytest", "pytest_regression"], [1])])], {
            ("core", "worker"): _write_code("core", BOOKING_MODULE),
            ("core", "reviewer"): list(reviewer),
            ("api", "worker"): _write("api", "api", {"api.py": API_MODULE, "test_api.py": API_TESTS},
                                      "Endpoint logic written"),
            ("api", "reviewer"): [_tool("read_file", {"path": "api.py"}, "review-api"), review_pass]}
    if scenario == "parallel":
        notify_criterion = "Reminder text implemented with passing sandboxed tests"
        # Both tasks run at once; whichever is accepted second is rebuilt on the
        # integrated first, so both carry optional retry steps.
        return [_plan([CORE_CRITERION, notify_criterion], [
            (_CORE_PACKET, "developer_sandbox", ["pytest"], [0]),
            (_NOTIFY_PACKET, "developer_sandbox", ["pytest"], [1])])], {
            ("core", "worker"): [*_write_code("core", BOOKING_MODULE), *retry("core")],
            ("core", "reviewer"): reviewer + reviewer,
            ("notify", "worker"): [*_write("notify", "notify", {"notify.py": NOTIFY_MODULE,
                                                                 "test_notify.py": NOTIFY_TESTS},
                                           "Reminder text written"), *retry("notify")],
            ("notify", "reviewer"): [_tool("read_file", {"path": "notify.py"}, "r1"), review_pass,
                                     _tool("read_file", {"path": "notify.py"}, "r2"), review_pass]}
    if scenario == "node":
        return [_plan([NODE_CRITERION], [(_SLOTS_PACKET, "developer_sandbox", ["node_test"], [0])])], {
            ("slots", "worker"): _write("slots", "slots", {"slots.ts": SLOTS_MODULE, "slots.test.ts": SLOTS_TESTS},
                                        "Availability helper written"),
            ("slots", "reviewer"): [_tool("read_file", {"path": "slots.ts"}, "review-slots"), review_pass]}
    if scenario == "follow-up":
        criterion = "Cancelling a booked slot makes it available again, with passing tests"
        return [_plan([criterion], [(_CANCEL_PACKET, "developer_sandbox", ["pytest", "pytest_regression"], [0])])], {
            ("cancel", "worker"): [_tool("read_file", {"path": "booking.py"}, "cancel-read"),
                                   *_write("cancel", "cancel", {"cancel.py": CANCEL_MODULE,
                                                                "test_cancel.py": CANCEL_TESTS},
                                           "Cancellation written")],
            ("cancel", "reviewer"): [_tool("read_file", {"path": "cancel.py"}, "review-cancel"), review_pass]}
    if scenario == "approval":
        if kind == "start":
            # The plan under-provisions the task; its specialist asks for a sandbox.
            return [_plan([CORE_CRITERION], [(_CORE_PACKET, "model_only", ["pytest"], [0])])], {
                ("core", "worker"): [_message(json.dumps({
                    "task_id": "core", "status": "blocked", "summary": "Needs a sandbox to write and test code",
                    "deliverable": "", "blocker": "No write or test tools were granted",
                    "capability_request": {"requested_capability": "developer_sandbox",
                                           "reason": "Writing booking.py and running its tests needs a sandbox",
                                           "risk": "Executes candidate code inside the isolated sandbox"}}))]}
        return [], {("core", "worker"): _write_code("core", BOOKING_MODULE), ("core", "reviewer"): list(reviewer)}
    raise ValueError(f"Unknown scripted scenario {scenario}")


class ScriptedProvider:
    """Registered with the runtime's provider seam; one model pair per job."""

    def __init__(self, load_run_for: Callable[[str], Callable], delay: float):
        self._load_run_for = load_run_for
        self._delay = delay
        self._pairs: dict[str, tuple] = {}
        self._jobs: dict[str, tuple[str, str]] = {}
        self._lock = threading.Lock()

    def prepare(self, job_key: str, run_id: str, kind: str) -> None:
        with self._lock:
            self._jobs[job_key] = (run_id, kind)

    def release(self, job_key: str) -> None:
        with self._lock:
            self._jobs.pop(job_key, None)
            self._pairs.pop(job_key, None)

    def __call__(self, config: runtime.RuntimeConfig) -> tuple:
        job_key = config.api_key
        with self._lock:
            if job_key not in self._pairs:
                run_id, kind = self._jobs[job_key]
                load_run = self._load_run_for(run_id)
                scenario = scenario_for(load_run().objective)
                planner, specialists = build_workflow_scripts(scenario, kind)
                self._pairs[job_key] = (_PacedModel(planner, self._delay),
                                        _RoutedModel(specialists, self._delay))
            return self._pairs[job_key]


def scripted_config(job_key: str, budget) -> runtime.RuntimeConfig:
    # The job key rides in api_key (never displayed or recorded); model names stay readable.
    return runtime.RuntimeConfig(provider=PROVIDER, api_key=job_key,
                                 base_url="https://scripted.invalid", manager_model="scripted-manager",
                                 worker_model="scripted-specialist", budget=budget)
