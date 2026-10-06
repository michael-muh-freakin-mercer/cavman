"""Starting a project from a public GitHub repository, and planning on existing files.

The network is never used: GitHub metadata is stubbed and the clone comes from a
local upstream repository over file://, which the importer otherwise refuses.
"""
import json
import subprocess

import pytest

from cavman import importer
from walter.sandbox import INTEGRATION_REF, WorkspaceManager


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


PUBLIC = {"private": False, "visibility": "public", "size": 12, "default_branch": "trunk"}


@pytest.fixture
def upstream(tmp_path):
    repo = tmp_path / "upstream"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "app.py").write_text("def hello():\n    return 'hi'\n")
    (repo / "README.md").write_text("# Upstream\n")
    (repo / ".gitignore").write_text("build/\n")
    (repo / ".env").write_text("SECRET=do-not-import\n")
    (repo / "deploy.pem").write_text("-----BEGIN KEY-----\n")
    git(tmp_path, "init", "-q", "-b", "trunk", str(repo))
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=u", "-c", "user.email=u@u", "commit", "-qm", "first")
    (repo / "src" / "more.py").write_text("X = 1\n")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=u", "-c", "user.email=u@u", "commit", "-qm", "second")
    return repo


@pytest.fixture
def local_github(monkeypatch, upstream):
    """Point the importer at the local upstream, keeping every other rule."""
    metadata = dict(PUBLIC)
    monkeypatch.setattr(importer, "repository_metadata", lambda source, api_url, token=None, **kwargs: metadata)
    monkeypatch.setattr(importer, "ALLOWED_PROTOCOLS", "file")
    monkeypatch.setattr(importer.Source, "clone_url", property(lambda self: f"file://{upstream}"))
    return metadata


def run_import(tmp_path, **limits):
    return importer.import_repository(
        "https://github.com/octo/demo", tmp_path / "project" / "repo", "Demo",
        api_url="https://api.github.invalid", max_mb=limits.get("max_mb", 50),
        max_files=limits.get("max_files", 100))


@pytest.mark.parametrize("url", [
    "http://github.com/octo/demo", "git@github.com:octo/demo.git", "ssh://github.com/octo/demo",
    "https://github.com/octo", "https://github.com/octo/demo/tree/main", "https://github.com.evil.test/octo/demo",
    "https://user:pass@github.com/octo/demo", "https://github.com/octo/demo?x=1", "https://gitlab.com/octo/demo",
    "file:///etc", "https://github.com/-octo/demo", "https://github.com/octo/..",
])
def test_only_plain_public_github_urls_are_accepted(url):
    with pytest.raises(importer.RepositoryImportError):
        importer.parse_url(url)


def test_accepted_url_forms():
    assert importer.parse_url("https://github.com/octo/demo").full_name == "octo/demo"
    assert importer.parse_url("https://github.com/octo/demo.git").full_name == "octo/demo"
    assert importer.parse_url(" https://github.com/Octo-Cat/my.repo_1/ ").full_name == "Octo-Cat/my.repo_1"


def test_import_keeps_files_but_not_history_remote_or_secrets(tmp_path, local_github, upstream):
    imported = run_import(tmp_path)
    repo = tmp_path / "project" / "repo"
    assert imported.commit == git(upstream, "rev-parse", "HEAD") and imported.branch == "trunk"
    assert imported.files == 4 and set(imported.dropped) == {".env", "deploy.pem"}
    # One local commit on main, no parents, no remote, no shallow state.
    assert git(repo, "branch", "--show-current") == "main"
    assert git(repo, "rev-list", "--count", "HEAD") == "1"
    assert git(repo, "remote") == "" and not (repo / ".git" / "shallow").exists()
    assert "octo/demo" in git(repo, "log", "-1", "--format=%B")
    files = set(git(repo, "ls-tree", "-r", "--name-only", "HEAD").splitlines())
    assert files == {".gitignore", "README.md", "src/app.py", "src/more.py"}
    assert not (repo / ".env").exists() and (repo / "src" / "more.py").read_text() == "X = 1\n"
    # Cavman state is ignored locally without changing the project's own .gitignore.
    assert ".local/" in (repo / ".git" / "info" / "exclude").read_text()
    assert (repo / ".gitignore").read_text() == "build/\n"
    assert git(repo, "status", "--porcelain") == ""
    # The imported repository works as a Cavman project: integration starts from it.
    workspaces = WorkspaceManager(repo)
    assert workspaces.tracked_files() == sorted(files)
    head = workspaces.integration_head(create=True)
    assert git(repo, "rev-parse", INTEGRATION_REF) == head


