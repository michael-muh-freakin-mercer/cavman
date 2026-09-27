import json
import subprocess
from pathlib import Path

import pytest

from caveman import cli
from caveman.orchestration import Orchestrator


def setup_repository(tmp_path):
    (tmp_path / "README.md").write_text("# Fixture\n")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run([
        "git", "-C", str(tmp_path), "-c", "user.name=Fixture", "-c",
        "user.email=fixture@example.invalid", "commit", "-qm", "fixture",
    ], check=True)


def test_offline_run_list_inspect_events_resume_and_approval(tmp_path, monkeypatch, capsys):
    setup_repository(tmp_path)
    monkeypatch.chdir(tmp_path)
    store = cli._store()
    core = Orchestrator(store)
    run = core.create_run("offline fixture", ["fixture accepted"])
    request = core.request_approval(run.id, "fixture", {"candidate": "one"}, "human boundary")
    store.close()

    cli._operations(["list"])
    listed = json.loads(capsys.readouterr().out)
    assert listed[0]["id"] == run.id

    cli._operations(["inspect", run.id])
    assert json.loads(capsys.readouterr().out)["objective"] == "offline fixture"

    cli._operations(["events", run.id])
    assert [event["kind"] for event in json.loads(capsys.readouterr().out)] == [
        "run.created", "approval.required",
    ]

    cli._operations(["resume", run.id])
    assert "Durable status: active" in capsys.readouterr().out

    monkeypatch.setattr(cli.getpass, "getuser", lambda: "fixture-user")
    monkeypatch.setattr(cli.os, "getuid", lambda: 1234)
    cli._operations(["approve", run.id, request.id, "--reason", "scope checked"])
    approved = json.loads(capsys.readouterr().out)
    assert approved["approval_decisions"][request.id]["approved"] is True
    assert approved["approval_decisions"][request.id]["human_id"] == "local-os:fixture-user:uid:1234"


def test_offline_abandon_closes_a_work_free_run_and_refuses_live_work(tmp_path, monkeypatch, capsys):
    """`caveman run abandon` is the offline, no-spend exit for stuck active runs."""
    from caveman.contracts import TaskPacket
    from caveman.models import TaskNode

    setup_repository(tmp_path)
    monkeypatch.chdir(tmp_path)
    store = cli._store()
    core = Orchestrator(store)
    run = core.create_run("offline abandon fixture", ["fixture accepted"])
    live = core.create_run("still running", ["fixture accepted"])
    core.add_tasks(live.id, [TaskNode(packet=TaskPacket(
        task_id="a", role="writer", objective="Write", deliverable="report",
        acceptance_criteria=["accurate"], stop_condition="deliver"))])
    core.delegate(live.id, "a", "author")
    store.close()

    monkeypatch.setattr(cli.getpass, "getuser", lambda: "fixture-user")
    monkeypatch.setattr(cli.os, "getuid", lambda: 1234)
    with pytest.raises(ValueError, match="in-flight work: a"):
        cli._operations(["abandon", live.id, "--reason", "close it anyway"])
    assert capsys.readouterr().out == ""

    cli._operations(["abandon", run.id, "--reason", "Superseded by a fresh run"])
    closed = json.loads(capsys.readouterr().out)
    assert closed["run_status"] == "abandoned"
    assert closed["durable_state"] == "preserved"

    cli._operations(["list"])
    statuses = {item["id"]: item["status"] for item in json.loads(capsys.readouterr().out)}
    assert statuses[run.id] == "abandoned" and statuses[live.id] == "active"


def test_one_shot_builds_manager_around_new_durable_run(monkeypatch, capsys):
    class Run:
        id = "durable-run"
        status = "active"
        final_result = None

    class Controller:
        def inspect(self):
            return Run()

        def close(self):
            observed["closed"] = True

    controller = Controller()
    observed = {}

    def make_controller(goal):
        observed["goal"] = goal
        return controller

    monkeypatch.setattr(cli, "_controller", make_controller)
    monkeypatch.setattr(cli.RuntimeConfig, "from_env", classmethod(lambda cls: object()))
    monkeypatch.setattr(cli, "build_caveman", lambda value: observed.setdefault("controller", value) or object())

    async def execute(caveman, goal, **kwargs):
        observed["caveman"] = caveman
        return object(), "trace"

    monkeypatch.setattr(cli, "_execute", execute)
    import asyncio
    asyncio.run(cli._run_once("bounded goal", None, 3))
    assert observed["goal"] == "bounded goal"
    assert observed["controller"] is controller
    assert observed["closed"] is True
    output = capsys.readouterr().out
    assert "Run ID: durable-run" in output
    assert "Local trace ID (provider export disabled): trace" in output


