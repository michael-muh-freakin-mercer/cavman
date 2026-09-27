import json
import os
from dataclasses import replace
from pathlib import Path
import shutil
import subprocess

import pytest

from caveman.sandbox import (
    MAX_AGGREGATE_RSS,
    MAX_PROCESSES,
    MAX_SCRATCH_BYTES,
    SandboxUnavailable,
    SandboxViolation,
    SafetyApprovalVerification,
    WorkspaceManager,
)
from caveman.orchestration import Orchestrator
from caveman.store import SQLiteStore


@pytest.fixture
def workspace(tmp_path):
    repo = tmp_path / "fixture"
    repo.mkdir()
    (repo / "src/caveman").mkdir(parents=True)
    (repo / "docs").mkdir()
    (repo / "tests").mkdir()
    (repo / "hello.py").write_text("VALUE = 1\n")
    (repo / "src/caveman/sandbox.py").write_text("BOUNDARY = 'candidate source'\n")
    (repo / "tests/test_hello.py").write_text(
        "from hello import VALUE\n\ndef test_value(): assert VALUE == 1\n"
    )
    (repo / ".env").write_text("FIXTURE_SECRET=never-expose\n")
    (repo / ".npmrc").write_text("//registry/:_authToken=never-expose\n")
    (repo / ".pypirc").write_text("password=never-expose\n")
    (repo / "secrets.yaml").write_text("token: never-expose\n")
    (repo / "token.json").write_text('{"token":"never-expose"}\n')
    (repo / ".env.example").write_text("FIXTURE_SECRET=replace-me\n")
    (repo / "PERMISSIONS.md").write_text("fixture policy source\n")
    (repo / "docs/credentials-guide.md").write_text("safe documentation\n")
    (repo / "keys.py").write_text("KEY_NAMES = []\n")
    (repo / "tokenizer.py").write_text("def tokenize(value): return value.split()\n")
    (repo / "secrets.example.yaml").write_text("token: replace-me\n")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Fixture", "-c",
                    "user.email=fixture@example.invalid", "commit", "-qm", "fixture"], check=True)
    manager = WorkspaceManager(repo)
    grant = manager.create_candidate("run", "task", "author")
    return manager, grant, repo


def test_candidate_source_and_safety_files_are_readable_and_fully_identified(workspace):
    manager, grant, repo = workspace
    assert manager.read_file(grant.id, "PERMISSIONS.md", worker_id="author")
    assert "candidate source" in manager.read_file(
        grant.id, "src/caveman/sandbox.py", worker_id="author"
    )
    before = manager.fingerprint(grant.id)
    manager.write_file(grant.id, "hello.py", "VALUE = 2\n", worker_id="author")
    assert manager.fingerprint(grant.id) != before
    assert "+VALUE = 2" in manager.diff(grant.id)
    assert (repo / "src/caveman/sandbox.py").read_text() == "BOUNDARY = 'candidate source'\n"

    # Even an out-of-band safety-file mutation is visible to artifact identity.
    prior_safety = manager.fingerprint(grant.id)
    (Path(grant.root) / "src/caveman/sandbox.py").write_text("BOUNDARY = 'tampered'\n")
    assert manager.fingerprint(grant.id) != prior_safety
    assert "+BOUNDARY = 'tampered'" in manager.diff(grant.id)

    manager.write_file(grant.id, "new/module.py", "VALUE = 2\n", worker_id="author")
    assert "new/module.py" in manager.diff(grant.id)
    with_new = manager.fingerprint(grant.id)
    os.chmod(Path(grant.root) / "new/module.py", 0o700)
    assert manager.fingerprint(grant.id) != with_new
    with_mode = manager.fingerprint(grant.id)
    manager.delete_file(grant.id, "new/module.py", worker_id="author")
    assert manager.fingerprint(grant.id) != with_mode

    # A candidate commit cannot hide changes: identity and diff are relative to signed base_revision.
    subprocess.run(["git", "-C", grant.root, "add", "src/caveman/sandbox.py"], check=True)
    subprocess.run(["git", "-C", grant.root, "-c", "user.name=Candidate", "-c",
                    "user.email=candidate@example.invalid", "commit", "-qm", "candidate"], check=True)
    assert "src/caveman/sandbox.py" in manager.diff(grant.id)
    assert grant.base_revision != subprocess.run(
        ["git", "-C", grant.root, "rev-parse", "HEAD"], check=True,
        capture_output=True, text=True,
    ).stdout.strip()