@pytest.mark.parametrize("kind", ["symlink", "submodule"])
def test_links_and_submodules_are_refused_and_nothing_is_left_behind(tmp_path, local_github, upstream, kind):
    if kind == "submodule":
        commit = git(upstream, "rev-parse", "HEAD")
        git(upstream, "update-index", "--add", "--cacheinfo", f"160000,{commit},vendor/lib")
    else:
        (upstream / "escape").symlink_to("/etc/passwd")
        git(upstream, "add", "-A")
    git(upstream, "-c", "user.name=u", "-c", "user.email=u@u", "commit", "-qm", kind)
    with pytest.raises(importer.RepositoryImportError, match=kind.replace("symlink", "symbolic link")):
        run_import(tmp_path)
    assert not (tmp_path / "project" / "repo").exists()


def test_private_oversized_and_disabled_imports_are_refused(tmp_path, local_github):
    local_github["private"] = True
    with pytest.raises(importer.RepositoryImportError, match="private"):
        run_import(tmp_path)
    local_github.update(private=False, size=10 * 1024)
    with pytest.raises(importer.RepositoryImportError, match="larger than the 1 MB"):
        run_import(tmp_path, max_mb=1)
    local_github["size"] = 1
    with pytest.raises(importer.RepositoryImportError, match="more than 2 files"):
        run_import(tmp_path, max_files=2)
    with pytest.raises(importer.RepositoryImportError, match="disabled"):
        run_import(tmp_path, max_mb=0)
    assert not (tmp_path / "project" / "repo").exists()


def test_git_refuses_non_https_transport_by_default(tmp_path, monkeypatch, upstream):
    monkeypatch.setattr(importer, "repository_metadata", lambda source, api_url, token=None, **kwargs: dict(PUBLIC))
    monkeypatch.setattr(importer.Source, "clone_url", property(lambda self: f"file://{upstream}"))
    with pytest.raises(importer.RepositoryImportError, match="Git could not import"):
        run_import(tmp_path)
    assert not (tmp_path / "project" / "repo").exists()


def test_tracked_files_prefers_the_integration_head_and_hides_state(tmp_path):
    repo = tmp_path / "p"
    repo.mkdir()
    (repo / "a.py").write_text("A = 1\n")
    (repo / ".env").write_text("X=1\n")
    git(repo, "init", "-q")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    workspaces = WorkspaceManager(repo)
    assert workspaces.tracked_files() == ["a.py"]
    head = workspaces.integration_head(create=True)
    grant = workspaces.create_candidate("run", "t", "w", base_revision=head)
    workspaces.write_file(grant.id, "b.py", "B = 1\n", worker_id="w")
    workspaces.integrate(grant.id, workspaces.freeze(grant.id), "Add b")
    assert workspaces.tracked_files() == ["a.py", "b.py"]
    assert workspaces.tracked_files("HEAD") == ["a.py"]


def test_api_starts_projects_and_builds_from_an_imported_repository(tmp_path, local_github):
    pytest.importorskip("agents")
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from cavman.api import create_app
    from cavman.config import Settings

    token = "t" * 40
    headers = {"Authorization": f"Bearer {token}", "X-Cavman-User": "alice"}
    settings = Settings(data_dir=tmp_path / "data", api_token=token, executor="scripted")
    with TestClient(create_app(settings)) as client:
        response = client.post("/api/projects", headers=headers, json={
            "name": "Demo", "repository_url": "https://github.com/octo/demo"})
        assert response.status_code == 201, response.text
        project = response.json()
        assert project["settings"]["source"]["url"] == "https://github.com/octo/demo"
        assert project["settings"]["source"]["files"] == 4
        repo = settings.projects_dir / project["id"] / "repo"
        assert (repo / "src" / "app.py").exists()

        response = client.post("/api/builds", headers=headers, json={
            "prompt": "Add a greeting endpoint", "settings": {"repository_url": "https://github.com/octo/demo"}})
        assert response.status_code == 201, response.text
        assert client.get(f"/api/projects/{response.json()['project_id']}",
                          headers=headers).json()["settings"]["source"]["branch"] == "trunk"

        # An existing project keeps its files; imports only start new projects.
        response = client.post("/api/builds", headers=headers, json={
            "prompt": "Add a greeting endpoint", "project_id": project["id"],
            "settings": {"repository_url": "https://github.com/octo/demo"}})
        assert response.status_code == 422

        before = sorted(p.name for p in settings.projects_dir.iterdir())
        response = client.post("/api/projects", headers=headers, json={
            "name": "Bad", "repository_url": "https://example.com/octo/demo"})
        assert response.status_code == 422 and "github.com" in response.json()["detail"]
        local_github["private"] = True
        response = client.post("/api/builds", headers=headers, json={
            "prompt": "Add a greeting endpoint", "settings": {"repository_url": "https://github.com/octo/demo"}})
        assert response.status_code == 422 and "private" in response.json()["detail"] and response.json()["needs_scope"] == "repo"
        assert sorted(p.name for p in settings.projects_dir.iterdir()) == before
        assert len(client.get("/api/projects", headers=headers).json()["projects"]) == 2