def _stub_run_once(monkeypatch, observed, *, result):
    class Run:
        id = "durable-run"
        status = "active"
        final_result = None

    class Controller:
        def inspect(self):
            return Run()

        def close(self):
            observed["closed"] = True

    monkeypatch.setattr(cli, "_controller", lambda goal: Controller())
    monkeypatch.setattr(cli.RuntimeConfig, "from_env", classmethod(lambda cls: object()))
    monkeypatch.setattr(cli, "build_caveman", lambda value: object())

    async def execute(caveman, goal, **kwargs):
        return result, "trace"

    monkeypatch.setattr(cli, "_execute", execute)


def test_one_shot_closes_synchronous_session_without_awaiting(monkeypatch):
    observed = {}

    class Session:
        def __init__(self, session_id, db):
            observed["session_id"] = session_id

        def close(self):
            observed["session_closed"] = True

    monkeypatch.setattr(cli, "SQLiteSession", Session)
    _stub_run_once(monkeypatch, observed, result=object())

    import asyncio
    asyncio.run(cli._run_once("bounded goal", "fixture-session", 3))

    assert observed["session_id"] == "fixture-session"
    assert observed["session_closed"] is True


def test_one_shot_prints_manager_final_output(monkeypatch, capsys):
    observed = {}

    class Result:
        final_output = "Run accepted; two tasks remain blocked."

    _stub_run_once(monkeypatch, observed, result=Result())

    import asyncio
    asyncio.run(cli._run_once("bounded goal", None, 3))

    output = capsys.readouterr().out
    assert "Manager: Run accepted; two tasks remain blocked." in output


def test_one_shot_skips_non_string_manager_output(monkeypatch, capsys):
    observed = {}

    class Result:
        final_output = None

    _stub_run_once(monkeypatch, observed, result=Result())

    import asyncio
    asyncio.run(cli._run_once("bounded goal", None, 3))

    assert "Manager:" not in capsys.readouterr().out


def test_legacy_trace_sensitive_flag_is_an_honest_noop(monkeypatch):
    monkeypatch.setenv("OPENAI_AGENTS_TRACE_INCLUDE_SENSITIVE_DATA", "1")
    args = cli._parser().parse_args(["--trace-sensitive", "goal"])
    assert args.trace_sensitive is True
    cli._configure_trace_privacy(args.trace_sensitive)
    assert cli.os.environ["OPENAI_AGENTS_TRACE_INCLUDE_SENSITIVE_DATA"] == "0"


def test_missing_provider_config_does_not_create_orphan_run(monkeypatch):
    observed = {"created": False}
    monkeypatch.setattr(
        cli.RuntimeConfig, "from_env",
        classmethod(lambda cls: (_ for _ in ()).throw(cli.RuntimeConfigurationError("missing key"))),
    )
    monkeypatch.setattr(cli, "_controller", lambda goal: observed.update(created=True))
    import asyncio
    with pytest.raises(cli.RuntimeConfigurationError, match="missing key"):
        asyncio.run(cli._run_once("goal", None, 2))
    assert observed["created"] is False


def test_new_cli_run_requires_manager_defined_criteria(tmp_path, monkeypatch):
    setup_repository(tmp_path)
    monkeypatch.chdir(tmp_path)
    controller = cli._controller("broad objective")
    try:
        assert controller.inspect().plan.completion_criteria == [cli.INITIAL_COMPLETION_CRITERION]
        assert not controller._criteria_defined()
    finally:
        controller.close()


def test_main_reports_missing_run_as_friendly_domain_error(tmp_path, monkeypatch):
    setup_repository(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.sys, "argv", ["caveman", "run", "inspect", "missing-run"])
    with pytest.raises(SystemExit, match="Caveman operation error"):
        cli.main()


def _run_with_approval(tmp_path, monkeypatch):
    setup_repository(tmp_path)
    monkeypatch.chdir(tmp_path)
    store = cli._store()
    core = Orchestrator(store)
    run = core.create_run("approval fixture", ["fixture accepted"])
    request = core.request_approval(run.id, "fixture", {"candidate": "one"}, "human boundary")
    store.close()
    return run, request


def test_approve_unknown_approval_id_is_an_operation_error(tmp_path, monkeypatch):
    run, _ = _run_with_approval(tmp_path, monkeypatch)
    monkeypatch.setattr(cli.sys, "argv",
                        ["caveman", "run", "approve", run.id, "missing-approval", "--reason", "scope checked"])
    with pytest.raises(SystemExit, match="Caveman operation error"):
        cli.main()


