"""Cavman API: the authenticated control plane around the orchestration core.

Trust model
-----------
* The API is private. Every request (except health) must present the shared
  ``CAVMAN_API_TOKEN``, which only the Cavman web server holds. Browsers talk
  to the web server, which authenticates the user session and forwards the
  user's identity in ``X-Cavman-User``.
* Authorization is enforced here: every project and run lookup is filtered by
  that owner. Another user's run is indistinguishable from a missing one.
* No endpoint can record validation, review, acceptance, completion, artifact
  provenance or test results. Those are written only by the kernel during
  execution. Users can decide pending approvals (bound to the exact scope
  digest they were shown), queue or stop execution, and read state.
"""
from __future__ import annotations

import asyncio
import base64
import hmac
import json
import math
import re
import shutil
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator

from walter.orchestration import GateError

from . import __version__, importer
from .config import EXECUTOR_PROVIDER, Settings
from .accounts import account_disk_bytes, account_usage, cost_history, server_usage
from .billing import Billing, BillingError
from .delivery import deliver_run
from .engine import ApprovalScopeChanged, Engine
from .platform_store import RunRecord, new_id
from .views import Projector

USER_ID = re.compile(r"^[A-Za-z0-9_.:@-]{1,128}$")


# Request models ------------------------------------------------------------

class BuildSettings(BaseModel):
    stack: str | None = Field(default=None, max_length=300)
    constraints: str | None = Field(default=None, max_length=2000)
    deployment_target: str | None = Field(default=None, max_length=200)
    budget_usd: float | None = Field(default=None, gt=0)
    model_mode: Literal["automatic", "budget", "balanced", "quality"] = "automatic"
    # New projects only: start from a public GitHub repository's files.
    repository_url: str | None = Field(default=None, max_length=300)


class BuildRequest(BaseModel):
    prompt: str = Field(min_length=3, max_length=8000)
    project_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    name: str | None = Field(default=None, max_length=80)
    settings: BuildSettings = Field(default_factory=BuildSettings)

    @field_validator("prompt")
    @classmethod
    def substantive(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 3:
            raise ValueError("Describe what you want to build.")
        return value


class ProjectRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=2000)
    repository_url: str | None = Field(default=None, max_length=300)


class ApprovalDecisionRequest(BaseModel):
    decision: Literal["approve", "reject"]
    scope_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason: str = Field(default="", max_length=2000)


class ContinueRequest(BaseModel):
    message: str = Field(default="", max_length=4000)


class InstructionRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)

    @field_validator("message")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Write an instruction.")
        return value


MAX_INSTRUCTIONS = 20


class AbandonRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=2000)


class PublishRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    private: bool = True
    confirm: Literal[True]
    github_token: str = Field(min_length=10, max_length=500)


class BudgetRequest(BaseModel):
    budget_usd: float = Field(gt=0)


class CheckoutRequest(BaseModel):
    kind: Literal["builder", "topup"]


# Helpers -------------------------------------------------------------------

def project_name_from_prompt(prompt: str) -> str:
    text = " ".join(prompt.split())
    text = re.sub(r"(?i)^(please\s+)?(build|make|create|write|design)\s+(me\s+)?(an?\s+|the\s+)?", "", text)
    text = re.sub(r"#[\w-]+", "", text).strip(" .!?")
    text = text[:1].upper() + text[1:]
    if len(text) > 60:
        text = text[:57].rsplit(" ", 1)[0] + "..."
    return text or "Untitled project"


def _constraints(settings: BuildSettings) -> list[str]:
    items = []
    if settings.stack and settings.stack.strip():
        items.append("Preferred stack: " + settings.stack.strip())
    if settings.constraints and settings.constraints.strip():
        items.append("Constraints: " + settings.constraints.strip())
    if settings.deployment_target and settings.deployment_target.strip():
        items.append("Intended deployment target: " + settings.deployment_target.strip()
                     + " (Cavman does not deploy; this informs the design only)")
    return items


def _provider_status() -> dict:
    from walter.runtime import RuntimeConfig, RuntimeConfigurationError
    try:
        config = RuntimeConfig.from_env()
    except RuntimeConfigurationError as exc:
        return {"configured": False, "problem": str(exc)}
    return {"configured": True, "provider": config.provider, "manager_model": config.manager_model,
            "worker_model": config.worker_model}


def _toolchains() -> list[str]:
    from walter.sandbox import _detect_node_root
    return ["python"] + (["node"] if _detect_node_root() is not None else [])


def _sandbox_status(backend: str = "bubblewrap") -> dict:
    from .sandbox_probe import probe
    usable, detail = probe(backend=backend)
    return {"available": usable, "detail": detail, "backend": backend,
            "bubblewrap": Path("/usr/bin/bwrap").exists(), "prlimit": Path("/usr/bin/prlimit").exists()}