def test_import_token_rides_only_in_the_clone_environment(tmp_path, local_github, monkeypatch):
    seen = {}
    original = importer._environment

    def spy(token=None):
        environment = original(token)
        if token:
            seen.update(environment)
        return environment
    monkeypatch.setattr(importer, "_environment", spy)
    importer.import_repository("https://github.com/octo/demo", tmp_path / "project" / "repo", "Demo",
                               api_url="https://api.github.invalid", max_mb=50, max_files=100,
                               token="ghp_" + "t" * 36)
    assert seen["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraHeader"
    assert "ghp_" not in seen["GIT_CONFIG_VALUE_0"]  # base64 credential, not the raw token
    repo = tmp_path / "project" / "repo"
    assert "ghp_" not in (repo / ".git" / "config").read_text()
    assert "extraheader" not in (repo / ".git" / "config").read_text().lower()


def test_a_private_repository_needs_the_users_own_token(tmp_path, local_github, monkeypatch):
    local_github["private"] = True
    local_github["visibility"] = "private"
    # Without a user token, not even the operator's import token may read it.
    with pytest.raises(importer.RepositoryImportError) as refused:
        importer.import_repository("https://github.com/octo/demo", tmp_path / "a" / "repo", "Demo",
                                   api_url="https://api.github.invalid", max_mb=50, max_files=100,
                                   token="ghp_" + "o" * 36)
    assert refused.value.needs_scope == "repo" and "private" in str(refused.value)
    assert not (tmp_path / "a").exists() or not (tmp_path / "a" / "repo").exists()

    used = []
    original = importer._environment

    def spy(token=None):
        if token:
            used.append(token)
        return original(token)
    monkeypatch.setattr(importer, "_environment", spy)
    user_token = "gho_" + "u" * 36
    imported = importer.import_repository("https://github.com/octo/demo", tmp_path / "b" / "repo", "Demo",
                                          api_url="https://api.github.invalid", max_mb=50, max_files=100,
                                          token="ghp_" + "o" * 36, user_token=user_token)
    assert imported.private is True and imported.files == 4
    assert set(used) == {user_token}  # the user's token, for the download only
    assert user_token not in (tmp_path / "b" / "repo" / ".git" / "config").read_text()


def test_a_missing_repository_without_a_user_token_offers_to_connect_github(monkeypatch):
    import urllib.error

    def not_found(*args, **kwargs):
        raise urllib.error.HTTPError("https://api.github.invalid", 404, "Not Found", {}, None)

    monkeypatch.setattr(importer.urllib.request, "urlopen", not_found)
    source = importer.parse_url("https://github.com/octo/secret")
    with pytest.raises(importer.RepositoryImportError) as anonymous:
        importer.repository_metadata(source, "https://api.github.invalid")
    assert anonymous.value.needs_scope == "repo"
    with pytest.raises(importer.RepositoryImportError) as with_user:
        importer.repository_metadata(source, "https://api.github.invalid", user_token="gho_" + "u" * 36)
    assert with_user.value.needs_scope is None and "cannot read it" in str(with_user.value)


def test_api_imports_a_private_repository_only_with_the_users_token(tmp_path, local_github):
    pytest.importorskip("agents")
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from cavman.api import create_app
    from cavman.config import Settings

    local_github["private"] = True
    token = "t" * 40
    headers = {"Authorization": f"Bearer {token}", "X-Cavman-User": "alice"}
    settings = Settings(data_dir=tmp_path / "data", api_token=token, executor="scripted")
    with TestClient(create_app(settings)) as client:
        body = {"prompt": "Add a greeting endpoint", "settings": {"repository_url": "https://github.com/octo/demo"}}
        refused = client.post("/api/builds", headers=headers, json=body)
        assert refused.status_code == 422 and refused.json()["needs_scope"] == "repo"
        allowed = client.post("/api/builds", headers={**headers, "X-Cavman-GitHub-Token": "gho_" + "u" * 36},
                              json=body)
        assert allowed.status_code == 201, allowed.text
        project = client.get(f"/api/projects/{allowed.json()['project_id']}", headers=headers).json()
        assert project["settings"]["source"]["private"] is True
        assert "gho_" not in json.dumps(project)
