"""Durable run execution, decoupled from any HTTP request or browser connection.

A worker claims one queued job at a time under a lease, heartbeats while the
workflow driver runs, and records a plain outcome when it stops. Closing the browser
does nothing to a run; killing the worker leaves an expired lease that the next
worker converts into an explicit recovery job.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
from dataclasses import replace
from uuid import uuid4

from walter import runtime
from walter.adapter import DurableController
from walter.orchestration import Orchestrator
from walter.sandbox import WorkspaceManager
from walter.usage import UsageBudget, UsageBudgetExceeded

from .config import EXECUTOR_SCRIPTED, Settings
from .delivery import deliver_run
from .engine import Engine
from .platform_store import Job
from .views import Projector

logger = logging.getLogger("cavman.worker")

MAX_DETAIL_CHARS = 4000

PLATFORM_NOTES = ("Executable sandbox checks support Python (compile, pytest, pytest_regression) and "
                  "Node/TypeScript (node_test with node:test test files; tsc with a tsconfig.json and "
                  "typescript dependency; npm_build runs the project's own npm run build). Deliver other stacks as reviewed documents or source files, "
                  "and say so honestly in the final result.")


def _merge_budget(env_budget: UsageBudget | None, budget_usd: float, max_calls: int) -> UsageBudget:
    """The stricter of the environment budget and the run's own ceiling wins."""
    def stricter(a, b):
        return b if a is None else a if b is None else min(a, b)
    env_budget = env_budget or UsageBudget()
    return replace(env_budget, max_calls=stricter(env_budget.max_calls, max_calls),
                   max_cost_usd=stricter(env_budget.max_cost_usd, budget_usd))