def test_approve_already_decided_approval_is_an_operation_error(tmp_path, monkeypatch):
    run, request = _run_with_approval(tmp_path, monkeypatch)
    store = cli._store()
    Orchestrator(store).decide_approval(run.id, request.id, True, "human", "Already decided")
    store.close()
    monkeypatch.setattr(cli.sys, "argv",
                        ["caveman", "run", "approve", run.id, request.id, "--reason", "second decision"])
    with pytest.raises(SystemExit, match="Caveman operation error"):
        cli.main()


def test_approve_deny_records_rejected_decision_with_local_identity(tmp_path, monkeypatch, capsys):
    run, request = _run_with_approval(tmp_path, monkeypatch)
    monkeypatch.setattr(cli.getpass, "getuser", lambda: "fixture-user")
    monkeypatch.setattr(cli.os, "getuid", lambda: 1234)
    cli._operations(["approve", run.id, request.id, "--deny", "--reason", "scope not acceptable"])
    denied = json.loads(capsys.readouterr().out)
    decision = denied["approval_decisions"][request.id]
    assert decision["approved"] is False
    assert decision["status"] == "rejected"
    assert decision["human_id"] == "local-os:fixture-user:uid:1234"
    assert denied["approvals"][request.id]["status"] == "rejected"


def _stub_usage_budget_path(monkeypatch, *, execute):
    class Run:
        id = "durable-run"
        status = "active"
        final_result = None

    class Controller:
        def inspect(self):
            return Run()

        def close(self):
            pass

    monkeypatch.setattr(cli, "_controller", lambda *args, **kwargs: Controller())
    monkeypatch.setattr(cli.RuntimeConfig, "from_env", classmethod(lambda cls: object()))
    monkeypatch.setattr(cli, "build_caveman", lambda value: object())
    monkeypatch.setattr(cli, "_execute", execute)


def test_one_shot_reports_usage_budget_exceeded_and_exits_nonzero(monkeypatch, capsys):
    async def execute(caveman, goal, **kwargs):
        raise cli.UsageBudgetExceeded("Model-call budget exhausted")

    _stub_usage_budget_path(monkeypatch, execute=execute)

    import asyncio
    with pytest.raises(SystemExit) as excinfo:
        asyncio.run(cli._run_once("bounded goal", None, 3))

    assert excinfo.value.code != 0
    captured = capsys.readouterr()
    assert "Usage budget exceeded: Model-call budget exhausted" in captured.out
    assert "Run ID: durable-run" not in captured.out
    assert "Local trace ID" not in captured.out
    assert "Traceback" not in captured.err