def test_default_grant_cannot_write_or_delete_safety_boundary(workspace):
    manager, grant, _ = workspace
    protected = [
        "PERMISSIONS.md", "SYSTEM_PROMPT.md", "src/caveman/sandbox.py",
        "src/caveman/orchestration.py",
        # Cost control, the worker contract and the launch surface decide
        # authority just as much as the kernel does.
        "src/caveman/usage.py", "src/caveman/usage_model.py",
        "src/caveman/contracts.py", "src/caveman/__init__.py", "pyproject.toml",
    ]
    # Missing protected paths are still protected from creation.
    for path in protected:
        with pytest.raises(SandboxViolation, match="denied"):
            manager.write_file(grant.id, path, "weaken safety\n", worker_id="author")
        if Path(grant.root, path).exists():
            with pytest.raises(SandboxViolation):
                manager.delete_file(grant.id, path, worker_id="author")
    assert manager.read_file(grant.id, "PERMISSIONS.md", worker_id="author")
    assert "src/caveman/sandbox.py" in manager.list_files(grant.id, worker_id="author")


def _safety_scope(repo, base, *, run_id="run", task_id="safety-task",
                  worker_id="safety-author", paths=("src/caveman/sandbox.py",),
                  operation="write"):
    return {
        "action": "safety_boundary_change",
        "category": "safety_boundary_change",
        "run_id": run_id,
        "task_id": task_id,
        "worker_id": worker_id,
        "repository": str(repo.resolve()),
        "base_commit": base,
        "allowed_paths": sorted(paths),
        "operation": operation,
    }


def _core_verifier(core):
    def verify(run_id, approval_id, action, scope):
        core.require_approval(run_id, approval_id, action, scope)
        run = core.get_run(run_id)
        return SafetyApprovalVerification(
            approval_id=approval_id,
            scope_digest=run.approvals[approval_id].scope_digest,
            human_id=run.approval_decisions[approval_id].human_id,
            category=run.approvals[approval_id].category,
        )
    return verify


def test_reserved_safety_facility_is_inert_at_production_construction_sites(tmp_path):
    """The safety-grant path is a reserved facility (2026-09-21 decision).

    No Manager tool, CLI command, or construction site injects an
    approval_verifier, so the shipped runtime can only ever deny. This test
    pins that posture: if a construction site ever starts injecting a
    verifier, it fails until the security design review TOOLS.md requires
    has happened and this test is deliberately updated.
    """
    from caveman import cli, readiness
    import inspect as _inspect

    for source in (cli, readiness):
        assert "approval_verifier" not in _inspect.getsource(source), (
            f"{source.__name__} now injects an approval verifier; the reserved "
            "safety facility requires a design review before being wired")

    repo = tmp_path / "fixture"
    repo.mkdir()
    (repo / "hello.py").write_text("VALUE = 1\n")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Fixture", "-c",
                    "user.email=fixture@example.invalid", "commit", "-qm", "fixture"],
                   check=True)
    manager = WorkspaceManager(repo)  # constructed exactly as production does
    with pytest.raises(SandboxViolation, match="Manager approval verifier"):
        manager.create_safety_candidate(
            "run", "safety-task", "safety-author", "a" * 64,
            ("src/caveman/sandbox.py",), "write")


