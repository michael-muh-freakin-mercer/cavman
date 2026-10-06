"""Cavman platform settings, resolved from the environment.

Provider credentials are deliberately not read here: the orchestration core's
``RuntimeConfig.from_env`` stays the only place that reads them, and only the
worker process needs them.
"""
from __future__ import annotations

import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .legacy import is_legacy, prefer_existing, with_legacy_names

ORCHESTRATION_WORKFLOW = "workflow"

EXECUTOR_PROVIDER = "provider"
EXECUTOR_SCRIPTED = "scripted"

SANDBOX_BUBBLEWRAP = "bubblewrap"
SANDBOX_E2B = "e2b"

MIN_API_TOKEN_LENGTH = 32


class SettingsError(ValueError):
    """Raised when the Cavman platform configuration is missing or unsafe."""


def _float(values: Mapping[str, str], name: str, default: float, *, minimum: float = 0.0) -> float:
    raw = values.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        raise SettingsError(f"{name} must be a number; got {raw!r}.") from None
    if not math.isfinite(value) or value < minimum:
        raise SettingsError(f"{name} must be a finite number >= {minimum}; got {raw!r}.")
    return value


def _int(values: Mapping[str, str], name: str, default: int, *, minimum: int = 1) -> int:
    raw = values.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        raise SettingsError(f"{name} must be an integer; got {raw!r}.") from None
    if value < minimum:
        raise SettingsError(f"{name} must be >= {minimum}; got {raw!r}.")
    return value


MODEL_MODES = ("budget", "balanced", "quality")