def test_interactive_reports_usage_budget_exceeded_and_continues(monkeypatch, capsys):
    observed = {"closed": 0}

    class Session:
        def __init__(self, session_id, db):
            pass

        async def clear_session(self):
            pass

        def close(self):
            pass

    class Controller:
        run_id = "durable-run"

        def inspect(self):
            raise AssertionError("inspect should not run after budget failure")

        def close(self):
            observed["closed"] += 1

    async def execute(caveman, goal, **kwargs):
        raise cli.UsageBudgetExceeded("Model-call budget exhausted")

    monkeypatch.setattr(cli, "SQLiteSession", Session)
    monkeypatch.setattr(cli, "_controller", lambda goal: Controller())
    monkeypatch.setattr(cli.RuntimeConfig, "from_env", classmethod(lambda cls: object()))
    monkeypatch.setattr(cli, "build_caveman", lambda value: object())
    monkeypatch.setattr(cli, "_execute", execute)

    responses = iter(["first goal", ":quit"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(responses))

    import asyncio
    asyncio.run(cli._run_interactive("fixture-session", 3))

    captured = capsys.readouterr()
    assert "Usage budget exceeded: Model-call budget exhausted" in captured.out
    assert observed["closed"] == 1


def test_interactive_followups_continue_the_current_run(monkeypatch, capsys):
    """Interactive mode keeps one durable run open across follow-ups until it
    completes or the operator asks for a new one (2026-09-21 decision)."""
    observed = {"created": 0, "closed": 0}

    class Session:
        def __init__(self, session_id, db):
            pass

        async def clear_session(self):
            pass

        def close(self):
            pass

    class Run:
        id = "durable-run"
        status = "active"
        final_result = None

    class Controller:
        run_id = "durable-run"

        def inspect(self):
            return Run()

        def close(self):
            observed["closed"] += 1

    def make_controller(goal):
        observed["created"] += 1
        return Controller()

    executed = []

    async def execute(caveman, goal, **kwargs):
        executed.append(goal)
        return object(), "trace"

    monkeypatch.setattr(cli, "SQLiteSession", Session)
    monkeypatch.setattr(cli, "_controller", make_controller)
    monkeypatch.setattr(cli.RuntimeConfig, "from_env", classmethod(lambda cls: object()))
    monkeypatch.setattr(cli, "build_caveman", lambda value: object())
    monkeypatch.setattr(cli, "_execute", execute)

    responses = iter(["first goal", "keep going", ":new", "fresh goal", ":quit"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(responses))

    import asyncio
    asyncio.run(cli._run_interactive("fixture-session", 3))

    assert executed == ["first goal", "keep going", "fresh goal"]
    assert observed["created"] == 2   # "keep going" reused the open run
    assert observed["closed"] == 2    # :new retired the first; exit retired the second
    output = capsys.readouterr().out
    assert ":new starts a fresh run" in output


def _terminal_run_with_candidate(tmp_path, monkeypatch):
    """A completed run owning one real candidate worktree and branch.

    chdir first: cli._store() resolves the operational database from the
    current working directory, so building the run before chdir would write
    into whatever store the developer happens to be sitting in.
    """
    from caveman.models import Event
    from caveman.sandbox import WorkspaceManager

    setup_repository(tmp_path)
    monkeypatch.chdir(tmp_path)
    store = cli._store()
    core = Orchestrator(store)
    run = core.create_run("cleanup fixture", ["fixture accepted"])
    workspaces = WorkspaceManager(tmp_path)
    grant = workspaces.create_candidate(run.id, "task", "author")
    snapshot = store.load(run.id)
    snapshot.status = "completed"
    snapshot.final_result = "fixture complete"
    store.save(snapshot, [Event(run_id=run.id, kind="test.forced_terminal")], snapshot.version)
    store.close()
    return run.id, grant


def _branches(tmp_path):
    return subprocess.run(["git", "-C", str(tmp_path), "branch", "--list",
                           "caveman-candidate/*"], capture_output=True, text=True,
                          check=True).stdout


def test_cleanup_refuses_an_active_run_and_leaves_its_workspace(tmp_path, monkeypatch):
    from caveman.sandbox import WorkspaceManager

    setup_repository(tmp_path)
    monkeypatch.chdir(tmp_path)
    store = cli._store()
    core = Orchestrator(store)
    run = core.create_run("active fixture", ["fixture accepted"])
    store.close()
    grant = WorkspaceManager(tmp_path).create_candidate(run.id, "task", "author")

    with pytest.raises(ValueError, match="still active"):
        cli._operations(["cleanup", run.id])
    # Refusal must not destroy anything.
    assert Path(grant.root).is_dir()
    assert grant.branch in _branches(tmp_path)


def test_cleanup_retires_a_terminal_run_without_touching_durable_state(tmp_path, monkeypatch, capsys):
    run_id, grant = _terminal_run_with_candidate(tmp_path, monkeypatch)
    before = cli._store()
    durable, events = before.load(run_id).model_dump_json(), before.events(run_id)
    before.close()

    cli._operations(["cleanup", run_id])
    report = json.loads(capsys.readouterr().out)
    assert report["run_status"] == "completed"
    assert report["durable_state"] == "unchanged"
    assert report["candidates"] == [{
        "candidate_id": grant.candidate_id, "branch": grant.branch,
        "worktree_removed": True, "branch_deleted": True,
    }]
    assert not Path(grant.root).exists()
    assert grant.branch not in _branches(tmp_path)

    # The run snapshot and the whole event log must survive byte-for-byte.
    after = cli._store()
    assert after.load(run_id).model_dump_json() == durable
    assert after.events(run_id) == events
    after.close()


def test_cleanup_reclaims_a_branch_left_by_an_out_of_band_worktree_removal(tmp_path, monkeypatch, capsys):
    """WorkspaceManager reconciles a vanished worktree to closed on construction.

    That left the candidate branch and a prunable worktree registration behind
    with nothing to reclaim them, so cleanup reported success having done
    nothing.
    """
    import shutil

    run_id, grant = _terminal_run_with_candidate(tmp_path, monkeypatch)
    shutil.rmtree(grant.root)
    assert grant.branch in _branches(tmp_path)

    cli._operations(["cleanup", run_id])
    report = json.loads(capsys.readouterr().out)
    assert report["candidates"] == [{
        "candidate_id": grant.candidate_id, "branch": grant.branch,
        "worktree_removed": False, "branch_deleted": True,
    }]
    assert grant.branch not in _branches(tmp_path)
    worktrees = subprocess.run(["git", "-C", str(tmp_path), "worktree", "list"],
                               capture_output=True, text=True, check=True).stdout
    assert "prunable" not in worktrees and grant.candidate_id not in worktrees

    # Idempotent: a second pass reports nothing further to reclaim.
    cli._operations(["cleanup", run_id])
    again = json.loads(capsys.readouterr().out)
    assert again["candidates"][0]["worktree_removed"] is False
    assert again["candidates"][0]["branch_deleted"] is False