def test_safety_candidate_denies_untrusted_or_forged_approval(workspace):
    manager, _, repo = workspace
    arbitrary_hex = "a" * 64
    with pytest.raises(SandboxViolation, match="Manager approval verifier"):
        manager.create_safety_candidate(
            "run", "safety-task", "safety-author", arbitrary_hex,
            ("src/caveman/sandbox.py",), "write")

    forged = WorkspaceManager(
        repo, approval_verifier=lambda *_: SafetyApprovalVerification(
            approval_id="different", scope_digest="b" * 64, human_id="human",
            category="safety_boundary_change"))
    with pytest.raises(SandboxViolation, match="Invalid safety approval verification"):
        forged.create_safety_candidate(
            "run", "safety-task", "safety-author", arbitrary_hex,
            ("src/caveman/sandbox.py",), "write")


def test_core_approved_safety_candidate_is_exact_bounded_and_single_use(workspace):
    _, normal_grant, repo = workspace
    core = Orchestrator(SQLiteStore())
    run = core.create_run("Change safety boundary", ["exact reviewed change"])
    old_base = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    old_scope = _safety_scope(repo, old_base, run_id=run.id)
    old_approval = core.request_approval(
        run.id, "safety_boundary_change", old_scope, "Review old candidate",
        category="safety_boundary_change")
    core.decide_approval(run.id, old_approval.id, True, "operator", "Approved old base")
    (repo / "base-change.txt").write_text("new candidate base\n")
    subprocess.run(["git", "-C", str(repo), "add", "base-change.txt"], check=True)
    subprocess.run([
        "git", "-C", str(repo), "-c", "user.name=Fixture", "-c",
        "user.email=fixture@example.invalid", "commit", "-qm", "new base"], check=True)
    stale_manager = WorkspaceManager(repo, approval_verifier=_core_verifier(core))
    with pytest.raises(SandboxViolation, match="Exact scoped human approval required"):
        stale_manager.create_safety_candidate(
            run.id, "safety-task", "safety-author", old_approval.id,
            ("src/caveman/sandbox.py",), "write")

    base = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    scope = _safety_scope(repo, base, run_id=run.id)
    wrong_category = core.request_approval(
        run.id, "safety_boundary_change", scope, "Wrong approval category",
        category="general_change")
    core.decide_approval(run.id, wrong_category.id, True, "operator", "Approved")
    category_manager = WorkspaceManager(repo, approval_verifier=_core_verifier(core))
    with pytest.raises(SandboxViolation, match="Invalid safety approval verification"):
        category_manager.create_safety_candidate(
            run.id, "safety-task", "safety-author", wrong_category.id,
            ("src/caveman/sandbox.py",), "write")
    approval = core.request_approval(
        run.id, "safety_boundary_change", scope, "Human reviewed exact boundary change",
        category="safety_boundary_change", target="src/caveman/sandbox.py",
        risk="Changes Caveman's authority boundary")
    core.decide_approval(run.id, approval.id, True, "operator", "Approved exact scope")
    manager = WorkspaceManager(repo, approval_verifier=_core_verifier(core))

    for changed in (
        {"run_id": "wrong-run"}, {"task_id": "wrong-task"},
        {"worker_id": "wrong-worker"},
    ):
        values = {"run_id": run.id, "task_id": "safety-task", "worker_id": "safety-author"}
        values.update(changed)
        with pytest.raises(SandboxViolation, match="Exact scoped human approval required"):
            manager.create_safety_candidate(
                values["run_id"], values["task_id"], values["worker_id"], approval.id,
                ("src/caveman/sandbox.py",), "write")
    with pytest.raises(SandboxViolation, match="Exact scoped human approval required"):
        manager.create_safety_candidate(
            run.id, "safety-task", "safety-author", approval.id,
            ("PERMISSIONS.md",), "write")
    with pytest.raises(SandboxViolation, match="Exact scoped human approval required"):
        manager.create_safety_candidate(
            run.id, "safety-task", "safety-author", approval.id,
            ("src/caveman/sandbox.py",), "delete")

    grant = manager.create_safety_candidate(
        run.id, "safety-task", "safety-author", approval.id,
        ("src/caveman/sandbox.py",), "write")
    assert grant.safety_approval_id == approval.id
    assert grant.safety_approval_digest == approval.scope_digest
    manager.write_file(
        grant.id, "src/caveman/sandbox.py", "BOUNDARY = 'approved candidate'\n",
        worker_id="safety-author")
    with pytest.raises(SandboxViolation, match="Write denied"):
        manager.write_file(grant.id, "hello.py", "bad\n", worker_id="safety-author")
    reloaded = WorkspaceManager(repo, approval_verifier=_core_verifier(core))
    with pytest.raises(SandboxViolation, match="already consumed"):
        reloaded.create_safety_candidate(
            run.id, "safety-task", "safety-author", approval.id,
            ("src/caveman/sandbox.py",), "write")

    # The normal grant still has no authority to mutate the boundary.
    with pytest.raises(SandboxViolation, match="Write denied"):
        reloaded.write_file(
            normal_grant.id, "src/caveman/sandbox.py", "bad\n", worker_id="author")


