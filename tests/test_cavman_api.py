"""Cavman API and worker: ownership, trust boundaries, durable execution, delivery.

Runs use the scripted test executor, so the Manager's *model* is scripted while
every state change goes through the real kernel and every check runs in the real
Bubblewrap sandbox.
"""
import asyncio
import io
import json
import os
import tarfile
import time
import uuid
from pathlib import Path

import pytest

pytest.importorskip("agents")
pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from cavman.api import create_app, project_name_from_prompt
from cavman.config import Settings, SettingsError
from cavman.platform_store import PlatformStore
from cavman.worker import Worker

TOKEN = "t" * 40
ALICE = {"Authorization": f"Bearer {TOKEN}", "X-Cavman-User": "alice"}
MALLORY = {"Authorization": f"Bearer {TOKEN}", "X-Cavman-User": "mallory"}


def sandbox_available() -> bool:
    return Path("/usr/bin/bwrap").exists() and Path("/usr/bin/prlimit").exists()


needs_sandbox = pytest.mark.skipif(not sandbox_available(), reason="Bubblewrap sandbox unavailable")


@pytest.fixture
def settings(tmp_path):
    # CAVMAN_TEST_DATABASE_URL runs this suite with all state in PostgreSQL,
    # each test in its own schemas.
    database_url = os.environ.get("CAVMAN_TEST_DATABASE_URL") or None
    return Settings(data_dir=tmp_path / "data", api_token=TOKEN, executor="scripted",
                    scripted_step_delay=0, heartbeat_seconds=0.2, lease_seconds=30,
                    stream_max_seconds=1.5, stream_poll_seconds=0.1, database_url=database_url,
                    database_schema="t_" + uuid.uuid4().hex[:12] if database_url else "cavman")


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def drain(settings):
    worker = Worker(settings)
    try:
        while asyncio.run(worker.run_once()):
            pass
    finally:
        worker.close()


