"""Publish a completed run's verified project to GitHub: a new repository, or a
pull request on the repository an earlier build of the same project created.

Publishing is a promotion, so it happens only on the user's explicit request
for one exact target (repository name, visibility, commit). The GitHub token
arrives per request from the web server, which holds it for the signed-in
user; it is never stored, logged or written into the project repository.
Only the integration head that the delivery archive was built from is pushed:
to ``main`` of a repository created for it, or to a new ``cavman/<run>``
branch for a pull request. No existing branch is ever overwritten, and the
default branch changes only if the user merges the pull request.
"""
from __future__ import annotations

import base64
import json
import re
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

from .engine import _GIT_ENV

REPO_NAME = re.compile(r"^[A-Za-z0-9._-]{1,100}$")


class PublishError(RuntimeError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class GitHubClient:
    def __init__(self, token: str, api_url: str = "https://api.github.com"):
        self._token = token
        self._api = api_url.rstrip("/")

    def _request(self, method: str, path: str, body: dict | None = None, *,
                 action: str = "create repositories") -> dict:
        request = urllib.request.Request(
            self._api + path, method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {self._token}", "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "cavman-publisher",
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = json.loads(exc.read() or b"{}").get("message", "")
            except ValueError:
                pass
            if exc.code == 401:
                raise PublishError("GitHub rejected the access token. Reconnect GitHub and try again.", 401) from None
            if exc.code == 403:
                raise PublishError(f"GitHub refused: the token lacks permission to {action}.", 403) from None
            if exc.code == 404 and action != "create repositories":
                raise PublishError("GitHub could not find the repository, or this account cannot reach it.",
                                   404) from None
            if exc.code == 422 and action != "create repositories":
                raise PublishError(f"GitHub could not {action}: {detail or 'invalid request'}.", 409) from None
            if exc.code == 422:
                raise PublishError(f"GitHub could not create that repository: {detail or 'invalid request'}. "
                                   "It may already exist; choose another name.", 409) from None
            raise PublishError(f"GitHub returned an error ({exc.code}).", 502) from None
        except urllib.error.URLError:
            raise PublishError("GitHub could not be reached.", 502) from None

    def create_repository(self, name: str, private: bool, description: str) -> dict:
        data = self._request("POST", "/user/repos", {
            "name": name, "private": private, "description": description[:350],
            "auto_init": False, "has_wiki": False})
        return {"full_name": data["full_name"], "html_url": data["html_url"], "clone_url": data["clone_url"]}

    def repository(self, full_name: str) -> dict:
        data = self._request("GET", f"/repos/{full_name}", action="read the repository")
        return {"full_name": data["full_name"], "clone_url": data["clone_url"],
                "default_branch": data.get("default_branch") or "main", "private": bool(data.get("private"))}

    def create_pull_request(self, full_name: str, head: str, base: str, title: str, body: str) -> dict:
        data = self._request("POST", f"/repos/{full_name}/pulls",
                             {"head": head, "base": base, "title": title[:200], "body": body[:4000]},
                             action="open the pull request")
        return {"html_url": data["html_url"], "number": data.get("number")}


def push(repo: Path, commit: str, remote_url: str, token: str, ref: str = "refs/heads/main") -> None:
    """Push one commit to ``ref`` of a remote, never forced, authenticating through the environment.

    The credential is passed as git config through environment variables, so it
    never appears in argv, the repository's config, or the process list.
    """
    credential = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    environment = {**_GIT_ENV, "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.extraHeader",
                   "GIT_CONFIG_VALUE_0": f"Authorization: Basic {credential}"}
    result = subprocess.run(
        ["/usr/bin/git", "-c", "core.hooksPath=/dev/null", "-C", str(repo), "push", "--quiet",
         remote_url, f"{commit}:{ref}"],
        env=environment, capture_output=True, text=True, timeout=300)
    if result.returncode:
        message = result.stderr.replace(token, "[redacted]").replace(credential, "[redacted]").strip()
        raise PublishError("Pushing to GitHub failed: " + message[-500:], 502)