def test_safety_approval_rejects_candidate_author_as_human_authority(workspace):
    _, _, repo = workspace
    core = Orchestrator(SQLiteStore())
    run = core.create_run("Change safety boundary", ["reviewed"])
    base = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True,
        capture_output=True, text=True).stdout.strip()
    scope = _safety_scope(repo, base, run_id=run.id)
    approval = core.request_approval(
        run.id, "safety_boundary_change", scope, "review", category="safety_boundary_change")
    core.decide_approval(run.id, approval.id, True, "safety-author", "self approval")
    manager = WorkspaceManager(repo, approval_verifier=_core_verifier(core))
    with pytest.raises(SandboxViolation, match="Invalid safety approval verification"):
        manager.create_safety_candidate(
            run.id, "safety-task", "safety-author", approval.id,
            ("src/caveman/sandbox.py",), "write")


def test_path_aware_state_and_secret_policy(workspace):
    manager, grant, _ = workspace
    denied = [
        "../hello.py", "/tmp/escape", ".git", ".git/config", ".local/state",
        ".env", ".env.production", "nested/key.pem", "nested/private.key",
        ".npmrc", ".pypirc", "secrets.yaml", "token.json", ".ssh/config",
        ".aws/credentials", "nested/credentials.json", "nested/secrets.yml",
        ".docker/config.json", ".config/gcloud/application_default_credentials.json",
    ]
    for path in denied:
        with pytest.raises(SandboxViolation):
            manager.write_file(grant.id, path, "attack", worker_id="author")
        with pytest.raises(SandboxViolation):
            manager.read_file(grant.id, path, worker_id="author")

    # Candidate code/policy and harmless names are not confused with host secrets/state.
    for path in ["PERMISSIONS.md", "src/caveman/sandbox.py", "docs/credentials-guide.md",
                 "keys.py", ".env.example", "tokenizer.py", "secrets.example.yaml"]:
        assert manager.read_file(grant.id, path, worker_id="author")
    files = manager.list_files(grant.id, worker_id="author")
    assert not {".env", ".npmrc", ".pypirc", "secrets.yaml", "token.json"} & set(files)
    assert ".env.example" in files
    assert "src/caveman/sandbox.py" in files


