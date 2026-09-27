from __future__ import annotations

import argparse
import asyncio
import getpass
import inspect
import os
import json
import sys
from pathlib import Path

from agents import RunConfig, Runner, SQLiteSession, trace
from dotenv import load_dotenv

from .runtime import RuntimeConfig, RuntimeConfigurationError, build_caveman
from .adapter import INITIAL_COMPLETION_CRITERION
from .usage import UsageBudgetExceeded


DEFAULT_SESSION = "main"
DEFAULT_MAX_TURNS = 30


def _session_db() -> str:
    local_dir = Path.cwd() / ".local"
    local_dir.mkdir(parents=True, exist_ok=True)
    return str(local_dir / "caveman-sessions.db")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="caveman",
        description="Run Caveman, the general-purpose orchestration Manager.",
    )
    parser.add_argument(
        "goal",
        nargs="*",
        help="Optional one-shot goal. Omit it to start an interactive Caveman session.",
    )
    parser.add_argument(
        "--session",
        default=None,
        help=(
            "Persist conversation state under this session ID. Interactive mode defaults "
            f"to '{DEFAULT_SESSION}'."
        ),
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        default=DEFAULT_MAX_TURNS,
        help=f"Maximum manager turns per run (default: {DEFAULT_MAX_TURNS}).",
    )
    parser.add_argument(
        "--trace-sensitive",
        action="store_true",
        help=(
            "Reserved compatibility flag. Provider trace export and sensitive trace payloads "
            "remain disabled in this runtime."
        ),
    )
    return parser


def _configure_trace_privacy(_include_sensitive: bool) -> None:
    # Provider tracing is deliberately disabled by runtime.build_models(). Keep
    # accepting the legacy flag without suggesting that sensitive data is exported.
    os.environ["OPENAI_AGENTS_TRACE_INCLUDE_SENSITIVE_DATA"] = "0"


async def _execute(
    caveman,
    goal: str,
    *,
    session=None,
    session_id: str | None = None,
    max_turns: int,
):
    metadata = {"runtime": "agents-sdk", "manager": "Caveman"}
    include_sensitive = (
        os.getenv("OPENAI_AGENTS_TRACE_INCLUDE_SENSITIVE_DATA", "0") == "1"
    )
    with trace(
        "Caveman orchestration",
        group_id=session_id,
        metadata=metadata,
    ) as workflow_trace:
        result = await Runner.run(
            caveman,
            goal,
            session=session,
            max_turns=max_turns,
            run_config=RunConfig(
                trace_include_sensitive_data=include_sensitive,
            ),
        )
    return result, workflow_trace.trace_id


async def _close_session(session) -> None:
    if session is None:
        return
    result = session.close()
    if inspect.isawaitable(result):
        await result


def _print_manager_output(result) -> None:
    final_output = getattr(result, "final_output", None)
    if isinstance(final_output, str) and final_output.strip():
        print(f"Manager: {final_output}")


def _report_usage_budget_exceeded(exc: UsageBudgetExceeded) -> None:
    print(
        f"Usage budget exceeded: {exc}. The run remains incomplete. "
        "Raise the CAVEMAN_MAX_* budget to continue."
    )


async def _run_once(goal: str, session_id: str | None, max_turns: int) -> None:
    RuntimeConfig.from_env()  # Validate before creating durable operational state.
    controller = _controller(goal)
    session = None
    try:
        session = SQLiteSession(session_id, _session_db()) if session_id else None
        result, trace_id = await _execute(
            build_caveman(controller), goal, session=session,
            session_id=session_id, max_turns=max_turns,
        )
        _print_outcome(controller)
        _print_manager_output(result)
        print(f"\nLocal trace ID (provider export disabled): {trace_id}")
    except UsageBudgetExceeded as exc:
        _report_usage_budget_exceeded(exc)
        raise SystemExit(1) from exc
    finally:
        await _close_session(session)
        controller.close()


