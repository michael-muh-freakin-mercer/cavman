"""Account data export and erasure.

Erasure removes the platform's ownership records first, in one transaction that
refuses while a build is running, and then everything those records pointed to:
the kernel's run snapshots and events, conversation sessions, project
repositories (with candidate worktrees) and delivery archives. If the process
stops part-way, ``cavman ops purge-orphans`` removes what is left, because
nothing can reach it any more.
"""
from __future__ import annotations

import logging
import re
import shutil
import sqlite3
import time
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("cavman.erasure")

_HEX_ID = re.compile(r"^[0-9a-f]{32}$")


def _purge_sessions(sessions_db: Path, run_ids: list[str]) -> None:
    # Only runs from the retired manager mode have conversation sessions; erase them too.
    if not run_ids or not sessions_db.exists():
        return
    connection = sqlite3.connect(str(sessions_db))
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        with connection:
            for table in ("agent_messages", "agent_sessions"):
                if table in tables:
                    connection.executemany(f"DELETE FROM {table} WHERE session_id=?", [(r,) for r in run_ids])
    finally:
        connection.close()


def _purge(settings, engine, run_ids: list[str], project_ids: list[str]) -> None:
    with engine.core() as core:
        for run_id in run_ids:
            core.store.delete_run(run_id)
    _purge_sessions(settings.sessions_db, run_ids)
    for run_id in run_ids:
        for name in (f"{run_id}.tar.gz", f"{run_id}.tar.gz.tmp"):
            (settings.deliveries_dir / name).unlink(missing_ok=True)
    for project_id in project_ids:
        if _HEX_ID.fullmatch(project_id):
            shutil.rmtree(settings.projects_dir / project_id, ignore_errors=True)


def erase_account(settings, engine, platform, owner_id: str) -> dict:
    """Delete everything Cavman holds for ``owner_id``. Raises ActiveWork while a build runs."""
    owned = platform.delete_owner(owner_id)
    try:
        _purge(settings, engine, owned["runs"], owned["projects"])
    except Exception:
        logger.exception("Account erasure left data behind; run `cavman ops purge-orphans`",
                         extra={"runs": len(owned["runs"]), "projects": len(owned["projects"])})
        raise
    return {"runs": len(owned["runs"]), "projects": len(owned["projects"])}


def purge_orphans(settings, engine, platform, *, min_age_seconds: float = 3600) -> dict:
    """Remove kernel runs, sessions, repositories and archives no platform record owns.

    Items younger than ``min_age_seconds`` are kept: a build being created has
    its repository and kernel run for a moment before its platform records.
    """
    cutoff = time.time() - min_age_seconds
    runs, projects = platform.known_ids()
    with engine.core() as core:
        orphan_runs = [run_id for run_id in core.store.run_ids()
                       if run_id not in runs and datetime.fromisoformat(core.store.load(run_id).created_at).timestamp() < cutoff]
    orphan_projects = [path.name for path in settings.projects_dir.glob("*")
                       if path.is_dir() and _HEX_ID.fullmatch(path.name) and path.name not in projects
                       and path.stat().st_mtime < cutoff]
    orphan_archives = [path for path in settings.deliveries_dir.glob("*.tar.gz*")
                       if path.name.split(".", 1)[0] not in runs and path.stat().st_mtime < cutoff]
    _purge(settings, engine, orphan_runs, orphan_projects)
    for path in orphan_archives:
        path.unlink(missing_ok=True)
    return {"runs": len(orphan_runs), "projects": len(orphan_projects), "archives": len(orphan_archives)}