def test_symlinks_hardlinks_live_checkout_and_worker_identity(workspace):
    manager, grant, repo = workspace
    root = Path(grant.root)
    (root / "escape").symlink_to("/etc", target_is_directory=True)
    (root / "link").symlink_to(repo / "hello.py")
    os.link(repo / "hello.py", root / "hard")
    for path in ["escape/hello.py", "link", "hard", str(repo / "hello.py")]:
        with pytest.raises(SandboxViolation):
            manager.write_file(grant.id, path, "attack", worker_id="author")
        with pytest.raises(SandboxViolation):
            manager.read_file(grant.id, path, worker_id="author")
    with pytest.raises(SandboxViolation):
        manager.fingerprint(grant.id)  # directory symlink cannot leave identity unchanged
    with pytest.raises(SandboxViolation):
        manager.status(grant.id)
    assert (repo / "hello.py").read_text() == "VALUE = 1\n"
    with pytest.raises(SandboxViolation):
        manager.read_file(grant.id, "hello.py", worker_id="intruder")


def test_reviewer_preserves_original_author_across_derivation(workspace):
    manager, grant, _ = workspace
    reviewer = manager.reviewer_grant(grant.id, "reviewer")
    assert reviewer.author_id == "author"
    assert reviewer.worker_id == "reviewer"
    second = manager.reviewer_grant(reviewer.id, "second-reviewer")
    assert second.author_id == "author"
    with pytest.raises(SandboxViolation):
        manager.reviewer_grant(reviewer.id, "author")
    with pytest.raises(SandboxViolation):
        manager.write_file(reviewer.id, "hello.py", "bad", worker_id="reviewer")


def test_signed_manifest_rejects_binding_tamper(workspace):
    manager, grant, repo = workspace
    manifest = manager.state_root / "grants.json"
    envelope = json.loads(manifest.read_text())
    state = json.loads(envelope["payload"])
    payload = state["grants"]
    record = next(item for item in payload if item["id"] == grant.id)
    record["task_id"] = "forged-task"
    record["worker_id"] = "forged-worker"
    record["root"] = str(repo)
    record["branch"] = "main"
    state["grants"] = payload
    envelope["payload"] = json.dumps(state, sort_keys=True, separators=(",", ":"))
    manifest.write_text(json.dumps(envelope))
    with pytest.raises(SandboxViolation, match="authentication"):
        WorkspaceManager(repo)


def test_worktree_branch_binding_is_reverified(workspace):
    manager, grant, _ = workspace
    subprocess.run(["git", "-C", grant.root, "branch", "-m", "forged"], check=True)
    with pytest.raises(SandboxViolation, match="signed candidate binding"):
        manager.read_file(grant.id, "hello.py", worker_id="author")


def test_inspect_grant_is_read_only_and_enforces_worker_binding(workspace):
    manager, grant, _ = workspace
    inspected = manager.inspect_grant(grant.id, worker_id="author")
    assert inspected == grant and inspected is not grant
    with pytest.raises(AttributeError):
        inspected.worker_id = "intruder"
    with pytest.raises(SandboxViolation, match="different worker"):
        manager.inspect_grant(grant.id, worker_id="intruder")
    with pytest.raises(SandboxViolation, match="Unknown or inactive"):
        manager.inspect_grant("missing", worker_id="author")


def test_useful_edit_freeze_reload_and_cleanup(workspace):
    manager, grant, repo = workspace
    manager.write_file(grant.id, "hello.py", "VALUE = 2\n", worker_id="author")
    identity = manager.freeze(grant.id)
    reviewer = manager.reviewer_grant(grant.id, "reviewer")
    restored = WorkspaceManager(repo)
    assert restored.fingerprint(reviewer.id) == identity
    with pytest.raises(SandboxViolation):
        restored.write_file(grant.id, "hello.py", "bad", worker_id="author")
    restored.cleanup(grant.id)
    assert not Path(grant.root).exists()
    with pytest.raises(SandboxViolation):
        restored.read_file(reviewer.id, "hello.py", worker_id="reviewer")