def _profiles(values: Mapping[str, str]) -> tuple[tuple[str, str, str], ...]:
    """CAVMAN_MODELS_<MODE>="manager=<model>,worker=<model>" for budget, balanced, quality."""
    profiles = []
    for mode in MODEL_MODES:
        raw = (values.get(f"CAVMAN_MODELS_{mode.upper()}") or "").strip()
        if not raw:
            continue
        parts = dict(item.split("=", 1) for item in raw.split(",") if "=" in item)
        manager, worker = parts.get("manager", "").strip(), parts.get("worker", "").strip()
        if not manager or not worker:
            raise SettingsError(f"CAVMAN_MODELS_{mode.upper()} must be 'manager=<model>,worker=<model>'.")
        profiles.append((mode, manager, worker))
    return tuple(profiles)


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    api_token: str
    environment: str = "development"
    executor: str = EXECUTOR_PROVIDER
    # Spending is bounded by default: every run gets a USD ceiling (enforced on
    # provider-reported cost) and a model-call ceiling (which also bounds calls
    # whose cost the provider did not report).
    default_budget_usd: float = 5.0
    max_budget_usd: float = 100.0
    default_max_model_calls: int = 300
    # Per-account monthly ceilings across all runs.
    account_monthly_budget_usd: float = 25.0
    account_monthly_max_calls: int = 3000
    budget_warning_ratio: float = 0.8
    # Steps (model replies) per specialist attempt. Live runs showed capable
    # specialists reaching green checks around step 24.
    specialist_max_turns: int = 40
    worker_concurrency: int = 1
    lease_seconds: float = 90.0
    heartbeat_seconds: float = 5.0
    max_recoveries: int = 3
    scripted_step_delay: float = 0.25
    github_api_url: str = "https://api.github.com"
    # postgres://... puts operational and platform state in PostgreSQL (schemas
    # <database_schema>_ops and <database_schema>_platform) so API and workers
    # can run on several hosts. Unset: SQLite files in data_dir.
    database_url: str | None = None
    database_schema: str = "cavman"
    # Per-account abuse limits. Rates are per user across all API hosts.
    builds_per_hour: int = 20          # new builds and continuations
    imports_per_hour: int = 5          # repository imports
    actions_per_minute: int = 60       # approvals, stop, close, budget, delivery, publish
    max_concurrent_builds: int = 2     # runs with queued or running work
    max_projects: int = 100
    account_disk_mb: int = 2048        # project repositories plus delivery archives
    # One worker per interval retires finished runs' candidate worktrees and
    # prunes dependency caches unused for node_deps_max_age_days.
    maintenance_interval_seconds: float = 3600.0
    node_deps_max_age_days: float = 7.0
    # Optional token for GitHub API and clone requests during imports; any
    # token (no scopes needed) raises GitHub's anonymous 60-per-hour limit.
    github_import_token: str | None = None
    # Starting a project from a public GitHub repository (0 disables imports).
    import_max_mb: int = 100
    import_max_files: int = 5000
    # Optional routing profiles: mode -> (manager/planner model, specialist model).
    # "automatic" always exists and uses WALTER_MODEL / WALTER_WORKER_MODEL.
    model_profiles: tuple[tuple[str, str, str], ...] = ()
    # Prometheus scrape token; the metrics endpoint is disabled when unset.
    metrics_token: str | None = None
    # Event streams end after this long; EventSource reconnects with Last-Event-ID.
    stream_max_seconds: float = 300.0
    stream_poll_seconds: float = 1.0
    # Where candidate code runs: bubblewrap (namespaces on the worker host) or
    # e2b (a fresh E2B microVM per check; the worker needs E2B_API_KEY).
    sandbox_backend: str = SANDBOX_BUBBLEWRAP
    e2b_template: str = "cavman-sandbox"

    @property
    def operations_db(self) -> Path:
        return prefer_existing(self.data_dir / "cavman-operations.db", self.data_dir / "caveman-operations.db")

    @property
    def platform_db(self) -> Path:
        return prefer_existing(self.data_dir / "cavman-platform.db", self.data_dir / "caveman-platform.db")

    @property
    def sessions_db(self) -> Path:
        return prefer_existing(self.data_dir / "cavman-sessions.db", self.data_dir / "caveman-sessions.db")

    def open_operations_store(self):
        """The orchestration kernel's run store (snapshots and events)."""
        from walter.store import open_store
        return open_store(self.database_url or self.operations_db, schema=f"{self.database_schema}_ops")

    def open_platform_store(self):
        """Ownership, jobs, deliveries and publications."""
        from .platform_store import PlatformStore
        return PlatformStore(self.database_url or self.platform_db, schema=f"{self.database_schema}_platform")

    def execution_backend(self):
        """The isolation backend for candidate code; None means WorkspaceManager's Bubblewrap default."""
        if self.sandbox_backend == SANDBOX_E2B:
            from walter.sandbox_e2b import E2BBackend
            return E2BBackend(template=self.e2b_template)
        return None

    @property
    def projects_dir(self) -> Path:
        return self.data_dir / "projects"

    @property
    def deliveries_dir(self) -> Path:
        return self.data_dir / "deliveries"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        raw = os.environ if env is None else env
        values = with_legacy_names(raw)
        # An install still configured with CAVEMAN_* names predates the rename:
        # keep its database schema and E2B template unless it names new ones.
        legacy = is_legacy(raw)
        token = values.get("CAVMAN_API_TOKEN", "").strip()
        if len(token) < MIN_API_TOKEN_LENGTH:
            raise SettingsError(
                "CAVMAN_API_TOKEN must be set to a random secret of at least "
                f"{MIN_API_TOKEN_LENGTH} characters (for example `openssl rand -hex 32`). "
                "The web server presents it on every API call; browsers never see it.")
        environment = values.get("CAVMAN_ENV", "development").strip().lower() or "development"
        executor = values.get("CAVMAN_EXECUTOR", EXECUTOR_PROVIDER).strip().lower()
        if executor not in {EXECUTOR_PROVIDER, EXECUTOR_SCRIPTED}:
            raise SettingsError(
                f"CAVMAN_EXECUTOR must be '{EXECUTOR_PROVIDER}' or '{EXECUTOR_SCRIPTED}'; got {executor!r}.")
        if executor == EXECUTOR_SCRIPTED and environment == "production":
            raise SettingsError(
                "The scripted test executor drives runs with scripted models and is refused "
                "when CAVMAN_ENV=production.")
        orchestration = values.get("CAVMAN_ORCHESTRATION", ORCHESTRATION_WORKFLOW).strip().lower()
        if orchestration != ORCHESTRATION_WORKFLOW:
            # Manager mode (a Manager model driving every step) was retired on 2026-10-02.
            raise SettingsError(
                f"CAVMAN_ORCHESTRATION={orchestration!r} is no longer supported: manager mode was retired "
                f"and every build runs the workflow driver. Remove the setting or set it to "
                f"'{ORCHESTRATION_WORKFLOW}'.")
        sandbox_backend = (values.get("CAVMAN_SANDBOX_BACKEND") or SANDBOX_BUBBLEWRAP).strip().lower()
        if sandbox_backend not in {SANDBOX_BUBBLEWRAP, SANDBOX_E2B}:
            raise SettingsError(
                f"CAVMAN_SANDBOX_BACKEND must be '{SANDBOX_BUBBLEWRAP}' or '{SANDBOX_E2B}'; got {sandbox_backend!r}.")
        e2b_template = (values.get("CAVMAN_E2B_TEMPLATE") or ("caveman-sandbox" if legacy else "cavman-sandbox")).strip()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_.:/-]{0,127}", e2b_template):
            raise SettingsError("CAVMAN_E2B_TEMPLATE must be an E2B template name such as cavman-sandbox.")
        data_dir = Path(values.get("CAVMAN_DATA_DIR")
                        or prefer_existing(Path(".local/cavman"), Path(".local/caveman"))).expanduser().resolve()
        database_url = (values.get("CAVMAN_DATABASE_URL") or "").strip() or None
        if database_url and not database_url.startswith(("postgres://", "postgresql://")):
            raise SettingsError("CAVMAN_DATABASE_URL must be a postgres:// URL; leave it unset for SQLite files.")
        database_schema = (values.get("CAVMAN_DATABASE_SCHEMA") or ("caveman" if legacy else "cavman")).strip()
        if not re.fullmatch(r"[a-z_][a-z0-9_]{0,40}", database_schema):
            raise SettingsError("CAVMAN_DATABASE_SCHEMA must be lowercase letters, digits and underscores.")
        default_budget = _float(values, "CAVMAN_DEFAULT_BUDGET_USD", 5.0, minimum=0.01)
        max_budget = _float(values, "CAVMAN_MAX_BUDGET_USD", 100.0, minimum=0.01)
        if default_budget > max_budget:
            raise SettingsError("CAVMAN_DEFAULT_BUDGET_USD cannot exceed CAVMAN_MAX_BUDGET_USD.")
        return cls(
            data_dir=data_dir,
            api_token=token,
            environment=environment,
            executor=executor,
            model_profiles=_profiles(values),
            database_url=database_url,
            database_schema=database_schema,
            github_api_url=(values.get("CAVMAN_GITHUB_API_URL") or "https://api.github.com").strip(),
            metrics_token=(values.get("CAVMAN_METRICS_TOKEN") or "").strip() or None,
            import_max_mb=_int(values, "CAVMAN_IMPORT_MAX_MB", 100, minimum=0),
            github_import_token=(values.get("CAVMAN_GITHUB_IMPORT_TOKEN") or "").strip() or None,
            builds_per_hour=_int(values, "CAVMAN_BUILDS_PER_HOUR", 20),
            imports_per_hour=_int(values, "CAVMAN_IMPORTS_PER_HOUR", 5),
            actions_per_minute=_int(values, "CAVMAN_ACTIONS_PER_MINUTE", 60),
            max_concurrent_builds=_int(values, "CAVMAN_MAX_CONCURRENT_BUILDS", 2),
            max_projects=_int(values, "CAVMAN_MAX_PROJECTS", 100),
            account_disk_mb=_int(values, "CAVMAN_ACCOUNT_DISK_MB", 2048),
            import_max_files=_int(values, "CAVMAN_IMPORT_MAX_FILES", 5000),
            default_budget_usd=default_budget,
            max_budget_usd=max_budget,
            default_max_model_calls=_int(values, "CAVMAN_DEFAULT_MAX_MODEL_CALLS", 300),
            account_monthly_budget_usd=_float(values, "CAVMAN_ACCOUNT_MONTHLY_BUDGET_USD", 25.0, minimum=0.01),
            account_monthly_max_calls=_int(values, "CAVMAN_ACCOUNT_MONTHLY_MAX_CALLS", 3000),
            specialist_max_turns=_int(values, "CAVMAN_SPECIALIST_MAX_TURNS", 40),
            worker_concurrency=_int(values, "CAVMAN_WORKER_CONCURRENCY", 1),
            lease_seconds=_float(values, "CAVMAN_LEASE_SECONDS", 90.0, minimum=5.0),
            heartbeat_seconds=_float(values, "CAVMAN_HEARTBEAT_SECONDS", 5.0, minimum=0.1),
            max_recoveries=_int(values, "CAVMAN_MAX_RECOVERIES", 3, minimum=0),
            maintenance_interval_seconds=_float(values, "CAVMAN_MAINTENANCE_INTERVAL_SECONDS", 3600.0, minimum=60.0),
            node_deps_max_age_days=_float(values, "CAVMAN_NODE_DEPS_MAX_AGE_DAYS", 7.0, minimum=0.0),
            scripted_step_delay=_float(values, "CAVMAN_SCRIPTED_STEP_DELAY", 0.25),
            sandbox_backend=sandbox_backend,
            e2b_template=e2b_template,
        )

    def ensure_directories(self) -> None:
        for directory in (self.data_dir, self.projects_dir, self.deliveries_dir):
            directory.mkdir(parents=True, exist_ok=True)
        # Platform state holds ownership and job records; keep it private.
        os.chmod(self.data_dir, 0o700)
