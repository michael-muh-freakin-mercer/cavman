"""Against the real github.com: opt in with CAVMAN_LIVE_GITHUB=1 (network, no credentials, no spend).

Every other importer test uses a local stand-in for GitHub. These prove the
same code path works against GitHub's real API and git hosting.
"""
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("CAVMAN_LIVE_GITHUB") != "1",
                                reason="set CAVMAN_LIVE_GITHUB=1 to run against github.com")

# GitHub's own long-lived example repository: one README, public, tiny.
REPOSITORY = "https://github.com/octocat/Hello-World"


def test_a_real_public_repository_imports_through_the_api_and_builds(tmp_path):
    pytest.importorskip("agents")
    from fastapi.testclient import TestClient

    from cavman.api import create_app
    from cavman.config import Settings

    token = "t" * 40
    headers = {"Authorization": f"Bearer {token}", "X-Cavman-User": "alice"}
    settings = Settings(data_dir=tmp_path / "data", api_token=token, executor="scripted",
                        github_import_token=os.environ.get("CAVMAN_GITHUB_IMPORT_TOKEN") or None)
    with TestClient(create_app(settings)) as client:
        response = client.post("/api/builds", headers=headers, json={
            "prompt": "Add a short greeting module", "settings": {"repository_url": REPOSITORY}})
        assert response.status_code == 201, response.text
        project = client.get(f"/api/projects/{response.json()['project_id']}", headers=headers).json()
        source = project["settings"]["source"]
        assert source["url"] == REPOSITORY and len(source["commit"]) == 40 and source["files"] >= 1
        repo = settings.projects_dir / project["id"] / "repo"
        assert (repo / "README").exists()
        assert not (repo / ".git" / "shallow").exists()
        import subprocess
        remotes = subprocess.run(["git", "-C", str(repo), "remote"], capture_output=True, text=True).stdout
        assert remotes.strip() == ""  # the upstream remote is gone
        log = subprocess.run(["git", "-C", str(repo), "log", "--format=%s"], capture_output=True, text=True).stdout
        assert log.splitlines() == [f"Import octocat/Hello-World into {project['name']}"]


def test_a_private_or_missing_repository_is_refused_by_the_real_api(tmp_path):
    from cavman import importer

    with pytest.raises(importer.RepositoryImportError, match="not found, or it is private"):
        importer.import_repository("https://github.com/octocat/this-repository-does-not-exist-0", tmp_path / "r",
                                   "x", api_url="https://api.github.com", max_mb=50, max_files=100)
    assert not (tmp_path / "r").exists()