def test_command_templates_deny_model_chosen_executables_and_arguments(workspace):
    manager, grant, _ = workspace
    bad = [
        ("push", ["git", "push"]),
        ("test", ["python", "-c", "import os; os.system('id')"]),
        ("test", ["python", "-m", "pytest", "--trace"]),
        ("build", ["python", "-m", "pip", "install", "evil"]),
        ("build", ["python", "-m", "py_compile", "../outside.py"]),
    ]
    for category, argv in bad:
        with pytest.raises(SandboxViolation):
            manager.run_command(grant.id, category, argv, worker_id="author")


def test_pytest_template_accepts_candidate_test_paths_and_denies_traversal(workspace):
    manager, grant, _ = workspace
    manager.write_file(grant.id, "tests/test_candidate.py",
                       "def test_ok():\n    assert True\n", worker_id="author")
    argv = ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider",
            "tests/test_candidate.py"]
    assert manager._command(grant, "test", argv) == ["/opt/caveman-env/bin/python", *argv[1:]]
    for path in ("tests/test_missing.py", "../outside.py", "/tmp/evil.py",
                 "tests/../outside.py"):
        with pytest.raises(SandboxViolation):
            manager._command(grant, "test",
                             ["python", "-m", "pytest", "-q", path])
    with pytest.raises(SandboxViolation):
        manager._command(grant, "test", ["python", "-m", "unittest", "discover", "-v"])


def test_backend_absence_fails_closed_after_allowed_template(workspace, monkeypatch):
    manager, grant, _ = workspace
    original = Path.is_file
    monkeypatch.setattr(Path, "is_file",
                        lambda path: False if str(path) == "/usr/bin/bwrap" else original(path))
    with pytest.raises(SandboxUnavailable, match="host fallback prohibited"):
        manager.run_command(grant.id, "isolation_probe", ["sandbox-probe"], worker_id="author")


def test_real_isolated_caveman_dependency_execution_is_required(workspace):
    """This acceptance test must fail, never skip, when isolation is unavailable."""
    manager, grant, repo = workspace
    result = manager.run_command(
        grant.id, "test",
        ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_hello.py"],
        worker_id="author",
    )
    assert result.returncode == 0, result.stderr
    assert "1 passed" in result.stdout
    os.environ["FIXTURE_PRIVATE_ENV"] = "never-expose"
    try:
        probe = manager.run_command(
            grant.id, "isolation_probe", ["sandbox-probe"], worker_id="author"
        )
    finally:
        del os.environ["FIXTURE_PRIVATE_ENV"]
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.strip() == "isolated"
    assert (repo / "hello.py").read_text() == "VALUE = 1\n"


def test_build_template_compiles_to_scratch_without_candidate_writes(workspace):
    manager, grant, _ = workspace
    before = manager.fingerprint(grant.id)
    result = manager.run_command(
        grant.id, "build",
        ["python", "-m", "py_compile", "hello.py", "src/caveman/sandbox.py"],
        worker_id="author",
    )
    assert result.returncode == 0, result.stderr
    assert manager.fingerprint(grant.id) == before
    assert not list(Path(grant.root).rglob("*.pyc"))
    assert not list(Path(grant.root).rglob("__pycache__"))


def test_inventory_limits_fit_ordinary_repositories_and_listing_reads_no_contents(workspace):
    """2026-09-21 decision: limits raised (50 MB / 100k files) and
    list/status/changed_paths no longer read every file's bytes."""
    import caveman.sandbox as sandbox

    assert sandbox.MAX_FILE_BYTES == 50_000_000
    assert sandbox.MAX_FILES == 100_000

    manager, grant, _ = workspace
    # A 3 MB file was refused under the old 2 MB bound; it is ordinary now.
    manager.write_file(grant.id, "data.bin", "x" * 3_000_000, worker_id="author")
    inventory = manager._inventory(manager._get(grant.id), contents=False)
    assert inventory["data.bin"][2] == b""      # policy checked, bytes not read
    assert "data.bin" in manager.list_files(grant.id, worker_id="author")
    assert "data.bin" in manager.changed_paths(grant.id, worker_id="author")
    # Identity and diff still see real bytes.
    assert "x" * 100 in manager.diff(grant.id)
    full = manager._inventory(manager._get(grant.id))
    assert len(full["data.bin"][2]) == 3_000_000