async def _run_interactive(session_id: str, max_turns: int) -> None:
    session = SQLiteSession(session_id, _session_db())

    print(f"Caveman ready. Session: {session_id}")
    print("Commands: :new starts a fresh run, :clear resets this conversation, :quit exits.")
    print("Follow-up messages continue the current run until it completes.")
    print("Operational state is durable. Provider trace export is disabled.")

    # Interactive runs are a conversation, not a sequence of orphans: one
    # durable run stays open across follow-ups ("keep going", "now fix the
    # tests") until it completes or the operator explicitly starts a new one
    # (2026-09-21 decision). A fresh run per input line used to strand empty
    # sentinel-criterion runs in the durable ledger.
    controller = None
    try:
        while True:
            try:
                goal = input("\ncaveman> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return

            if not goal:
                continue
            if goal in {":quit", ":q", "quit", "exit"}:
                return
            if goal == ":clear":
                await session.clear_session()
                print("Session cleared.")
                continue
            if goal == ":new":
                if controller is not None:
                    controller.close()
                    controller = None
                print("Next message starts a fresh run.")
                continue

            try:
                RuntimeConfig.from_env()  # Never orphan a run on missing provider configuration.
                if controller is None:
                    controller = _controller(goal)
                    print(f"Run ID: {controller.run_id}")
                result, trace_id = await _execute(
                    build_caveman(controller), goal, session=session,
                    session_id=session_id, max_turns=max_turns,
                )
                _print_outcome(controller)
                _print_manager_output(result)
                print(f"\nLocal trace ID (provider export disabled): {trace_id}")
                if controller.inspect().status != "active":
                    controller.close()
                    controller = None
            except KeyboardInterrupt:
                print("\nRun interrupted.")
            except RuntimeConfigurationError as exc:
                print(f"Caveman configuration error: {exc}")
            except UsageBudgetExceeded as exc:
                _report_usage_budget_exceeded(exc)
    finally:
        if controller is not None:
            controller.close()
        await _close_session(session)



def _store():
    from .store import SQLiteStore
    directory = Path.cwd() / ".local"
    directory.mkdir(parents=True, exist_ok=True)
    return SQLiteStore(directory / "caveman-operations.db")


def _controller(goal=None, run_id=None):
    from .adapter import DurableController
    from .orchestration import Orchestrator
    from .sandbox import WorkspaceManager
    store = _store()
    try:
        core = Orchestrator(store)
        if run_id is None:
            run_id = core.create_run(goal, [INITIAL_COMPLETION_CRITERION]).id
        return DurableController(core, run_id, WorkspaceManager(Path.cwd()))
    except Exception:
        store.close()
        raise


def _print_outcome(controller):
    run = controller.inspect()
    print(f"Run ID: {run.id}\nDurable status: {run.status}")
    if run.status == "completed":
        print(run.final_result or "Completed through the kernel acceptance gate.")
    elif run.status == "abandoned":
        print("Run was abandoned offline. Durable tasks, decisions and events remain readable.")
    else:
        print("Run is not complete. Inspect durable tasks, blockers, and approvals with caveman run inspect " + run.id)


async def _resume(run_id, max_turns):
    controller = _controller(run_id=run_id)
    try:
        controller.core.resume(run_id)
        _print_outcome(controller)
    finally:
        controller.close()


async def _resume_and_execute(run_id, max_turns):
    RuntimeConfig.from_env()  # Validate before recording a resume mutation.
    controller = _controller(run_id=run_id)
    try:
        controller.core.resume(run_id)
        result, _ = await _execute(build_caveman(controller),
            "Continue this durable run from its persisted state. Inspect it first; recover interrupted assignments explicitly.",
            max_turns=max_turns)
        _print_outcome(controller)
        _print_manager_output(result)
    except UsageBudgetExceeded as exc:
        _report_usage_budget_exceeded(exc)
        raise SystemExit(1) from exc
    finally:
        controller.close()


def _local_human_principal() -> str:
    uid = os.getuid() if hasattr(os, "getuid") else "unknown"
    return f"local-os:{getpass.getuser()}:uid:{uid}"


def _cleanup(run_id: str) -> None:
    """Retire candidate worktrees, branches and grants for one terminal run.

    Durable operational state is never touched. The run snapshot and its
    append-only event log are the record of what happened and stay fully
    readable after cleanup; only reclaimable filesystem and grant state goes.
    """
    from .sandbox import SandboxViolation, WorkspaceManager

    store = _store()
    try:
        run = store.load(run_id)
        if run.status == "active":
            raise ValueError(
                f"run {run_id} is still active; cleanup only retires workspaces for a "
                "terminal run. Finish or abandon it first.")
        try:
            workspaces = WorkspaceManager(Path.cwd())
            retired = workspaces.retire_run(run_id)
        except SandboxViolation as exc:
            # SandboxViolation subclasses OSError, which main() does not
            # translate, so it would otherwise surface as a traceback.
            raise RuntimeError(f"workspace retirement refused: {exc}") from exc
        print(json.dumps({
            "run_id": run_id,
            "run_status": run.status,
            "candidates": retired,
            "durable_state": "unchanged",
        }, indent=2))
    finally:
        store.close()


def _abandon(run_id: str, reason: str) -> None:
    """Close a work-free active run offline, without invoking the Manager model.

    Only durable status changes: no provider calls, no workspace mutation. This
    is the offline exit for runs whose plan holds no in-flight work and no
    pending gate, which otherwise stay `active` forever. Refusals name the exact
    in-flight task or pending gate, so the operator knows what still needs a
    decision.
    """
    store = _store()
    try:
        from .orchestration import Orchestrator

        core = Orchestrator(store)
        run = core.abandon(run_id, reason, actor_id=_local_human_principal())
        print(json.dumps({
            "run_id": run.id,
            "run_status": run.status,
            "plan_status": run.plan.status,
            "reason": reason,
            "durable_state": "preserved",
        }, indent=2))
    finally:
        store.close()


def _operations(argv):
    parser = argparse.ArgumentParser(prog="caveman run")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list")
    for name in ("inspect", "events", "resume"):
        command = commands.add_parser(name)
        command.add_argument("run_id")
        if name == "resume":
            command.add_argument("--max-turns", type=int, default=DEFAULT_MAX_TURNS)
            command.add_argument("--execute", action="store_true",
                                 help="After offline recovery, invoke the configured Manager model.")
    approval = commands.add_parser("approve")
    approval.add_argument("run_id")
    approval.add_argument("approval_id")
    approval.add_argument("--deny", action="store_true")
    approval.add_argument("--reason", required=True)
    cleanup = commands.add_parser("cleanup", help=(
        "Retire candidate worktrees, branches and workspace grants for a terminal run. "
        "Durable run and event state is never modified."))
    cleanup.add_argument("run_id")
    abandon = commands.add_parser("abandon", help=(
        "Close a work-free active run offline (no provider calls). Refused while the run "
        "holds in-flight work, a pending approval, or a pending capability request."))
    abandon.add_argument("run_id")
    abandon.add_argument("--reason", required=True)
    commands.add_parser("readiness-demo")
    args = parser.parse_args(argv)
    if args.command == "readiness-demo":
        from .readiness import run_readiness_demo
        report = run_readiness_demo(Path.cwd())
        print(report.model_dump_json(indent=2) if hasattr(report, "model_dump_json") else json.dumps(report, indent=2))
        return
    if args.command == "abandon":
        _abandon(args.run_id, args.reason)
        return
    if args.command == "cleanup":
        _cleanup(args.run_id)
        return
    if args.command == "resume":
        target = _resume_and_execute if args.execute else _resume
        asyncio.run(target(args.run_id, args.max_turns))
        return
    store = _store()
    try:
        if args.command == "list":
            print(json.dumps([{"id": r.id, "objective": r.objective, "status": r.status} for r in store.list_runs()], indent=2))
        elif args.command == "inspect":
            print(store.load(args.run_id).model_dump_json(indent=2))
        elif args.command == "events":
            print(json.dumps([e.model_dump(mode="json") for e in store.events(args.run_id)], indent=2))
        elif args.command == "approve":
            from .orchestration import Orchestrator
            core = Orchestrator(store)
            core.decide_approval(args.run_id, args.approval_id, approved=not args.deny,
                                 human_id=_local_human_principal(), reason=args.reason)
            print(store.load(args.run_id).model_dump_json(indent=2))
    finally:
        store.close()


def main() -> None:
    load_dotenv()
    if len(sys.argv) > 1 and sys.argv[1] == "run":
        try:
            _operations(sys.argv[2:])
        except (KeyError, ValueError, RuntimeError) as exc:
            raise SystemExit(f"Caveman operation error: {exc}") from exc
        return
    args = _parser().parse_args()
    _configure_trace_privacy(args.trace_sensitive)

    if args.max_turns < 1:
        raise SystemExit("--max-turns must be at least 1.")

    goal = " ".join(args.goal).strip()
    try:
        if goal:
            asyncio.run(_run_once(goal, args.session, args.max_turns))
            return
        asyncio.run(_run_interactive(args.session or DEFAULT_SESSION, args.max_turns))
    except RuntimeConfigurationError as exc:
        raise SystemExit(f"Caveman configuration error: {exc}") from exc


if __name__ == "__main__":
    main()