def build(client, prompt="Build me a booking app for a tattoo studio", headers=ALICE, **extra):
    response = client.post("/api/builds", json={"prompt": prompt, **extra}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def test_settings_require_strong_service_token_and_refuse_scripted_production(tmp_path):
    with pytest.raises(SettingsError, match="CAVMAN_API_TOKEN"):
        Settings.from_env({"CAVMAN_API_TOKEN": "short"})
    with pytest.raises(SettingsError, match="production"):
        Settings.from_env({"CAVMAN_API_TOKEN": TOKEN, "CAVMAN_EXECUTOR": "scripted",
                           "CAVMAN_ENV": "production"})
    with pytest.raises(SettingsError, match="cannot exceed"):
        Settings.from_env({"CAVMAN_API_TOKEN": TOKEN, "CAVMAN_DEFAULT_BUDGET_USD": "50",
                           "CAVMAN_MAX_BUDGET_USD": "10"})
    settings = Settings.from_env({"CAVMAN_API_TOKEN": TOKEN, "CAVMAN_DATA_DIR": str(tmp_path)})
    assert settings.default_budget_usd == 5.0 and settings.default_max_model_calls == 300


def test_service_token_and_user_identity_are_required(client):
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/projects").status_code == 401
    assert client.get("/api/projects", headers={"Authorization": "Bearer wrong",
                                                "X-Cavman-User": "alice"}).status_code == 401
    assert client.get("/api/projects", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 401
    assert client.get("/api/projects", headers={"Authorization": f"Bearer {TOKEN}",
                                                "X-Cavman-User": "bad user!"}).status_code == 401


def test_a_web_server_from_before_the_rename_is_still_trusted(client):
    created = build(client)
    old_web = {"Authorization": f"Bearer {TOKEN}", "X-Caveman-User": "alice"}
    assert client.get(f"/api/runs/{created['run_id']}", headers=old_web).status_code == 200
    assert client.get(f"/api/runs/{created['run_id']}",
                      headers={**old_web, "X-Caveman-User": "mallory"}).status_code == 404
    assert client.get("/api/projects", headers={"Authorization": "Bearer wrong",
                                                "X-Caveman-User": "alice"}).status_code == 401


def test_runs_and_projects_are_private_to_their_owner(client):
    created = build(client)
    run_id, project_id = created["run_id"], created["project_id"]
    assert client.get(f"/api/runs/{run_id}", headers=ALICE).status_code == 200
    for path in (f"/api/runs/{run_id}", f"/api/projects/{project_id}", f"/api/runs/{run_id}/events",
                 f"/api/runs/{run_id}/delivery/download"):
        assert client.get(path, headers=MALLORY).status_code == 404, path
    assert client.post(f"/api/runs/{run_id}/stop", headers=MALLORY).status_code == 404
    assert client.get("/api/runs", headers=MALLORY).json() == {"runs": [], "next": None}
    other = client.post("/api/builds", json={"prompt": "Hijack", "project_id": project_id}, headers=MALLORY)
    assert other.status_code == 404


def test_browser_cannot_forge_kernel_evidence(client):
    run_id = build(client)["run_id"]
    forged = [
        ("post", f"/api/runs/{run_id}/tasks/core/accept"),
        ("post", f"/api/runs/{run_id}/validations"),
        ("post", f"/api/runs/{run_id}/reviews"),
        ("post", f"/api/runs/{run_id}/complete"),
        ("post", f"/api/runs/{run_id}/artifacts"),
    ]
    for method, path in forged:
        assert getattr(client, method)(path, json={"passed": True}, headers=ALICE).status_code in {404, 405}


def test_new_build_is_queued_not_executed_in_the_request(client, settings):
    created = build(client)
    assert created["run"]["state"] == "starting"
    platform = settings.open_platform_store()
    try:
        jobs = platform.jobs(created["run_id"])
    finally:
        platform.close()
    assert [(job.kind, job.status) for job in jobs] == [("start", "queued")]
    detail = client.get(f"/api/runs/{created['run_id']}", headers=ALICE).json()
    assert detail["tasks"] == [] and detail["artifacts"] == [] and detail["usage"]["calls"] == 0
    assert detail["project_name"] == "Booking app for a tattoo studio"


def test_provider_must_be_configured_before_a_run_exists(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    settings = Settings(data_dir=tmp_path / "data", api_token=TOKEN)
    with TestClient(create_app(settings)) as client:
        response = client.post("/api/builds", json={"prompt": "Build a CLI"}, headers=ALICE)
        assert response.status_code == 503
        assert "not configured" in response.json()["detail"]
        assert client.get("/api/runs", headers=ALICE).json() == {"runs": [], "next": None}
        system = client.get("/api/system", headers=ALICE).json()
        assert system["provider"]["configured"] is False
        assert "OPENROUTER_API_KEY" not in json.dumps(system).replace("OPENROUTER_API_KEY is not set", "")


def test_budget_ceiling_is_bounded(client):
    response = client.post("/api/builds", json={"prompt": "Build an API", "settings": {"budget_usd": 1000}},
                           headers=ALICE)
    assert response.status_code == 422
    run_id = build(client, settings={"budget_usd": 2.5})["run_id"]
    detail = client.get(f"/api/runs/{run_id}", headers=ALICE).json()
    assert detail["usage"]["budget"]["limit_usd"] == 2.5
    assert client.patch(f"/api/runs/{run_id}/budget", json={"budget_usd": 7}, headers=ALICE).status_code == 200
    assert client.patch(f"/api/runs/{run_id}/budget", json={"budget_usd": 700}, headers=ALICE).status_code == 422


@needs_sandbox
def test_completed_run_exposes_real_state_and_verified_delivery(client, settings):
    run_id = build(client)["run_id"]
    drain(settings)
    detail = client.get(f"/api/runs/{run_id}", headers=ALICE).json()
    assert detail["state"] == "complete" and detail["stage"] == "deliver"
    assert [t["state"] for t in detail["tasks"]] == ["Accepted", "Accepted"]
    core = next(t for t in detail["tasks"] if t["id"] == "core")
    assert {c["check"]: c["status"] for c in core["required_checks"]} == {"compile": "passed", "pytest": "passed"}
    code = next(a for a in detail["artifacts"] if a["task_id"] == "core")
    assert code["kind"] == "code_change" and code["changed_files"] == ["booking.py", "test_booking.py"]
    pytest_record = next(v for v in code["validations"] if v["check"] == "pytest")
    assert pytest_record["trusted"] and pytest_record["returncode"] == 0 and "2 passed" in pytest_record["output"]
    assert code["review_state"] == "passed"
    titles = [e["title"] for e in detail["timeline"]]
    assert "Build complete" in titles and "Validation passed: Candidate tests" in titles
    assert detail["usage"]["calls"] > 0 and detail["usage"]["cost_complete"] is False
    assert detail["delivery"]["status"] == "ready"
    download = client.get(f"/api/runs/{run_id}/delivery/download", headers=ALICE)
    assert download.status_code == 200
    with tarfile.open(fileobj=io.BytesIO(download.content)) as archive:
        names = archive.getnames()
        root = names[0].split("/")[0]
        assert f"{root}/booking.py" in names and f"{root}/CAVMAN_BUILD_REPORT.md" in names
        assert f"{root}/docs/cavman/01-spec.md" in names
        assert not any("/.local" in n or n.endswith(".env") for n in names)
    artifact = client.get(f"/api/runs/{run_id}/artifacts/{code['id']}", headers=ALICE).json()
    assert "class Calendar" in artifact["diff"]
    assert str(settings.data_dir) not in json.dumps(detail) + json.dumps(artifact)


@needs_sandbox
def test_failed_validation_is_shown_with_its_recovery(client, settings):
    run_id = build(client, prompt="Booking core #fail-validation")["run_id"]
    drain(settings)
    detail = client.get(f"/api/runs/{run_id}", headers=ALICE).json()
    assert detail["state"] == "complete"
    [failure] = detail["failure_details"]
    assert failure["classification"] == "BAD_OUTPUT"
    assert failure["recovery"]["action"] == "REVISE"
    first, second = [a for a in detail["artifacts"] if a["task_id"] == "core"]
    assert first["status"] == "rejected" and first["validation_state"] == "failed"
    assert second["status"] == "accepted" and second["validation_state"] == "passed"
    assert any(e["title"] == "Validation failed: Candidate tests" for e in detail["timeline"])


@needs_sandbox
def test_approval_is_exact_scoped_and_resumes_execution(client, settings):
    run_id = build(client, prompt="Booking core #approval")["run_id"]
    drain(settings)
    detail = client.get(f"/api/runs/{run_id}", headers=ALICE).json()
    assert detail["state"] == "approval_needed"
    [approval] = [a for a in detail["approvals"] if a["status"] == "pending"]
    assert approval["scope"]["task_id"] == "core" and approval["why"]
    stale = client.post(f"/api/runs/{run_id}/approvals/{approval['id']}",
                        json={"decision": "approve", "scope_digest": "0" * 64}, headers=ALICE)
    assert stale.status_code == 409
    assert client.post(f"/api/runs/{run_id}/approvals/{approval['id']}",
                       json={"decision": "approve", "scope_digest": approval["scope_digest"]},
                       headers=MALLORY).status_code == 404
    decided = client.post(f"/api/runs/{run_id}/approvals/{approval['id']}",
                          json={"decision": "approve", "scope_digest": approval["scope_digest"]}, headers=ALICE)
    assert decided.status_code == 200 and decided.json()["continued"] is True
    again = client.post(f"/api/runs/{run_id}/approvals/{approval['id']}",
                        json={"decision": "reject", "scope_digest": approval["scope_digest"]}, headers=ALICE)
    assert again.status_code == 409
    drain(settings)
    detail = client.get(f"/api/runs/{run_id}", headers=ALICE).json()
    assert detail["state"] == "complete"
    decision = detail["approvals"][0]["decision"]
    assert decision["approved"] is True and decision["decided_by"] == "cavman-user:alice"


def test_stop_cancels_queued_work_and_continue_requeues(client, settings):
    run_id = build(client)["run_id"]
    stopped = client.post(f"/api/runs/{run_id}/stop", headers=ALICE)
    assert stopped.status_code == 202 and stopped.json()["job"]["status"] == "cancelled"
    assert client.get(f"/api/runs/{run_id}", headers=ALICE).json()["state"] == "paused"
    assert client.post(f"/api/runs/{run_id}/stop", headers=ALICE).status_code == 409
    assert client.post(f"/api/runs/{run_id}/continue", json={}, headers=ALICE).status_code == 202
    assert client.post(f"/api/runs/{run_id}/continue", json={}, headers=ALICE).status_code == 409


def test_abandon_uses_kernel_rules(client):
    run_id = build(client)["run_id"]
    assert client.post(f"/api/runs/{run_id}/abandon", json={"reason": "Changed my mind"},
                       headers=ALICE).status_code == 409  # still queued
    client.post(f"/api/runs/{run_id}/stop", headers=ALICE)
    closed = client.post(f"/api/runs/{run_id}/abandon", json={"reason": "Changed my mind"}, headers=ALICE)
    assert closed.status_code == 200 and closed.json()["state"] == "cancelled"


def test_expired_lease_becomes_explicit_recovery(settings, client):
    run_id = build(client)["run_id"]
    platform = settings.open_platform_store()
    try:
        job = platform.claim("dead-worker", lease_seconds=0.01)
        assert job.run_id == run_id
        time.sleep(0.05)
        [recovery] = platform.reap_expired(max_recoveries=3)
        assert recovery.kind == "recover" and recovery.status == "queued"
        assert [j.outcome for j in platform.jobs(run_id)][0] == "interrupted"
        assert platform.heartbeat(job.id, "dead-worker", 30) is True  # lost lease stops the old worker
    finally:
        platform.close()
    assert client.get(f"/api/runs/{run_id}", headers=ALICE).json()["state"] == "recovering"


def test_event_stream_emits_state(client):
    run_id = build(client)["run_id"]
    with client.stream("GET", f"/api/runs/{run_id}/stream", headers=ALICE) as response:
        assert response.headers["content-type"].startswith("text/event-stream")
        seen = list(response.iter_lines())  # the server closes the stream at its deadline
    assert "event: state" in seen
    assert any(line.startswith("id: 1") for line in seen)
    assert any('"title": "Build requested"' in line for line in seen)


def test_project_names_come_from_the_request():
    assert project_name_from_prompt("Build me a booking app for a tattoo studio") == \
        "Booking app for a tattoo studio"
    assert project_name_from_prompt("  make an API for invoices #approval ") == "API for invoices"
    assert len(project_name_from_prompt("Build " + "very long words " * 20)) <= 60


@needs_sandbox
def test_spending_ceiling_pauses_the_run_safely(settings):
    from dataclasses import replace
    limited = replace(settings, default_max_model_calls=3)
    with TestClient(create_app(limited)) as client:
        run_id = build(client)["run_id"]
        drain(limited)
        detail = client.get(f"/api/runs/{run_id}", headers=ALICE).json()
    assert detail["state"] == "budget_reached"
    assert detail["jobs"][-1]["outcome"] == "budget_exceeded"
    assert detail["usage"]["calls"] == 3 and detail["usage"]["budget"]["exceeded"] is True


@needs_sandbox
def test_stopping_a_running_build_records_interruption_honestly(settings):
    from dataclasses import replace
    slow = replace(settings, scripted_step_delay=0.3, heartbeat_seconds=0.1)
    with TestClient(create_app(slow)) as client:
        run_id = build(client)["run_id"]

        async def scenario():
            worker = Worker(slow)
            try:
                running = asyncio.create_task(worker.run_once())
                await asyncio.sleep(1.2)
                assert client.post(f"/api/runs/{run_id}/stop", headers=ALICE).status_code == 202
                await running
            finally:
                worker.close()
        asyncio.run(scenario())
        detail = client.get(f"/api/runs/{run_id}", headers=ALICE).json()
    assert detail["state"] == "paused" and detail["jobs"][-1]["outcome"] == "cancelled"
    assert detail["status"] == "active"
    assert not any(t["status"] in {"DELEGATED", "RUNNING"} for t in detail["tasks"])


@needs_sandbox
def test_sandbox_probe_reports_real_usability(client):
    from cavman.sandbox_probe import probe
    usable, detail = probe(max_age=0)
    assert usable, detail
    assert client.get("/api/system", headers=ALICE).json()["sandbox"]["available"] is True


def test_worker_refuses_to_start_without_isolation(settings, monkeypatch):
    import cavman.sandbox_probe as sandbox_probe
    from cavman import worker as worker_module
    monkeypatch.setattr(sandbox_probe, "probe", lambda **_: (False, "no namespaces"))
    with pytest.raises(SystemExit, match="refuses to start: no namespaces"):
        worker_module.main(settings)


@needs_sandbox
def test_dependent_code_tasks_build_on_integrated_work(client, settings):
    run_id = build(client, prompt="Booking API #dependent")["run_id"]
    drain(settings)
    detail = client.get(f"/api/runs/{run_id}", headers=ALICE).json()
    assert detail["state"] == "complete"
    api = next(a for a in detail["artifacts"] if a["task_id"] == "api")
    # The regression check ran the core's tests inside the api workspace: the
    # api candidate was built on the integrated core, not on an empty repo.
    regression = next(v for v in api["validations"] if v["check"] == "pytest_regression")
    assert regression["status"] == "passed" and "test_booking.py" in regression["command"]
    commits = [a["integrated_commit"] for a in detail["artifacts"]]
    assert all(commits) and len(set(commits)) == 2
    assert detail["delivery"]["commit"] == api["integrated_commit"]
    assert detail["delivery"]["files"] == ["api.py", "booking.py", "test_api.py", "test_booking.py"]
    assert sum(e["title"].startswith("Merged into the project") for e in detail["timeline"]) == 2


@needs_sandbox
def test_parallel_task_on_stale_base_is_rebuilt_with_its_work_carried_over(client, settings):
    run_id = build(client, prompt="Booking #parallel")["run_id"]
    drain(settings)
    detail = client.get(f"/api/runs/{run_id}", headers=ALICE).json()
    assert detail["state"] == "complete"
    [failure] = detail["failure_details"]
    assert failure["classification"] == "STALE_BASE" and failure["recovery"]["action"] == "RETRY"
    # Whichever task was accepted second was rebuilt on the integrated first.
    first, second = [a for a in detail["artifacts"] if a["task_id"] == failure["task_id"]]
    assert first["status"] == "rejected" and first["integrated_commit"] is None
    assert second["status"] == "accepted" and second["integrated_commit"]
    assert len(second["changed_files"]) == 2
    assert detail["delivery"]["files"] == ["booking.py", "notify.py", "test_booking.py", "test_notify.py"]


@needs_sandbox
def test_failed_integration_blocks_completion(client, settings, monkeypatch):
    from walter.sandbox import SandboxViolation, WorkspaceManager

    def refuse(self, *args, **kwargs):
        raise SandboxViolation("integration storage unavailable")
    monkeypatch.setattr(WorkspaceManager, "integrate", refuse)
    run_id = build(client, prompt="Booking core")["run_id"]
    drain(settings)
    detail = client.get(f"/api/runs/{run_id}", headers=ALICE).json()
    assert detail["status"] == "active"
    core = next(t for t in detail["tasks"] if t["id"] == "core")
    assert core["state"] == "Accepted"
    assert all(a["integrated_commit"] is None for a in detail["artifacts"])
    assert detail["delivery"] is None


@needs_sandbox
def test_delivery_refuses_an_integration_branch_moved_outside_cavman(client, settings):
    import subprocess
    from cavman.delivery import DeliveryError, assemble
    from cavman.engine import Engine
    from cavman.platform_store import PlatformStore

    run_id = build(client, prompt="Booking core")["run_id"]
    drain(settings)
    platform = settings.open_platform_store()
    record = platform.run_by_id(run_id)
    platform.close()
    engine = Engine(settings)
    repo = engine.project_repo(record.project_id)
    subprocess.run(["git", "-C", str(repo), "update-ref", "refs/heads/walter-integration", "HEAD"], check=True)
    with pytest.raises(DeliveryError, match="moved outside Cavman"):
        assemble(engine.load(run_id), repo, settings.deliveries_dir, "x", cost_usd=0, cost_complete=True)



@needs_sandbox
def test_workflow_mode_spends_one_planning_call_and_no_manager_calls(client, settings):
    run_id = build(client)["run_id"]
    drain(settings)
    detail = client.get(f"/api/runs/{run_id}", headers=ALICE).json()
    assert detail["state"] == "complete"
    assert detail["usage"]["by_role"]["planner"]["calls"] == 1
    assert "manager" not in detail["usage"]["by_role"]


@needs_sandbox
def test_rejected_capability_leaves_the_task_blocked(client, settings):
    run_id = build(client, prompt="Booking core #approval")["run_id"]
    drain(settings)
    approval = client.get(f"/api/runs/{run_id}", headers=ALICE).json()["approvals"][0]
    assert approval["action"] == "change_capability" and approval["title"] == "Grant a capability"
    client.post(f"/api/runs/{run_id}/approvals/{approval['id']}",
                json={"decision": "reject", "scope_digest": approval["scope_digest"]}, headers=ALICE)
    drain(settings)
    detail = client.get(f"/api/runs/{run_id}", headers=ALICE).json()
    assert detail["status"] == "active" and detail["state"] in {"blocked", "waiting"}
    assert detail["approvals"][0]["status"] == "rejected"
    assert detail["capability_requests"][0]["status"] == "denied"
    assert next(t for t in detail["tasks"] if t["id"] == "core")["state"] == "Blocked"


def test_workflow_mode_refuses_follow_up_instructions(client):
    run_id = build(client)["run_id"]
    client.post(f"/api/runs/{run_id}/stop", headers=ALICE)
    response = client.post(f"/api/runs/{run_id}/continue", json={"message": "add dark mode"}, headers=ALICE)
    assert response.status_code == 422


@needs_sandbox
def test_typescript_build_is_validated_by_node_tests(client, settings):
    from walter.sandbox import _detect_node_root
    if _detect_node_root() is None:
        pytest.skip("Node unavailable")
    run_id = build(client, prompt="Booking UI helper #node")["run_id"]
    drain(settings)
    detail = client.get(f"/api/runs/{run_id}", headers=ALICE).json()
    assert detail["state"] == "complete", detail["jobs"][-1]["message"]
    [artifact] = detail["artifacts"]
    [check] = artifact["validations"]
    assert check["check"] == "node_test" and check["label"] == "Node tests" and check["status"] == "passed"
    assert "# pass 1" in check["output"]
    assert detail["delivery"]["files"] == ["slots.test.ts", "slots.ts"]


@needs_sandbox
def test_account_monthly_call_cap_blocks_new_work_and_tightens_runs(settings):
    from dataclasses import replace
    capped = replace(settings, account_monthly_max_calls=5)
    with TestClient(create_app(capped)) as client:
        first = build(client)["run_id"]
        drain(capped)
        detail = client.get(f"/api/runs/{first}", headers=ALICE).json()
        # The run was cut off at the account's remaining allowance, not its own 300 calls.
        assert detail["state"] == "budget_reached" and detail["usage"]["calls"] == 5
        spending = client.get("/api/account", headers=ALICE).json()["spending"]
        assert spending["exhausted"] is True and spending["model_calls"] == 5
        refused = client.post("/api/builds", json={"prompt": "Another build"}, headers=ALICE)
        assert refused.status_code == 402 and "monthly spending limit" in refused.json()["detail"]
        assert client.post(f"/api/runs/{first}/continue", json={}, headers=ALICE).status_code == 402
        # Other accounts are unaffected.
        assert client.post("/api/builds", json={"prompt": "Mallory's build"}, headers=MALLORY).status_code == 201


def test_metrics_are_disabled_by_default_and_token_protected(settings):
    from dataclasses import replace
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/metrics").status_code == 404
    metered = replace(settings, metrics_token="m" * 32)
    with TestClient(create_app(metered)) as client:
        build(client)
        assert client.get("/api/metrics").status_code == 401
        assert client.get("/api/metrics", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 401
        body = client.get("/api/metrics", headers={"Authorization": "Bearer " + "m" * 32}).text
    assert 'cavman_jobs{status="queued",outcome=""} 1' in body
    assert "cavman_runs 1" in body and "cavman_sandbox_available" in body
    assert "cavman_spend_month_usd 0.000000" in body and "cavman_model_calls_month 0" in body
    # Exported before any delivery has failed, so the first failure is a visible rise.
    assert 'cavman_deliveries{status="failed"} 0' in body


def test_server_usage_counts_this_months_calls_by_when_they_were_made(tmp_path):
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from cavman.accounts import server_usage

    now = datetime(2026, 9, 15, tzinfo=timezone.utc)
    store = PlatformStore(str(tmp_path / "platform.db"))
    store.create_project("alice", "P", project_id="p1")
    usage = {}
    for run_id, created, finished, calls in (
            # Created four months ago, still working this month.
            ("old-active", "2026-05-02T00:00:00+00:00", "2026-09-10T00:00:00+00:00",
             [("2026-05-02T01:00:00+00:00", 5.0), ("2026-09-09T00:00:00+00:00", 2.0)]),
            # Created and finished before this month: not counted, and not read.
            ("old-done", "2026-08-01T00:00:00+00:00", "2026-08-20T00:00:00+00:00",
             [("2026-08-02T00:00:00+00:00", 7.0)]),
            # Created this month and still running.
            ("new", "2026-09-12T00:00:00+00:00", None, [("2026-09-12T01:00:00+00:00", 1.5)])):
        store.create_run(run_id, "p1", "alice", "x", executor="scripted", budget_usd=10, max_model_calls=10)
        store.enqueue(run_id, "start")
        job = store.claim("w", 60)
        if finished:
            store.finish(job.id, "w", "succeeded", "done")
        store.connection.execute("UPDATE runs SET created_at=? WHERE id=?", (created, run_id))
        store.connection.execute("UPDATE jobs SET started_at=?, finished_at=? WHERE run_id=?",
                                 (created, finished, run_id))
        store.connection.commit()
        usage[run_id] = [SimpleNamespace(created_at=at, raw_usage={"cost": cost}) for at, cost in calls]
    loaded = []

    def load(run_id):
        loaded.append(run_id)
        return SimpleNamespace(usage_records=usage[run_id])

    result = server_usage(SimpleNamespace(load=load), store, now=now)
    store.close()
    assert result == {"spent_usd": 3.5, "model_calls": 2, "calls_without_cost": 0}
    assert sorted(loaded) == ["new", "old-active"]


@needs_sandbox
def test_metrics_count_this_months_model_calls(settings):
    from dataclasses import replace
    metered = replace(settings, metrics_token="m" * 32)
    with TestClient(create_app(metered)) as client:
        build(client)
        drain(metered)
        body = client.get("/api/metrics", headers={"Authorization": "Bearer " + "m" * 32}).text
    calls = next(int(line.split()[1]) for line in body.splitlines() if line.startswith("cavman_model_calls_month "))
    # The scripted executor reports no cost, so every call is counted as uncosted.
    assert calls > 0 and f"cavman_model_calls_without_cost_month {calls}" in body


def test_operator_commands_list_requeue_and_abandon(settings, capsys):
    import argparse
    from cavman.ops import run_ops
    with TestClient(create_app(settings)) as client:
        run_id = build(client)["run_id"]
        client.post(f"/api/runs/{run_id}/stop", headers=ALICE)
    assert run_ops(settings, argparse.Namespace(ops_command="list", attention=True)) == 0
    [row] = json.loads(capsys.readouterr().out)
    assert row["run_id"] == run_id and row["state"] == "paused" and row["owner"] == "alice"
    assert run_ops(settings, argparse.Namespace(ops_command="requeue", run_id=run_id)) == 0
    capsys.readouterr()
    assert run_ops(settings, argparse.Namespace(ops_command="abandon", run_id=run_id, reason="cleanup")) == 1
    platform = settings.open_platform_store()
    platform.request_cancel(run_id)
    platform.close()
    assert run_ops(settings, argparse.Namespace(ops_command="abandon", run_id=run_id, reason="cleanup")) == 0
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["status"] == "abandoned"


def test_json_log_format_is_one_object_per_line():
    import logging
    from cavman.logs import JsonFormatter
    record = logging.LogRecord("cavman.worker", logging.INFO, __file__, 1, "Job %s done", ("j1",), None)
    record.run_id = "r1"
    payload = json.loads(JsonFormatter().format(record))
    assert payload["message"] == "Job j1 done" and payload["run_id"] == "r1" and payload["level"] == "INFO"


@needs_sandbox
def test_publish_pushes_exactly_the_verified_commit_to_a_new_repository(client, settings, tmp_path, monkeypatch):
    import subprocess
    from cavman import publish as publish_module

    token = "gho_" + "t" * 36
    created = []

    def create_repository(self, name, private, description):
        assert self._token == token
        remote = tmp_path / f"{name}.git"
        subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
        created.append((name, private))
        return {"full_name": f"alice/{name}", "html_url": f"https://github.com/alice/{name}",
                "clone_url": str(remote)}
    monkeypatch.setattr(publish_module.GitHubClient, "create_repository", create_repository)

    run_id = build(client, prompt="Booking API #dependent")["run_id"]
    body = {"name": "booking-api", "private": True, "confirm": True, "github_token": token}
    early = client.post(f"/api/runs/{run_id}/publish", json=body, headers=ALICE)
    assert early.status_code == 409 and created == []
    drain(settings)
    detail = client.get(f"/api/runs/{run_id}", headers=ALICE).json()
    assert client.post(f"/api/runs/{run_id}/publish", json={**body, "confirm": False},
                       headers=ALICE).status_code == 422
    assert client.post(f"/api/runs/{run_id}/publish", json={**body, "name": "../x"},
                       headers=ALICE).status_code == 422
    assert client.post(f"/api/runs/{run_id}/publish", json=body, headers=MALLORY).status_code == 404
    published = client.post(f"/api/runs/{run_id}/publish", json=body, headers=ALICE)
    assert published.status_code == 201, published.text
    publication = published.json()["publication"]
    assert publication["repository"] == "alice/booking-api" and publication["private"] is True
    assert publication["commit"] == detail["delivery"]["commit"]
    remote = tmp_path / "booking-api.git"
    head = subprocess.run(["git", "-C", str(remote), "rev-parse", "main"], capture_output=True, text=True).stdout.strip()
    assert head == detail["delivery"]["commit"]
    files = subprocess.run(["git", "-C", str(remote), "ls-tree", "-r", "--name-only", "main"],
                           capture_output=True, text=True).stdout.split()
    assert {"api.py", "booking.py", "test_api.py", "test_booking.py"} <= set(files)
    again = client.post(f"/api/runs/{run_id}/publish", json=body, headers=ALICE)
    assert again.status_code == 409 and len(created) == 1
    final = client.get(f"/api/runs/{run_id}", headers=ALICE).json()
    assert final["publication"]["url"] == "https://github.com/alice/booking-api"
    assert token not in json.dumps(final)


def test_push_failure_messages_never_contain_the_token(tmp_path):
    from cavman.publish import PublishError, push
    token = "gho_" + "s" * 36
    repo = tmp_path / "r"
    repo.mkdir()
    import subprocess
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    with pytest.raises(PublishError) as raised:
        push(repo, "0" * 40, f"https://x-access-token:{token}@invalid.invalid/r.git", token)
    assert token not in str(raised.value)


def test_model_modes_are_offered_only_when_configured(tmp_path, monkeypatch):
    from cavman.config import Settings as S
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-not-real")
    settings = S.from_env({"CAVMAN_API_TOKEN": TOKEN, "CAVMAN_DATA_DIR": str(tmp_path),
                           "CAVMAN_MODELS_BUDGET": "manager=deepseek/deepseek-chat,worker=qwen/qwen3-coder"})
    with pytest.raises(SettingsError, match="CAVMAN_MODELS_QUALITY"):
        S.from_env({"CAVMAN_API_TOKEN": TOKEN, "CAVMAN_MODELS_QUALITY": "worker=x"})
    with TestClient(create_app(settings)) as client:
        modes = {m["mode"]: m for m in client.get("/api/system", headers=ALICE).json()["model_modes"]}
        assert modes["budget"]["available"] and modes["budget"]["worker_model"] == "qwen/qwen3-coder"
        assert modes["automatic"]["available"] and not modes["quality"]["available"]
        refused = client.post("/api/builds", json={"prompt": "Build x", "settings": {"model_mode": "quality"}},
                              headers=ALICE)
        assert refused.status_code == 422
        made = client.post("/api/builds", json={"prompt": "Build x", "settings": {"model_mode": "budget"}},
                           headers=ALICE)
        assert made.status_code == 201 and made.json()["run"]["model_mode"] == "budget"


def test_worker_routes_models_by_the_runs_mode(settings, monkeypatch):
    from dataclasses import replace as dc_replace
    from walter import runtime
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-not-real")
    routed = dc_replace(settings, executor="provider", model_profiles=(("budget", "m-budget", "w-budget"),))
    with TestClient(create_app(routed)) as client:
        run_id = client.post("/api/builds", json={"prompt": "Build x", "settings": {"model_mode": "budget"}},
                             headers=ALICE).json()["run_id"]
    worker = Worker(routed)
    try:
        platform = worker.platform
        job = platform.claim("w", 30)
        config, _ = worker._config(job, platform.run_by_id(run_id))
    finally:
        worker.close()
    assert (config.manager_model, config.worker_model) == ("m-budget", "w-budget")
    assert isinstance(config, runtime.RuntimeConfig) and config.budget.max_calls == 300


@needs_sandbox
def test_one_worker_process_runs_several_jobs_concurrently(settings):
    from dataclasses import replace
    parallel = replace(settings, worker_concurrency=2, scripted_step_delay=0.05)
    with TestClient(create_app(parallel)) as client:
        runs = [build(client, prompt=p)["run_id"] for p in ("Booking one", "Booking two #node")]

        async def scenario():
            worker = Worker(parallel)
            try:
                loop = asyncio.create_task(worker.run_forever(poll_seconds=0.05))
                for _ in range(400):
                    await asyncio.sleep(0.05)
                    states = [client.get(f"/api/runs/{r}", headers=ALICE).json()["state"] for r in runs]
                    if all(state == "complete" for state in states):
                        break
                worker.stop()
                await loop
            finally:
                worker.close()
            return states
        assert asyncio.run(scenario()) == ["complete", "complete"]
        started = [client.get(f"/api/runs/{r}", headers=ALICE).json()["jobs"][0]["started_at"] for r in runs]
        finished = [client.get(f"/api/runs/{r}", headers=ALICE).json()["jobs"][0]["finished_at"] for r in runs]
    assert max(started) < min(finished)  # the two jobs overlapped


@needs_sandbox
def test_account_export_and_erasure_cover_everything_the_owner_has(client, settings):
    run_id = build(client)["run_id"]
    other = build(client, headers=MALLORY)["run_id"]
    drain(settings)
    project_id = client.get(f"/api/runs/{run_id}", headers=ALICE).json()["project_id"]

    export = client.get("/api/account/export", headers=ALICE).json()
    assert export["format"] == "cavman-export/1" and export["user_id"] == "alice"
    assert [r["id"] for r in export["runs"]] == [run_id] and [p["id"] for p in export["projects"]] == [project_id]
    assert any(a.get("diff") for a in export["runs"][0]["artifacts"])
    assert "Build complete" in [e["title"] for e in export["runs"][0]["timeline"]]
    assert str(settings.data_dir) not in json.dumps(export)

    deleted = client.delete("/api/account", headers=ALICE)
    assert deleted.status_code == 200 and deleted.json()["deleted"] == {"runs": 1, "projects": 1}
    assert client.get(f"/api/runs/{run_id}", headers=ALICE).status_code == 404
    assert client.get("/api/projects", headers=ALICE).json()["projects"] == []
    assert not (settings.projects_dir / project_id).exists()
    assert not (settings.deliveries_dir / f"{run_id}.tar.gz").exists()
    from walter.store import SQLiteStore
    store = settings.open_operations_store()
    try:
        assert run_id not in store.run_ids() and other in store.run_ids()
    finally:
        store.close()
    import sqlite3
    with sqlite3.connect(settings.sessions_db) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "agent_sessions" in tables:
            assert db.execute("SELECT COUNT(*) FROM agent_sessions WHERE session_id=?", (run_id,)).fetchone()[0] == 0
    # Another account is untouched.
    assert client.get(f"/api/runs/{other}", headers=MALLORY).json()["state"] == "complete"
    assert client.get("/api/account/export", headers=ALICE).json()["runs"] == []


def test_account_erasure_waits_for_running_work(client, settings):
    run_id = build(client)["run_id"]
    platform = settings.open_platform_store()
    try:
        assert platform.claim("worker-1", 30) is not None
        refused = client.delete("/api/account", headers=ALICE)
        assert refused.status_code == 409 and "running" in refused.json()["detail"]
        assert client.get(f"/api/runs/{run_id}", headers=ALICE).status_code == 200
    finally:
        platform.close()


def test_orphans_left_by_an_interrupted_erasure_are_purged(client, settings):
    from cavman.engine import Engine
    from cavman.erasure import purge_orphans

    kept = build(client)["run_id"]
    engine = Engine(settings)
    orphan = engine.create_run("Left behind", [])
    (settings.projects_dir / ("f" * 32) / "repo").mkdir(parents=True)
    (settings.deliveries_dir / f"{orphan.id}.tar.gz").write_bytes(b"x")
    platform = settings.open_platform_store()
    try:
        assert purge_orphans(settings, engine, platform) == {"runs": 0, "projects": 0, "archives": 0}  # too new
        assert purge_orphans(settings, engine, platform, min_age_seconds=-60) == {"runs": 1, "projects": 1, "archives": 1}
        with engine.core() as core:
            assert core.store.run_ids() == [kept]
    finally:
        platform.close()


@needs_sandbox
def test_follow_up_run_builds_on_the_projects_integrated_work(client, settings):
    first = build(client)
    drain(settings)
    second = build(client, prompt="Add cancellation to the booking app #follow-up", project_id=first["project_id"])
    drain(settings)
    detail = client.get(f"/api/runs/{second['run_id']}", headers=ALICE).json()
    assert detail["state"] == "complete" and detail["delivery"]["status"] == "ready"
    first_commit = client.get(f"/api/runs/{first['run_id']}", headers=ALICE).json()["delivery"]["commit"]
    import subprocess
    repo = settings.projects_dir / first["project_id"] / "repo"
    assert subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", first_commit,
                           detail["delivery"]["commit"]]).returncode == 0
    cancel = next(a for a in detail["artifacts"] if a["task_id"] == "cancel")
    assert cancel["changed_files"] == ["cancel.py", "test_cancel.py"]
    assert {"booking.py", "cancel.py"} <= set(detail["delivery"]["files"])


def limited_client(settings, **limits):
    from dataclasses import replace
    return TestClient(create_app(replace(settings, **limits)))


def test_build_rate_limit_is_per_user_and_says_when_to_retry(settings):
    with limited_client(settings, builds_per_hour=2, max_concurrent_builds=10) as client:
        build(client)
        build(client)
        refused = client.post("/api/builds", json={"prompt": "Build a third thing"}, headers=ALICE)
        assert refused.status_code == 429 and "2 new builds per hour" in refused.json()["detail"]
        assert int(refused.headers["Retry-After"]) > 3000
        build(client, headers=MALLORY)  # other accounts are unaffected


def test_concurrent_builds_projects_and_disk_are_capped(settings):
    with limited_client(settings, max_concurrent_builds=1, max_projects=2, account_disk_mb=1) as client:
        first = build(client)
        busy = client.post("/api/builds", json={"prompt": "Build another thing"}, headers=ALICE)
        assert busy.status_code == 429 and "1 builds running" in busy.json()["detail"]
        client.post(f"/api/runs/{first['run_id']}/stop", headers=ALICE)
        assert client.post("/api/projects", json={"name": "Second"}, headers=ALICE).status_code == 201
        full = client.post("/api/projects", json={"name": "Third"}, headers=ALICE)
        assert full.status_code == 403 and "2 projects" in full.json()["detail"]
        (settings.projects_dir / first["project_id"] / "repo" / "big.bin").write_bytes(b"x" * (2 * 1024 * 1024))
        disk = client.post("/api/builds", json={"prompt": "Build more", "project_id": first["project_id"]},
                           headers=ALICE)
        assert disk.status_code == 403 and "1 MB of storage" in disk.json()["detail"]


def test_mutating_actions_are_rate_limited(settings):
    with limited_client(settings, actions_per_minute=2) as client:
        run_id = build(client)["run_id"]
        assert client.post(f"/api/runs/{run_id}/stop", headers=ALICE).status_code == 202
        assert client.post(f"/api/runs/{run_id}/stop", headers=ALICE).status_code == 409
        limited = client.post(f"/api/runs/{run_id}/stop", headers=ALICE)
        assert limited.status_code == 429 and "Retry-After" in limited.headers


def test_rate_windows_slide_and_erasure_clears_them(client, settings):
    platform = settings.open_platform_store()
    try:
        assert platform.consume_rate("alice", "build", 2, 60, now=1000) is None
        assert platform.consume_rate("alice", "build", 2, 60, now=1010) is None
        assert platform.consume_rate("alice", "build", 2, 60, now=1020) == 40
        assert platform.consume_rate("alice", "build", 2, 60, now=1061) is None  # the first expired
        platform.delete_owner("alice")
        assert platform.consume_rate("alice", "build", 1, 60, now=1062) is None
    finally:
        platform.close()


def test_jobs_of_one_project_never_run_at_the_same_time(client, settings):
    first = build(client)
    second = build(client, prompt="Add cancellation #follow-up", project_id=first["project_id"])
    other = build(client, headers=MALLORY)
    platform = settings.open_platform_store()
    try:
        claimed = platform.claim("w1", 30)
        assert claimed.run_id == first["run_id"]
        # The same project's next job waits; another project's job does not.
        assert platform.claim("w2", 30).run_id == other["run_id"]
        assert platform.claim("w3", 30) is None
        assert not platform.acquire_project(first["project_id"], "maintenance", 60)
        platform.finish(claimed.id, "w1", "succeeded", "done")
        assert platform.acquire_project(first["project_id"], "maintenance", 60)
        assert platform.claim("w3", 30) is None  # held by maintenance
        assert not platform.acquire_project(first["project_id"], "someone-else", 60)
        platform.release_project(first["project_id"], "maintenance")
        assert platform.claim("w3", 30).run_id == second["run_id"]
        assert platform.due("cleanup", 3600) and not platform.due("cleanup", 3600)
    finally:
        platform.close()


@needs_sandbox
def test_maintenance_retires_finished_work_without_losing_deliveries(client, settings):
    import os
    import subprocess

    from cavman.engine import Engine
    from cavman.maintenance import run_maintenance

    first = build(client)
    drain(settings)
    repo = settings.projects_dir / first["project_id"] / "repo"
    candidates = lambda: [b for b in subprocess.run(  # noqa: E731
        ["git", "-C", str(repo), "branch", "--list", "walter-candidate/*"], capture_output=True, text=True
    ).stdout.split() if b.startswith("walter-candidate/")]
    assert candidates()
    deps = repo / ".local" / "sandboxes" / "node-deps"
    for name, age in (("old", 30 * 86400), ("fresh", 0)):
        (deps / name).mkdir(parents=True)
        (deps / name / ".complete").write_text("")
        os.utime(deps / name / ".complete", (time.time() - age, time.time() - age))

    platform = settings.open_platform_store()
    try:
        summary = run_maintenance(settings, Engine(settings), platform)
        assert summary["runs_retired"] == 1 and summary["node_deps_pruned"] == 1
        assert run_maintenance(settings, Engine(settings), platform)["runs_retired"] == 0  # idempotent
    finally:
        platform.close()
    assert candidates() == [] and not (deps / "old").exists() and (deps / "fresh").exists()
    assert client.get(f"/api/runs/{first['run_id']}/delivery/download", headers=ALICE).status_code == 200
    # The integration branch is untouched: a follow-up still builds on the delivered code.
    second = build(client, prompt="Add cancellation #follow-up", project_id=first["project_id"])
    drain(settings)
    assert client.get(f"/api/runs/{second['run_id']}", headers=ALICE).json()["state"] == "complete"


def test_cost_history_reports_only_real_completed_spend_per_mode():
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace

    from cavman.accounts import cost_history

    now = datetime(2026, 9, 28, tzinfo=timezone.utc)
    runs, records = {}, []

    def add(run_id, mode, costs, status="completed", age_days=1):
        records.append(SimpleNamespace(id=run_id, model_mode=mode,
                                       created_at=(now - timedelta(days=age_days)).isoformat()))
        runs[run_id] = SimpleNamespace(status=status, usage_records=[
            SimpleNamespace(raw_usage={"cost": c} if c is not None else {}) for c in costs])

    for i, cost in enumerate([0.04, 0.06, 0.09, 0.09, 0.10, 0.15, 2.00]):
        add(f"a{i}", "automatic", [cost / 2, cost / 2])
    add("unknown", "automatic", [0.05, None])           # a call reported no cost
    add("blocked", "automatic", [9.0], status="active")  # not completed
    add("old", "automatic", [9.0], age_days=45)          # outside the window
    for i in range(3):
        add(f"b{i}", "budget", [0.01])
    engine = SimpleNamespace(load=lambda run_id: runs[run_id])
    platform = SimpleNamespace(runs_created_since=lambda since: [r for r in records if r.created_at >= since])

    history = cost_history(engine, platform, now=now)
    automatic = history["modes"]["automatic"]
    assert automatic["builds"] == 7 and automatic["median_usd"] == 0.09
    assert automatic["low_usd"] == 0.06 and automatic["high_usd"] == 0.15  # the $2 outlier sets no bound
    assert history["modes"]["budget"] == {"builds": 3, "median_usd": None, "low_usd": None, "high_usd": None}

    add("a7", "automatic", [0.12])  # an even-sized sample: the median averages the middle two
    assert cost_history(engine, platform, now=now)["modes"]["automatic"]["median_usd"] == 0.095


def test_runs_created_since_returns_only_recent_runs(tmp_path):
    from cavman.platform_store import PlatformStore

    store = PlatformStore(tmp_path / "platform.db")
    try:
        store.create_project("alice", "Demo", project_id="p" * 32)
        for run_id in ("old", "new"):
            store.create_run(run_id, "p" * 32, "alice", "Build", executor="scripted",
                             budget_usd=5.0, max_model_calls=10)
        store.connection.execute("UPDATE runs SET created_at='2020-01-01T00:00:00+00:00' WHERE id='old'")
        assert [r.id for r in store.runs_created_since("2026-01-01T00:00:00+00:00")] == ["new"]
    finally:
        store.close()


def test_estimate_endpoint_returns_ceiling_and_allowance(client):
    body = client.get("/api/estimate", headers=ALICE).json()
    assert body["modes"] == {} and body["default_budget_usd"] == 5.0
    assert body["account"]["remaining_usd"] == body["account"]["limit_usd"]
    assert body["account"]["cost_complete"] is True and body["account"]["exhausted"] is False
    assert body["account"]["remaining_calls"] == body["account"]["max_model_calls"] > 0
    assert body["default_max_model_calls"] > 0
    assert client.get("/api/estimate").status_code == 401


def stopped(client, **extra):
    """Start a build and stop it at once, so the concurrent-build cap never applies."""
    made = build(client, **extra)
    assert client.post(f"/api/runs/{made['run_id']}/stop", headers=ALICE).status_code == 202
    return made


def test_run_and_project_lists_are_paged_newest_first(client):
    made = [stopped(client, prompt=f"Build thing number {n}") for n in range(5)]
    newest_first = [m["run_id"] for m in reversed(made)]
    first = client.get("/api/runs?limit=2", headers=ALICE).json()
    assert [r["id"] for r in first["runs"]] == newest_first[:2] and first["next"]
    stopped(client, prompt="Build one more thing")  # arrives while paging: shifts nothing
    second = client.get(f"/api/runs?limit=2&before={first['next']}", headers=ALICE).json()
    third = client.get(f"/api/runs?limit=2&before={second['next']}", headers=ALICE).json()
    assert [r["id"] for r in second["runs"] + third["runs"]] == newest_first[2:]
    assert third["next"] is None
    assert client.get("/api/runs?limit=0", headers=ALICE).status_code == 422
    assert client.get("/api/runs?limit=101", headers=ALICE).status_code == 422
    assert client.get("/api/runs?before=x' OR 1=1", headers=ALICE).status_code == 422
    assert client.get(f"/api/runs?before={first['next']}", headers=MALLORY).json()["runs"] == []

    projects = client.get("/api/projects?limit=4", headers=ALICE).json()
    assert len(projects["projects"]) == 4 and projects["next"]
    rest = client.get(f"/api/projects?limit=4&before={projects['next']}", headers=ALICE).json()
    assert len(rest["projects"]) == 2 and rest["next"] is None  # five builds and the one more
    assert all(p["run_count"] == 1 and p["latest_run"] for p in projects["projects"] + rest["projects"])


def test_a_project_pages_its_runs_but_counts_them_all(client):
    first = stopped(client, prompt="Build a ledger")
    for _ in range(2):
        stopped(client, prompt="Add a report", project_id=first["project_id"])
    page = client.get(f"/api/projects/{first['project_id']}?limit=2", headers=ALICE).json()
    assert page["run_count"] == 3 and len(page["runs"]) == 2 and page["next"]
    older = client.get(f"/api/projects/{first['project_id']}?limit=2&before={page['next']}", headers=ALICE).json()
    assert [r["id"] for r in older["runs"]] == [first["run_id"]] and older["next"] is None
    assert older["latest_run"]["id"] == page["runs"][0]["id"]