def test_timeout_and_failed_execution_do_not_change_candidate(workspace):
    manager, grant, _ = workspace
    manager.write_file(
        grant.id, "tests/test_slow.py",
        "import time\ndef test_slow(): time.sleep(10)\n", worker_id="author",
    )
    before = manager.fingerprint(grant.id)
    with pytest.raises(SandboxViolation, match="wall-time"):
        manager.run_command(
            grant.id, "test", ["python", "-m", "pytest", "-q", "tests/test_slow.py"],
            worker_id="author", timeout=.1,
        )
    assert manager.fingerprint(grant.id) == before


def test_aggregate_resource_limits_are_practical_and_monitored(workspace):
    manager, _, tmp_repo = workspace
    scratch = tmp_repo / ".local/resource-test"
    scratch.mkdir(parents=True)
    (scratch / "payload").write_bytes(b"x" * 1024)
    processes, rss, storage = manager._usage(os.getpid(), scratch)
    assert processes >= 1 and rss > 0 and storage == 1024
    assert MAX_PROCESSES == 32
    assert MAX_AGGREGATE_RSS == 1_073_741_824
    assert MAX_SCRATCH_BYTES == 32_000_000


def test_missing_candidate_root_is_closed_on_load(workspace):
    manager, grant, repo = workspace
    shutil.rmtree(grant.root)
    reloaded = WorkspaceManager(repo)
    assert reloaded._grants[grant.id].lifecycle == "closed"
    with pytest.raises(SandboxViolation, match="Unknown or inactive"):
        reloaded.inspect_grant(grant.id, worker_id="author")
    assert json.loads((reloaded.state_root / "grants.json").read_text())


def test_mismatched_dependency_root_is_closed_on_load(workspace):
    manager, grant, repo = workspace
    manager._grants[grant.id] = replace(grant, dependency_root="/nonexistent/dependency/venv")
    manager._save()
    reloaded = WorkspaceManager(repo)
    assert reloaded._grants[grant.id].lifecycle == "closed"
    with pytest.raises(SandboxViolation, match="Unknown or inactive"):
        reloaded.read_file(grant.id, "hello.py", worker_id="author")


def test_tampered_signed_manifest_still_raises(workspace):
    manager, grant, repo = workspace
    manager._grants[grant.id] = replace(grant, root=str(repo))
    manager._save()
    with pytest.raises(SandboxViolation, match="binding is invalid"):
        WorkspaceManager(repo)


def test_valid_active_grant_survives_reload(workspace):
    manager, grant, repo = workspace
    reloaded = WorkspaceManager(repo)
    assert reloaded._grants[grant.id].lifecycle == "active"
    assert reloaded.inspect_grant(grant.id, worker_id="author").root == grant.root
    assert reloaded.read_file(grant.id, "hello.py", worker_id="author") == "VALUE = 1\n"


def test_lib64_symlink_target_resolves_this_hosts_dynamic_loader():
    """The sandbox must reproduce the loader layout of the host it runs on.

    Hardcoding one distribution's layout makes every sandboxed command fail with
    ``execvp <path>: No such file or directory`` on any host that arranges
    ``/usr/lib`` and ``/usr/lib64`` differently, and the error names the binary
    rather than the unresolvable interpreter.
    """
    from caveman.sandbox import _loader_dir_target

    target = _loader_dir_target()
    assert target in {"usr/lib", "usr/lib64"}
    loaders = {candidate: list((Path("/") / candidate).glob("ld-linux*"))
               for candidate in ("usr/lib64", "usr/lib")}
    if any(loaders.values()):
        assert loaders[target], f"{target} holds no dynamic loader on this host"
    else:  # no glibc loader anywhere: fall back to the historical target
        assert target == "usr/lib"