def principal(request: Request, authorization: Annotated[str | None, Header()] = None,
              x_cavman_user: Annotated[str | None, Header()] = None,
              x_caveman_user: Annotated[str | None, Header()] = None) -> str:
    """Authenticate the calling web server and return the user it vouches for."""
    expected = f"Bearer {request.app.state.settings.api_token}"
    if not authorization or not hmac.compare_digest(authorization.encode(), expected.encode()):
        raise HTTPException(401, "Missing or invalid service credentials.")
    # A web server from before the Caveman -> Cavman rename still sends X-Caveman-User.
    user = x_cavman_user or x_caveman_user
    if not user or not USER_ID.fullmatch(user):
        raise HTTPException(401, "Missing user identity.")
    return user


User = Annotated[str, Depends(principal)]


def service(request: Request, authorization: Annotated[str | None, Header()] = None) -> None:
    """Authenticate the calling web server for requests on no user's behalf."""
    expected = f"Bearer {request.app.state.settings.api_token}"
    if not authorization or not hmac.compare_digest(authorization.encode(), expected.encode()):
        raise HTTPException(401, "Missing or invalid service credentials.")
# The importing user's GitHub token, set only by the web server from its auth store.
GitHubToken = Annotated[str | None, Header(max_length=500)]

# Lists are paged newest first. A cursor is the URL-safe base64 of
# "<created_at>~<id>" of the last item shown; keyset paging stays correct while
# new builds are added.
PageSize = Annotated[int, Query(ge=1, le=100)]
Cursor = Annotated[str | None, Query(max_length=120, pattern=r"^[A-Za-z0-9_-]+={0,2}$")]
CURSOR = re.compile(r"(\d{4}-\d\d-\d\dT[0-9:.+\-]+)~([0-9a-f]{32})")


def parse_cursor(cursor: str | None) -> tuple[str, str] | None:
    if not cursor:
        return None
    try:
        match = CURSOR.fullmatch(base64.urlsafe_b64decode(cursor.encode()).decode())
    except (ValueError, UnicodeDecodeError):
        match = None
    if match is None:
        raise HTTPException(422, "Invalid page cursor.")
    return match.group(1), match.group(2)


