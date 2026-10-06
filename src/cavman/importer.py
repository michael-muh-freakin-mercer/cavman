"""Start a project from an existing GitHub repository: public, or private with the user's own token.

The repository is untrusted input. Trusted platform code (never a model or a
sandboxed specialist) fetches it once, over HTTPS only, from github.com only:

- the URL must name exactly ``https://github.com/<owner>/<repo>``;
- GitHub's metadata must say the repository is public and within the size limit
  before anything is downloaded. A private repository is imported only with the
  importing user's own GitHub token (never the operator's), which proves the
  user can read it; the token is used for this download only and never stored;
- the clone is shallow, single-branch, tagless, without hooks, templates,
  submodules or credentials, and is checked out only after its tree is inspected;
- symlinks and submodules are refused, and files the sandbox treats as state or
  secrets (``.env``, keys, ``.git``-like directories) are dropped;
- the upstream history and remote are discarded: the project starts from one
  local commit whose tree is exactly the kept files, recording where they came from.
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path

from walter.sandbox import _excluded

from .engine import PROJECT_GITIGNORE, _GIT_ENV

GITHUB_URL = re.compile(
    r"^https://github\.com/(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/"
    r"(?P<repo>[A-Za-z0-9._-]{1,100}?)(?:\.git)?/?$")
# Only HTTPS may be used by git for the import (no file://, ssh, ext:: or git://).
ALLOWED_PROTOCOLS = "https"
CLONE_TIMEOUT_SECONDS = 120
_PASSTHROUGH = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "NO_PROXY", "no_proxy",
                "SSL_CERT_FILE", "GIT_SSL_CAINFO")


class RepositoryImportError(ValueError):
    """The repository cannot be imported; the message is safe to show the user."""

    def __init__(self, message: str, status: int = 422, needs_scope: str | None = None):
        super().__init__(message)
        self.status = status
        # Set when connecting GitHub with this scope would let the import proceed.
        self.needs_scope = needs_scope


@dataclass(frozen=True)
class Source:
    owner: str
    repo: str

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repo}"

    @property
    def clone_url(self) -> str:
        return f"https://github.com/{self.owner}/{self.repo}.git"


@dataclass(frozen=True)
class Imported:
    url: str
    commit: str
    branch: str
    files: int
    dropped: tuple[str, ...]
    private: bool = False


def parse_url(url: str) -> Source:
    match = GITHUB_URL.fullmatch(url.strip())
    if not match or match["repo"] in {".", ".."}:
        raise RepositoryImportError("Use a public GitHub repository address like https://github.com/owner/repo.")
    return Source(match["owner"], match["repo"])


def repository_metadata(source: Source, api_url: str, token: str | None = None, *,
                        user_token: str | None = None) -> dict:
    """Metadata from GitHub's API. An operator token only raises rate limits; a
    user's token also shows the private repositories that user can read."""
    token = user_token or token
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "cavman-importer",
               "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(f"{api_url.rstrip('/')}/repos/{source.owner}/{source.repo}", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            if user_token:
                raise RepositoryImportError("That repository was not found, or your GitHub account cannot "
                                            "read it.", 404) from None
            raise RepositoryImportError("That repository was not found, or it is private. To import a private "
                                        "repository, give Cavman access to your GitHub repositories.", 404,
                                        needs_scope="repo") from None
        if exc.code == 429 or (exc.code == 403 and exc.headers.get("X-RateLimit-Remaining") == "0"):
            raise RepositoryImportError("GitHub is rate limiting requests right now. Try again in a few minutes.", 503) from None
        raise RepositoryImportError(f"GitHub returned an error ({exc.code}).", 502) from None
    except (urllib.error.URLError, TimeoutError, ValueError):
        raise RepositoryImportError("GitHub could not be reached.", 502) from None


def _environment(token: str | None = None) -> dict[str, str]:
    environment = {**_GIT_ENV, "GIT_ALLOW_PROTOCOL": ALLOWED_PROTOCOLS, "GIT_LFS_SKIP_SMUDGE": "1"}
    environment.update({name: os.environ[name] for name in _PASSTHROUGH if os.environ.get(name)})
    if token:
        # Through environment config, never argv or the repository's config.
        credential = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        environment.update({"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.https://github.com/.extraHeader",
                            "GIT_CONFIG_VALUE_0": f"Authorization: Basic {credential}"})
    return environment