class Worker:
    def __init__(self, settings: Settings, *, worker_id: str | None = None):
        self.settings = settings
        self.engine = Engine(settings)
        self.platform = settings.open_platform_store()
        self.projector = Projector(self.engine.redact, settings)
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:8]}"
        self._stopping = asyncio.Event()
        self.execution_backend = settings.execution_backend()
        self._scripted = None
        if settings.executor == EXECUTOR_SCRIPTED:
            from .scripted import PROVIDER, ScriptedProvider

            def loader(run_id):
                return lambda: self.engine.load(run_id)
            self._scripted = ScriptedProvider(loader, settings.scripted_step_delay)
            runtime.register_provider(PROVIDER, self._scripted)

    def close(self) -> None:
        self.platform.close()

    def stop(self) -> None:
        self._stopping.set()

    async def run_forever(self, poll_seconds: float = 1.0) -> None:
        logger.info("Cavman worker %s started (executor: %s, concurrency: %s)",
                    self.worker_id, self.settings.executor, self.settings.worker_concurrency)
        await asyncio.gather(*(self._slot(poll_seconds) for _ in range(self.settings.worker_concurrency)))

    async def _slot(self, poll_seconds: float) -> None:
        """One execution slot. Jobs are leased, so slots never share a job."""
        while not self._stopping.is_set():
            ran = await self.run_once()
            if not ran:
                try:
                    await asyncio.wait_for(self._stopping.wait(), timeout=poll_seconds)
                except asyncio.TimeoutError:
                    pass

    async def run_once(self) -> bool:
        self.platform.reap_expired(self.settings.max_recoveries)
        if self.platform.due("cleanup", self.settings.maintenance_interval_seconds):
            from .maintenance import run_maintenance
            try:
                await asyncio.to_thread(run_maintenance, self.settings, self.engine, self.platform)
            except Exception:
                logger.exception("Maintenance pass failed")
        job = self.platform.claim(self.worker_id, self.settings.lease_seconds)
        if job is None:
            return False
        await self.execute(job)
        return True

    def _limits(self, record) -> tuple[float, int]:
        """The run's own ceilings, tightened to what its account has left this month.

        Budgets count a run's lifetime usage, so the account's remaining
        allowance is added to what this run has already used.
        """
        from walter.usage import usage_cost

        from .accounts import account_usage

        account = account_usage(self.engine, self.platform, self.settings, record.owner_id)
        run = self.engine.load(record.id)
        run_cost = sum(usage_cost(u.raw_usage) or 0.0 for u in run.usage_records)
        run_calls = len(run.usage_records)
        return (min(record.budget_usd, run_cost + account["remaining_usd"]),
                min(record.max_model_calls, run_calls + account["remaining_calls"]))

    def _config(self, job: Job, record):
        budget_usd, max_calls = self._limits(record)
        if self.settings.executor == EXECUTOR_SCRIPTED:
            from .scripted import scripted_config
            key = f"scripted:{job.id}"
            self._scripted.prepare(key, job.run_id, job.kind)
            return scripted_config(key, _merge_budget(None, budget_usd, max_calls)), key
        config = runtime.RuntimeConfig.from_env()
        profiles = {mode: (manager, worker) for mode, manager, worker in self.settings.model_profiles}
        if record.model_mode in profiles:
            manager, worker = profiles[record.model_mode]
            config = replace(config, manager_model=manager, worker_model=worker)
        return replace(config, budget=_merge_budget(config.budget, budget_usd, max_calls),
                       worker_max_turns=self.settings.specialist_max_turns), None

    async def execute(self, job: Job) -> None:
        record = self.platform.run_by_id(job.run_id)
        if record is None:
            self.platform.finish(job.id, self.worker_id, "failed", "error", "Run record is missing.")
            return
        try:
            run = self.engine.load(job.run_id)
        except Exception as exc:  # corrupt or missing operational state
            self.platform.finish(job.id, self.worker_id, "failed", "error",
                                 self.engine.redact(f"Run state could not be loaded: {exc}", MAX_DETAIL_CHARS))
            return
        if run.status != "active":
            self.platform.finish(job.id, self.worker_id, "succeeded", "terminal", "")
            return
        if job.kind == "recover":
            self.engine.recover_interrupted(job.run_id)
        scripted_key = None
        try:
            config, scripted_key = self._config(job, record)
        except runtime.RuntimeConfigurationError as exc:
            self.platform.finish(job.id, self.worker_id, "failed", "config_error",
                                 self.engine.redact(str(exc), MAX_DETAIL_CHARS))
            return
        status, outcome, detail = await self._drive(job, record.project_id, config)
        if scripted_key:
            self._scripted.release(scripted_key)
        if outcome == "cancelled" or outcome == "interrupted":
            # The Manager was stopped mid-step; convert in-flight work to explicit
            # TIMEOUT failures so the next Manager turn must decide recovery.
            try:
                self.engine.recover_interrupted(job.run_id)
            except Exception:
                logger.exception("Interruption recovery failed for run %s", job.run_id)
        self.platform.finish(job.id, self.worker_id, status, outcome, detail)
        self.deliver_if_complete(job.run_id)

    async def _drive(self, job: Job, project_id: str, config) -> tuple[str, str, str]:
        from .workflow import WorkflowDriver

        store = self.settings.open_operations_store()
        cancelled_by_user = False
        try:
            controller = DurableController(Orchestrator(store), job.run_id,
                                           WorkspaceManager(self.engine.project_repo(project_id),
                                                            backend=self.execution_backend),
                                           config=config, integration=True, salvage_exhausted=True)
            driver = WorkflowDriver(
                controller, platform_notes=PLATFORM_NOTES,
                load_state=lambda: self.platform.workflow_state(job.run_id),
                save_state=lambda state: self.platform.save_workflow_state(job.run_id, state))
            task = asyncio.create_task(driver.run())
            while not task.done():
                done, _ = await asyncio.wait({task}, timeout=self.settings.heartbeat_seconds)
                if done:
                    break
                if self.platform.heartbeat(job.id, self.worker_id, self.settings.lease_seconds):
                    cancelled_by_user = True
                    task.cancel()
            try:
                result = await task
            except asyncio.CancelledError:
                if not cancelled_by_user:
                    raise
                return "cancelled", "cancelled", "Stopped at your request. Continue the run to resume."
            output = result if isinstance(result, str) else (
                result.final_output if isinstance(result.final_output, str) else "")
            return "succeeded", "succeeded", self.engine.redact(output, MAX_DETAIL_CHARS)
        except UsageBudgetExceeded as exc:
            return "failed", "budget_exceeded", self.engine.redact(str(exc), MAX_DETAIL_CHARS)
        except runtime.RuntimeConfigurationError as exc:
            return "failed", "config_error", self.engine.redact(str(exc), MAX_DETAIL_CHARS)
        except Exception as exc:
            logger.exception("Job %s failed", job.id)
            return "failed", "error", self.engine.redact(f"{type(exc).__name__}: {exc}", MAX_DETAIL_CHARS)
        finally:
            store.close()

    def deliver_if_complete(self, run_id: str) -> None:
        deliver_run(self.engine, self.platform, self.projector, run_id)


def main(settings: Settings | None = None) -> None:
    from .logs import configure_logging
    configure_logging()
    settings = settings or Settings.from_env()
    from .sandbox_probe import probe, probe_execution
    usable, detail = probe(backend=settings.sandbox_backend)
    if usable and settings.sandbox_backend != "bubblewrap":
        usable, detail = probe_execution(settings.execution_backend())
    if not usable:
        # Fail closed: without isolation every executable check would fail, and
        # there is no host fallback. See deploy/README.md for container options.
        raise SystemExit(f"Cavman worker refuses to start: {detail}")
    logger.info(detail)
    worker = Worker(settings)

    async def runner():
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, worker.stop)
        await worker.run_forever()

    try:
        asyncio.run(runner())
    finally:
        worker.close()