def next_cursor(rows: list, limit: int) -> str | None:
    """The cursor for the next page, when one more row than the page was found."""
    if len(rows) <= limit:
        return None
    last = rows[limit - 1]
    return base64.urlsafe_b64encode(f"{last.created_at}~{last.id}".encode()).decode()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    engine = Engine(settings)
    platform = settings.open_platform_store()
    projector = Projector(engine.redact, settings)

    @asynccontextmanager
    async def lifespan(_app):
        yield
        platform.close()

    app = FastAPI(title="Cavman API", version=__version__, lifespan=lifespan,
                  docs_url=None if settings.environment == "production" else "/api/docs",
                  openapi_url=None if settings.environment == "production" else "/api/openapi.json",
                  redoc_url=None)
    app.state.settings = settings
    app.state.engine = engine
    app.state.platform = platform
    billing = Billing(settings, platform)
    app.state.billing = billing


    def owned_run(user: str, run_id: str) -> RunRecord:
        if not re.fullmatch(r"[0-9a-f]{32}", run_id):
            raise HTTPException(404, "Run not found.")
        record = platform.get_run(user, run_id)
        if record is None:
            raise HTTPException(404, "Run not found.")
        return record

    def project_name(project_id: str) -> str:
        project = platform.project_by_id(project_id)
        return project.name if project else "Project"

    def pending_questions(record: RunRecord, run) -> list[str]:
        """Questions the planner asked that the run is still waiting on (before any plan exists)."""
        from walter.adapter import INITIAL_COMPLETION_CRITERION

        if run.status != "active" or run.plan.completion_criteria != [INITIAL_COMPLETION_CRITERION]:
            return []
        return list((platform.workflow_state(record.id) or {}).get("questions") or [])

    def with_questions(view: dict, record: RunRecord, run) -> dict:
        questions = pending_questions(record, run)
        if questions and view["state"] == "waiting":
            view.update(state="input_needed", label="Needs your input",
                        explanation="Cavman has a few questions before it plans this build. Answer them, "
                                    "and it continues.")
        return view

    def summary(record: RunRecord) -> dict:
        run = engine.load(record.id)
        return with_questions(projector.run_summary(run, record, platform.jobs(record.id),
                                                    project_name(record.project_id)), record, run)

    def delivery_view(run_id: str) -> dict | None:
        delivery = platform.delivery(run_id)
        if delivery is None:
            return None
        manifest = delivery.manifest
        return {"status": delivery.status, "created_at": delivery.created_at,
                "error": manifest.get("error"),
                "files": sorted(manifest.get("files", {})),
                "documents": [d["path"] for d in manifest.get("documents", [])],
                "deleted": manifest.get("deleted", []),
                "total_files": manifest.get("total_files"),
                "commit": manifest.get("commit"),
                "report": manifest.get("report"),
                "downloadable": delivery.status == "ready" and bool(delivery.archive_name)}

    def publication_view(run_id: str) -> dict | None:
        found = platform.publication(run_id)
        if found is None:
            return None
        return {"repository": found["repo_full_name"], "url": found["html_url"], "commit": found["commit_sha"],
                "private": bool(found["private"]), "created_at": found["created_at"]}

    def detail(record: RunRecord) -> dict:
        run = engine.load(record.id)
        view = projector.run_detail(run, record, platform.jobs(record.id), engine.events(record.id),
                                    project_name(record.project_id), delivery_view(record.id))
        view["publication"] = publication_view(record.id)
        view["instructions"] = [{key: item[key] for key in ("id", "text", "created_at")}
                                for item in platform.instructions(record.id)]
        view["questions"] = pending_questions(record, run)
        return with_questions(view, record, run)

    @app.exception_handler(importer.RepositoryImportError)
    async def import_refused(_request, exc: importer.RepositoryImportError):
        return JSONResponse({"detail": str(exc), "needs_scope": exc.needs_scope}, status_code=exc.status)

    @app.exception_handler(GateError)
    async def gate_error(_request, exc: GateError):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(BillingError)
    async def billing_error(_request, exc: BillingError):
        return JSONResponse({"detail": str(exc)}, status_code=exc.status)

    # Public -----------------------------------------------------------

    @app.get("/api/health")
    def health():
        return {"status": "ok", "service": "cavman-api", "version": __version__}

    @app.get("/api/metrics")
    def metrics(authorization: Annotated[str | None, Header()] = None):
        from fastapi.responses import PlainTextResponse
        if not settings.metrics_token:
            raise HTTPException(404, "Not found.")
        if not authorization or not hmac.compare_digest(authorization.encode(),
                                                        f"Bearer {settings.metrics_token}".encode()):
            raise HTTPException(401, "Missing or invalid metrics credentials.")
        lines = ["# HELP cavman_jobs Jobs by status and outcome.", "# TYPE cavman_jobs gauge"]
        for (status, outcome), count in sorted(platform.job_counts().items()):
            lines.append(f'cavman_jobs{{status="{status}",outcome="{outcome}"}} {count}')
        queue = platform.queue_stats()
        lines += ["# HELP cavman_queue_oldest_seconds Age of the oldest queued job.",
                  "# TYPE cavman_queue_oldest_seconds gauge",
                  f"cavman_queue_oldest_seconds {queue['oldest_queued_seconds']:.1f}",
                  "# HELP cavman_expired_leases Running jobs whose worker stopped heartbeating.",
                  "# TYPE cavman_expired_leases gauge",
                  f"cavman_expired_leases {queue['expired_leases']}",
                  "# HELP cavman_runs Runs recorded on this server.", "# TYPE cavman_runs gauge",
                  f"cavman_runs {len(platform.all_runs())}",
                  "# HELP cavman_deliveries Delivery archives by status.", "# TYPE cavman_deliveries gauge"]
        # Always export the failed series, so the first failure is a rise from 0.
        for status, count in sorted({"failed": 0, **platform.delivery_counts()}.items()):
            lines.append(f'cavman_deliveries{{status="{status}"}} {count}')
        sandbox = _sandbox_status(settings.sandbox_backend)
        lines += ["# HELP cavman_sandbox_available Whether isolation works on the API host.",
                  "# TYPE cavman_sandbox_available gauge",
                  f"cavman_sandbox_available {1 if sandbox['available'] else 0}"]
        spend = server_usage(engine, platform)
        lines += ["# HELP cavman_spend_month_usd Provider-reported model cost this calendar month (UTC), all accounts.",
                  "# TYPE cavman_spend_month_usd gauge",
                  f"cavman_spend_month_usd {spend['spent_usd']:.6f}",
                  "# HELP cavman_model_calls_month Model calls this calendar month, all accounts.",
                  "# TYPE cavman_model_calls_month gauge",
                  f"cavman_model_calls_month {spend['model_calls']}",
                  "# HELP cavman_model_calls_without_cost_month Calls this month whose provider reported no cost.",
                  "# TYPE cavman_model_calls_without_cost_month gauge",
                  f"cavman_model_calls_without_cost_month {spend['calls_without_cost']}"]
        return PlainTextResponse("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")

    # System -----------------------------------------------------------

    @app.get("/api/system")
    def system(_user: User):
        provider = _provider_status() if settings.executor == EXECUTOR_PROVIDER else {
            "configured": True, "provider": "scripted test executor"}
        modes = [{"mode": "automatic", "available": True, "manager_model": provider.get("manager_model"),
                  "worker_model": provider.get("worker_model")}]
        configured = {mode: (manager, worker) for mode, manager, worker in settings.model_profiles}
        for mode in ("budget", "balanced", "quality"):
            manager, worker = configured.get(mode, (None, None))
            modes.append({"mode": mode, "available": mode in configured,
                          "manager_model": manager, "worker_model": worker})
        return {"version": __version__, "executor": settings.executor, "model_modes": modes,
                "provider": provider,
                "sandbox": _sandbox_status(settings.sandbox_backend),
                "budget": {"default_usd": settings.default_budget_usd, "max_usd": settings.max_budget_usd,
                           "account_monthly_usd": settings.account_monthly_budget_usd,
                           "account_monthly_calls": settings.account_monthly_max_calls,
                           "default_max_model_calls": settings.default_max_model_calls,
                           "warning_ratio": settings.budget_warning_ratio},
                "capabilities": {"github_publish": True, "previews": False,
                                 "sandbox_toolchains": _toolchains()}}

    def require_account_allowance(user: str) -> None:
        usage = account_usage(engine, platform, settings, user)
        if usage["exhausted"]:
            raise HTTPException(402, "Your monthly spending limit is reached "
                                     f"(${usage['spent_usd']:.2f} of ${usage['limit_usd']:.2f}, "
                                     f"{usage['model_calls']} of {usage['max_model_calls']} model calls). "
                                     + ("New work starts again next month, or add usage from Settings > Billing."
                                        if billing.enabled else
                                        "New work starts again next month or when an operator raises the limit."))

    def _wait(seconds: float) -> str:
        minutes = math.ceil(seconds / 60)
        return f"{math.ceil(seconds)} seconds" if seconds < 90 else f"{minutes} minutes"

    def rate_limit(user: str, action: str, limit: int, window_seconds: float, label: str) -> None:
        wait = platform.consume_rate(user, action, limit, window_seconds)
        if wait is not None:
            raise HTTPException(429, f"You've reached the limit of {limit} {label}. Try again in {_wait(wait)}.",
                                headers={"Retry-After": str(math.ceil(wait))})

    def act(user: str) -> None:
        rate_limit(user, "action", settings.actions_per_minute, 60, "actions per minute")

    def require_capacity(user: str, *, new_project: bool) -> None:
        running = platform.active_build_count(user)
        if running >= settings.max_concurrent_builds:
            raise HTTPException(429, f"You already have {running} builds running, the most at once. "
                                     "Wait for one to finish, or stop it, then try again.")
        if new_project and platform.project_count(user) >= settings.max_projects:
            raise HTTPException(403, f"You have reached the limit of {settings.max_projects} projects.")
        if account_disk_bytes(settings, platform, user) >= settings.account_disk_mb * 1024 * 1024:
            raise HTTPException(403, f"Your projects use more than your {settings.account_disk_mb} MB of storage. "
                                     "Delete your account data or ask an operator to raise the limit.")

    @app.get("/api/account")
    def account(user: User):
        return {"spending": account_usage(engine, platform, settings, user)}

    history_cache: dict[str, tuple[float, dict]] = {}

    @app.get("/api/estimate")
    def estimate(user: User):
        """What to expect before starting a build: real recent costs, the
        build's ceiling and the account's remaining allowance."""
        cached = history_cache.get("history")
        if cached is None or time.monotonic() - cached[0] > 300:
            cached = (time.monotonic(), cost_history(engine, platform))
            history_cache["history"] = cached
        spending = account_usage(engine, platform, settings, user)
        return {**cached[1],
                "default_budget_usd": settings.default_budget_usd, "max_budget_usd": settings.max_budget_usd,
                "default_max_model_calls": settings.default_max_model_calls,
                "account": {key: spending[key] for key in (
                    "remaining_usd", "limit_usd", "remaining_calls", "max_model_calls",
                    "cost_complete", "calls_without_cost", "exhausted")}}

    @app.get("/api/account/export")
    def export_account(user: User):
        """Everything Cavman holds for this account, as shown to its owner.

        Streamed one run at a time, so a large account never sits in memory as
        one document. The body is a single JSON object that starts with "{".
        """
        projects = [project_view(project, []) for project in platform.list_projects(user)]
        for project in projects:
            project.pop("runs", None)
        head = {"format": "cavman-export/1", "exported_at": datetime.now(timezone.utc).isoformat(),
                "user_id": user, "spending": account_usage(engine, platform, settings, user),
                "notes": "Delivered project files are in each run's download archive; "
                         "they are not repeated here.", "projects": projects}
        records = platform.list_runs(user)

        def body():
            yield json.dumps(head)[:-1] + ', "runs": ['
            for index, record in enumerate(records):
                view = detail(record)
                run = engine.load(record.id)
                view["timeline"] = [projector.event(run, event) for event in engine.events(record.id)]
                view["artifacts"] = [projector.artifact(run, artifact, content=True)
                                     for artifact in run.artifacts.values()]
                yield ("," if index else "") + json.dumps(view)
            yield "]}"

        return StreamingResponse(body(), media_type="application/json")

    # Billing ----------------------------------------------------------

    @app.get("/api/billing")
    def billing_view(user: User):
        return billing.view(user, account_usage(engine, platform, settings, user)["credit_usd"])

    @app.post("/api/billing/checkout")
    def billing_checkout(body: CheckoutRequest, user: User):
        act(user)
        return {"url": billing.checkout(user, body.kind)}

    @app.post("/api/billing/portal")
    def billing_portal(user: User):
        act(user)
        return {"url": billing.portal(user)}

    @app.post("/api/billing/webhook", dependencies=[Depends(service)])
    async def billing_webhook(request: Request, stripe_signature: Annotated[str | None, Header()] = None):
        """Stripe's events, relayed byte for byte by the web server."""
        if not stripe_signature:
            raise HTTPException(400, "Missing Stripe-Signature.")
        payload = await request.body()
        try:
            handled = await asyncio.to_thread(billing.handle, payload, stripe_signature)
        except ValueError:
            raise HTTPException(400, "Invalid Stripe signature.") from None
        return {"received": handled}

    @app.delete("/api/account")
    def delete_account(user: User):
        from .erasure import erase_account
        from .platform_store import ActiveWork
        subscription = platform.billing_account(user)
        try:
            deleted = erase_account(settings, engine, platform, user)
        except ActiveWork as exc:
            raise HTTPException(409, str(exc)) from None
        # After the erase succeeded: a refused deletion keeps its plan.
        billing.cancel_for_deletion(subscription)
        return {"deleted": deleted}

    # Builds -----------------------------------------------------------

    @app.post("/api/builds", status_code=201)
    def create_build(body: BuildRequest, user: User, x_cavman_github_token: GitHubToken = None):
        if settings.executor == EXECUTOR_PROVIDER:
            status = _provider_status()
            if not status["configured"]:
                raise HTTPException(503, "Cavman's model provider is not configured on the server yet, "
                                         "so builds cannot start. An operator needs to set "
                                         "OPENROUTER_API_KEY for the Cavman worker.")
        require_account_allowance(user)
        modes = {"automatic"} | {mode for mode, _, _ in settings.model_profiles}
        if body.settings.model_mode not in modes:
            raise HTTPException(422, f"The model mode '{body.settings.model_mode}' is not configured on this server.")
        budget = body.settings.budget_usd or settings.default_budget_usd
        if budget > settings.max_budget_usd:
            raise HTTPException(422, f"The maximum budget per run is ${settings.max_budget_usd:.2f}.")
        repository_url = (body.settings.repository_url or "").strip() or None
        require_capacity(user, new_project=not body.project_id)
        rate_limit(user, "build", settings.builds_per_hour, 3600, "new builds per hour")
        if repository_url and not body.project_id:
            rate_limit(user, "import", settings.imports_per_hour, 3600, "repository imports per hour")
        if body.project_id:
            if repository_url:
                raise HTTPException(422, "An existing project already has its files; "
                                         "start a new project to import a repository.")
            project = platform.get_project(user, body.project_id)
            if project is None:
                raise HTTPException(404, "Project not found.")
        else:
            name = (body.name or "").strip() or project_name_from_prompt(body.prompt)
            project_id = new_id()
            source = new_project_repo(project_id, name, body.prompt, repository_url, x_cavman_github_token)
            try:
                project = platform.create_project(user, name, body.prompt,
                                                  {**body.settings.model_dump(exclude_none=True), **source},
                                                  project_id=project_id)
            except Exception:
                shutil.rmtree(engine.project_repo(project_id).parent, ignore_errors=True)
                raise
        run = engine.create_run(body.prompt, _constraints(body.settings))
        record = platform.create_run(run.id, project.id, user, body.prompt, executor=settings.executor,
                                     budget_usd=budget, max_model_calls=settings.default_max_model_calls,
                                     model_mode=body.settings.model_mode)
        platform.enqueue(run.id, "start")
        return {"run_id": run.id, "project_id": project.id, "run": summary(record)}

    # Projects ---------------------------------------------------------

    def new_project_repo(project_id: str, name: str, description: str, repository_url: str | None,
                         user_token: str | None = None) -> dict:
        """Create the project repository, empty or from a GitHub repository.

        ``user_token`` is the user's own GitHub token, attached by the web server
        (never the browser) when the account has granted repository access. It
        is used for this one download and not stored."""
        if not repository_url:
            engine.init_project_repo(project_id, name, description)
            return {}
        try:
            imported = importer.import_repository(
                repository_url, engine.project_repo(project_id), name, api_url=settings.github_api_url,
                max_mb=settings.import_max_mb, max_files=settings.import_max_files,
                token=settings.github_import_token, user_token=user_token)
        except Exception:
            shutil.rmtree(engine.project_repo(project_id).parent, ignore_errors=True)
            raise
        return {"repository_url": imported.url,
                "source": {"url": imported.url, "commit": imported.commit, "branch": imported.branch,
                           "files": imported.files, "dropped": list(imported.dropped),
                           "private": imported.private}}

    def project_view(project, runs: list[RunRecord]) -> dict:
        summaries = [summary(r) for r in runs]
        return {"id": project.id, "name": project.name,
                "description": engine.redact(project.description, limit=2000),
                "settings": project.settings, "created_at": project.created_at,
                "updated_at": project.updated_at, "run_count": len(runs),
                "latest_run": summaries[0] if summaries else None, "runs": summaries}

    @app.get("/api/projects")
    def list_projects(user: User, limit: PageSize = 24, before: Cursor = None):
        projects = platform.list_projects(user, limit=limit + 1, before=parse_cursor(before))
        items = []
        for project in projects[:limit]:
            view = project_view(project, platform.list_runs(user, project.id, limit=1))
            view["run_count"] = platform.count_runs(user, project.id)
            view.pop("runs")
            items.append(view)
        return {"projects": items, "next": next_cursor(projects, limit)}

    @app.post("/api/projects", status_code=201)
    def create_project(body: ProjectRequest, user: User, x_cavman_github_token: GitHubToken = None):
        if platform.project_count(user) >= settings.max_projects:
            raise HTTPException(403, f"You have reached the limit of {settings.max_projects} projects.")
        act(user)
        if (body.repository_url or "").strip():
            require_capacity(user, new_project=True)
            rate_limit(user, "import", settings.imports_per_hour, 3600, "repository imports per hour")
        project_id = new_id()
        source = new_project_repo(project_id, body.name.strip(), body.description,
                                  (body.repository_url or "").strip() or None, x_cavman_github_token)
        try:
            project = platform.create_project(user, body.name.strip(), body.description, source,
                                              project_id=project_id)
        except Exception:
            shutil.rmtree(engine.project_repo(project_id).parent, ignore_errors=True)
            raise
        return project_view(project, [])

    @app.get("/api/projects/{project_id}")
    def get_project(project_id: str, user: User, limit: PageSize = 25, before: Cursor = None):
        if not re.fullmatch(r"[0-9a-f]{32}", project_id):
            raise HTTPException(404, "Project not found.")
        project = platform.get_project(user, project_id)
        if project is None:
            raise HTTPException(404, "Project not found.")
        runs = platform.list_runs(user, project_id, limit=limit + 1, before=parse_cursor(before))
        latest = runs[:1] if before is None else platform.list_runs(user, project_id, limit=1)
        view = project_view(project, runs[:limit])
        view["run_count"] = platform.count_runs(user, project_id)
        view["latest_run"] = summary(latest[0]) if latest else None
        return {**view, "next": next_cursor(runs, limit)}

    # Runs -------------------------------------------------------------

    @app.get("/api/runs")
    def list_runs(user: User, project_id: str | None = None, limit: PageSize = 25, before: Cursor = None):
        runs = platform.list_runs(user, project_id, limit=limit + 1, before=parse_cursor(before))
        return {"runs": [summary(r) for r in runs[:limit]], "next": next_cursor(runs, limit)}

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str, user: User):
        return detail(owned_run(user, run_id))

    @app.get("/api/runs/{run_id}/tasks/{task_id}")
    def get_task(run_id: str, task_id: str, user: User):
        record = owned_run(user, run_id)
        run = engine.load(record.id)
        task = run.tasks.get(task_id)
        if task is None:
            raise HTTPException(404, "Task not found.")
        view = projector.task(run, task)
        view["artifacts"] = [projector.artifact(run, run.artifacts[a]) for a in task.artifact_ids]
        view["failures"] = [f for f in projector.failures(run) if f["task_id"] == task_id]
        return view

    @app.get("/api/runs/{run_id}/artifacts/{artifact_id}")
    def get_artifact(run_id: str, artifact_id: str, user: User):
        record = owned_run(user, run_id)
        run = engine.load(record.id)
        artifact = run.artifacts.get(artifact_id)
        if artifact is None:
            raise HTTPException(404, "Artifact not found.")
        return projector.artifact(run, artifact, content=True)

    @app.get("/api/runs/{run_id}/events")
    def get_events(run_id: str, user: User, after: int = Query(0, ge=0), debug: bool = False):
        record = owned_run(user, run_id)
        run = engine.load(record.id)
        events = [projector.event(run, e) for e in engine.events(record.id, after)]
        return {"events": [e for e in events if debug or not e["debug"]],
                "cursor": run.event_cursor}

    @app.get("/api/runs/{run_id}/stream")
    async def stream(run_id: str, request: Request, user: User, after: int = Query(0, ge=0),
                     last_event_id: Annotated[str | None, Header()] = None):
        record = owned_run(user, run_id)
        cursor = int(last_event_id) if last_event_id and last_event_id.isdigit() else after

        async def generate():
            nonlocal cursor
            last_signature = None
            idle = 0
            deadline = asyncio.get_running_loop().time() + settings.stream_max_seconds
            yield "retry: 3000\n\n"
            while asyncio.get_running_loop().time() < deadline:
                if await request.is_disconnected():
                    return
                try:
                    version, latest = await asyncio.to_thread(engine.version, record.id)
                    job = platform.latest_job(record.id)
                    signature = (version, job.id if job else None, job.status if job else None,
                                 job.cancel_requested if job else None,
                                 platform.delivery(record.id) is not None)
                    if latest > cursor:
                        run = await asyncio.to_thread(engine.load, record.id)
                        for event in await asyncio.to_thread(engine.events, record.id, cursor):
                            view = projector.event(run, event)
                            cursor = event.sequence
                            if not view["debug"]:
                                yield f"id: {event.sequence}\nevent: timeline\ndata: {json.dumps(view)}\n\n"
                    if signature != last_signature:
                        last_signature = signature
                        state = await asyncio.to_thread(summary, record)
                        yield f"event: state\ndata: {json.dumps(state)}\n\n"
                        idle = 0
                except Exception:
                    yield "event: error\ndata: {\"detail\": \"Run state is temporarily unavailable.\"}\n\n"
                idle += 1
                if idle % 15 == 0:
                    yield ": keep-alive\n\n"
                await asyncio.sleep(settings.stream_poll_seconds)

        return StreamingResponse(generate(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache, no-transform",
                                          "X-Accel-Buffering": "no"})

    # Decisions and control ----------------------------------------------

    @app.post("/api/runs/{run_id}/approvals/{approval_id}")
    def decide(run_id: str, approval_id: str, body: ApprovalDecisionRequest, user: User):
        act(user)
        record = owned_run(user, run_id)
        approved = body.decision == "approve"
        reason = body.reason.strip() or ("Approved in Cavman after reviewing the exact scope." if approved
                                         else "Rejected in Cavman.")
        try:
            engine.decide_approval(record.id, approval_id, approved=approved,
                                   human_id=f"cavman-user:{user}", reason=reason,
                                   expected_scope_digest=body.scope_digest)
        except KeyError:
            raise HTTPException(404, "Approval not found.") from None
        except ApprovalScopeChanged as exc:
            raise HTTPException(409, str(exc)) from None
        run = engine.load(record.id)
        continued = False
        if run.status == "active" and platform.active_job(record.id) is None:
            _, continued = platform.enqueue(record.id, "continue")
        return {"run": detail(record), "continued": continued}

    @app.post("/api/runs/{run_id}/continue", status_code=202)
    def continue_run(run_id: str, body: ContinueRequest, user: User):
        record = owned_run(user, run_id)
        if engine.load(record.id).status != "active":
            raise HTTPException(409, "This run has finished; start a new build instead.")
        if settings.executor == EXECUTOR_PROVIDER and not _provider_status()["configured"]:
            raise HTTPException(503, "Cavman's model provider is not configured on the server.")
        require_account_allowance(user)
        if platform.active_job(record.id) is None:
            require_capacity(user, new_project=False)
            rate_limit(user, "build", settings.builds_per_hour, 3600, "new builds per hour")
        message = body.message.strip()
        if message and platform.active_job(record.id) is not None:
            raise HTTPException(409, "This run is already executing.")
        job, created = platform.enqueue(record.id, "continue", message)
        if not created:
            raise HTTPException(409, "This run is already executing.")
        if message:
            # Instructions are kept instructions beside the run; every later step reads them.
            platform.add_instruction(record.id, engine.redact(message, limit=4000))
        return {"job": projector.job(job)}

    @app.post("/api/runs/{run_id}/instructions", status_code=201)
    def add_instruction(run_id: str, body: InstructionRequest, user: User):
        """Direction for a build while it runs: specialists and reviewers starting
        work after this see it. Work already accepted is not redone."""
        act(user)
        record = owned_run(user, run_id)
        if engine.load(record.id).status != "active":
            raise HTTPException(409, "This run has finished. Ask for changes in a follow-up instead.")
        if len(platform.instructions(record.id)) >= MAX_INSTRUCTIONS:
            raise HTTPException(429, f"A run takes at most {MAX_INSTRUCTIONS} instructions.")
        item = platform.add_instruction(record.id, engine.redact(body.message.strip(), limit=4000))
        return {"instruction": {key: item[key] for key in ("id", "text", "created_at")}}

    @app.post("/api/runs/{run_id}/stop", status_code=202)
    def stop_run(run_id: str, user: User):
        act(user)
        record = owned_run(user, run_id)
        job = platform.request_cancel(record.id)
        if job is None:
            raise HTTPException(409, "This run is not executing.")
        return {"job": projector.job(job)}

    @app.post("/api/runs/{run_id}/abandon")
    def abandon_run(run_id: str, body: AbandonRequest, user: User):
        act(user)
        record = owned_run(user, run_id)
        if platform.active_job(record.id) is not None:
            raise HTTPException(409, "Stop the run before closing it.")
        engine.abandon(record.id, body.reason.strip(), actor_id=f"cavman-user:{user}")
        return detail(record)

    @app.patch("/api/runs/{run_id}/budget")
    def set_budget(run_id: str, body: BudgetRequest, user: User):
        act(user)
        record = owned_run(user, run_id)
        if body.budget_usd > settings.max_budget_usd:
            raise HTTPException(422, f"The maximum budget per run is ${settings.max_budget_usd:.2f}.")
        platform.set_budget(record.id, round(body.budget_usd, 2))
        return summary(platform.get_run(user, run_id))

    # Delivery -----------------------------------------------------------

    @app.get("/api/runs/{run_id}/delivery")
    def get_delivery(run_id: str, user: User):
        record = owned_run(user, run_id)
        view = delivery_view(record.id)
        if view is None:
            raise HTTPException(404, "No deliverable has been assembled for this run.")
        return view

    @app.post("/api/runs/{run_id}/delivery")
    def assemble_delivery(run_id: str, user: User):
        act(user)
        record = owned_run(user, run_id)
        if engine.load(record.id).status != "completed":
            raise HTTPException(409, "Only a completed run can be delivered.")
        existing = platform.delivery(record.id)
        if existing is not None and existing.status == "ready":
            return delivery_view(record.id)
        holder = f"api:{new_id()}"
        if not platform.acquire_project(record.project_id, holder, 600):
            raise HTTPException(409, "This project is busy with another build. Try again when it finishes.")
        try:
            deliver_run(engine, platform, projector, record.id, retry=True)
        finally:
            platform.release_project(record.project_id, holder)
        return delivery_view(record.id)

    @app.post("/api/runs/{run_id}/publish", status_code=201)
    def publish(run_id: str, body: PublishRequest, user: User):
        act(user)
        from .publish import REPO_NAME, GitHubClient, PublishError, push

        record = owned_run(user, run_id)
        if not REPO_NAME.fullmatch(body.name) or body.name.strip(".") == "":
            raise HTTPException(422, "Repository names use letters, digits, '.', '-' and '_' only.")
        if platform.publication(record.id) is not None:
            raise HTTPException(409, "This run was already published.")
        run = engine.load(record.id)
        delivery = platform.delivery(record.id)
        commit = (delivery.manifest.get("commit") if delivery and delivery.status == "ready" else None)
        if run.status != "completed" or not commit:
            raise HTTPException(409, "Only a completed run with a verified, integrated project can be published.")
        client = GitHubClient(body.github_token, settings.github_api_url)
        try:
            created = client.create_repository(body.name, body.private,
                                               f"{project_name(record.project_id)} - built with Cavman")
            push(engine.project_repo(record.project_id), commit, created["clone_url"], body.github_token)
        except PublishError as exc:
            raise HTTPException(exc.status, engine.redact(str(exc))) from None
        platform.save_publication(record.id, user, created["full_name"], created["html_url"], commit, body.private)
        return {"publication": publication_view(record.id)}

    @app.get("/api/runs/{run_id}/delivery/download")
    def download(run_id: str, user: User):
        record = owned_run(user, run_id)
        delivery = platform.delivery(record.id)
        if delivery is None or delivery.status != "ready" or not delivery.archive_name:
            raise HTTPException(404, "No deliverable is ready for this run.")
        path = settings.deliveries_dir / delivery.archive_name
        if not path.is_file() or path.parent.resolve() != settings.deliveries_dir.resolve():
            raise HTTPException(404, "The deliverable file is missing.")
        filename = f"{delivery.manifest.get('root', 'cavman-project')}.tar.gz"
        return FileResponse(path, media_type="application/gzip", filename=filename)

    return app