def _git(*args: str, cwd: Path | None = None, timeout: float = 60, token: str | None = None) -> str:
    try:
        result = subprocess.run(
            ["/usr/bin/git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
             "-c", "core.symlinks=false", "-c", "submodule.recurse=false",
             "-c", "user.name=Cavman", "-c", "user.email=cavman@localhost", *args],
            cwd=cwd, env=_environment(token), capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RepositoryImportError("Downloading the repository took too long.", 504) from None
    if result.returncode:
        message = result.stderr.strip()[-300:] or args[0]
        if token:
            message = message.replace(token, "[redacted]")
        raise RepositoryImportError("Git could not import the repository: " + message, 502)
    return result.stdout


def _tree_size(path: Path) -> int:
    total = 0
    for root, _, names in os.walk(path):
        for name in names:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def import_repository(url: str, repo: Path, name: str, *, api_url: str, max_mb: int, max_files: int,
                      clone_url: str | None = None, token: str | None = None,
                      user_token: str | None = None) -> Imported:
    """Create ``repo`` as a new project repository holding the kept files of ``url``.

    ``repo`` must not exist. On any failure it is removed again.
    """
    if max_mb <= 0:
        raise RepositoryImportError("Starting from an existing repository is disabled on this server.", 403)
    source = parse_url(url)
    metadata = repository_metadata(source, api_url, token, user_token=user_token)
    private = bool(metadata.get("private")) or metadata.get("visibility", "public") != "public"
    if private and not user_token:
        raise RepositoryImportError("That repository is private. To import it, give Cavman access to your "
                                    "GitHub repositories.", needs_scope="repo")
    size_kb = metadata.get("size")
    if isinstance(size_kb, int) and size_kb > max_mb * 1024:
        raise RepositoryImportError(f"That repository is larger than the {max_mb} MB import limit.")
    branch = str(metadata.get("default_branch") or "main")
    if not re.fullmatch(r"[A-Za-z0-9._/-]{1,200}", branch) or ".." in branch or branch.startswith("-"):
        raise RepositoryImportError("The repository's default branch name is not supported.")
    repo.parent.mkdir(parents=True, exist_ok=True)
    if repo.exists():
        raise RepositoryImportError("The project repository already exists.", 409)
    try:
        imported = _clone(source, repo, name, branch, clone_url or source.clone_url, max_mb, max_files,
                          user_token or token)
        return replace(imported, private=private)
    except BaseException:
        shutil.rmtree(repo, ignore_errors=True)
        raise


def _clone(source: Source, repo: Path, name: str, branch: str, clone_url: str,
           max_mb: int, max_files: int, token: str | None = None) -> Imported:
    # The token is used for the download only; no later command (and nothing
    # written into the repository) carries it.
    _git("clone", "--quiet", "--no-checkout", "--depth=1", "--single-branch", "--no-tags",
         "--template=", "--branch", branch, "--", clone_url, str(repo), timeout=CLONE_TIMEOUT_SECONDS,
         token=token)
    if _tree_size(repo / ".git") > max_mb * 1024 * 1024:
        raise RepositoryImportError(f"That repository is larger than the {max_mb} MB import limit.")
    upstream = _git("rev-parse", "HEAD", cwd=repo).strip()
    entries = _git("-c", "core.quotePath=false", "ls-tree", "-r", "-z", "--full-tree", upstream, cwd=repo)
    kept, dropped = [], []
    for record in filter(None, entries.split("\0")):
        meta, path = record.split("\t", 1)
        mode = meta.split()[0]
        if mode == "120000":
            raise RepositoryImportError(f"The repository contains a symbolic link ({path[:200]}), which Cavman does not import.")
        if mode == "160000":
            raise RepositoryImportError(f"The repository contains a submodule ({path[:200]}), which Cavman does not import.")
        if mode not in {"100644", "100755"}:
            raise RepositoryImportError(f"The repository contains an unsupported entry ({path[:200]}).")
        (dropped if _excluded(path) else kept).append(record)
    if not kept:
        raise RepositoryImportError("That repository has no files Cavman can import.")
    if len(kept) > max_files:
        raise RepositoryImportError(f"That repository has more than {max_files} files, the import limit.")
    # Build the project's first commit from exactly the kept files, then drop
    # the upstream history, remote and shallow state entirely.
    _git("read-tree", "--empty", cwd=repo)
    try:
        subprocess.run(["/usr/bin/git", "-c", "core.hooksPath=/dev/null", "update-index", "-z", "--index-info"],
                       cwd=repo, env=_environment(), input="\0".join(kept) + "\0",
                       text=True, capture_output=True, check=True, timeout=60)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        raise RepositoryImportError("Git could not stage the imported files.", 502) from None
    tree = _git("write-tree", cwd=repo).strip()
    message = (f"Import {source.full_name} into {name}\n\nStarted from https://github.com/{source.full_name} "
               f"at {upstream} ({branch}).\n")
    commit = _git("commit-tree", tree, "-m", message, cwd=repo).strip()
    _git("update-ref", "refs/heads/main", commit, cwd=repo)
    _git("symbolic-ref", "HEAD", "refs/heads/main", cwd=repo)
    for ref in _git("for-each-ref", "--format=%(refname)", cwd=repo).split():
        if ref != "refs/heads/main":
            _git("update-ref", "--no-deref", "-d", ref, cwd=repo)
    _git("remote", "remove", "origin", cwd=repo)
    _git("reflog", "expire", "--expire=now", "--all", cwd=repo)
    _git("gc", "--quiet", "--prune=now", cwd=repo, timeout=CLONE_TIMEOUT_SECONDS)
    (repo / ".git" / "shallow").unlink(missing_ok=True)
    _git("checkout", "--quiet", "--force", "main", cwd=repo)
    # Cavman's own state stays out of the project without changing its files.
    info = repo / ".git" / "info"
    info.mkdir(exist_ok=True)
    (info / "exclude").write_text(PROJECT_GITIGNORE)
    _git("fsck", "--no-dangling", "--no-progress", cwd=repo, timeout=CLONE_TIMEOUT_SECONDS)
    return Imported(url=f"https://github.com/{source.full_name}", commit=upstream, branch=branch,
                    files=len(kept), dropped=tuple(record.split("\t", 1)[1] for record in dropped[:50]))
