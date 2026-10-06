"""Node/TypeScript checks inside the network-denied jail, and isolated npm installs."""
import socket
import subprocess
from pathlib import Path

import pytest

from walter.sandbox import SandboxViolation, WorkspaceManager, _detect_node_root

pytestmark = pytest.mark.skipif(
    not (Path("/usr/bin/bwrap").exists() and _detect_node_root()), reason="Bubblewrap or Node unavailable")


def registry_reachable() -> bool:
    import os
    import urllib.request
    try:
        urllib.request.urlopen("https://registry.npmjs.org/typescript", timeout=5).close()
        return bool(os.environ.get("HTTPS_PROXY") or socket.gethostbyname("registry.npmjs.org"))
    except Exception:
        return False


@pytest.fixture
def manager(tmp_path):
    repo = tmp_path / "project"
    repo.mkdir()
    (repo / "README.md").write_text("# project\n")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "i"],
                   check=True)
    workspaces = WorkspaceManager(repo)
    grant = workspaces.create_candidate("run", "task", "author")
    return workspaces, grant


def write(workspaces, grant, path, content):
    workspaces.write_file(grant.id, path, content, worker_id="author")


def test_typescript_tests_run_in_the_jail_and_report_failures(manager):
    workspaces, grant = manager
    write(workspaces, grant, "sum.ts", "export const add = (a: number, b: number): number => a + b;\n")
    write(workspaces, grant, "sum.test.ts",
          'import { test } from "node:test";\nimport assert from "node:assert/strict";\n'
          'import { add } from "./sum.ts";\n'
          'test("adds", () => { assert.equal(add(2, 3), 5); });\n'
          'test("broken", () => { assert.equal(add(2, 2), 5); });\n')
    result = workspaces.run_command(grant.id, "test", ["node", "--test", "sum.test.ts"], worker_id="author")
    assert result.returncode != 0
    assert "# pass 1" in result.stdout and "# fail 1" in result.stdout


def test_node_jail_has_no_network_and_no_host_secrets(manager, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-never-expose-this")
    workspaces, grant = manager
    write(workspaces, grant, "jail.test.mjs", """import { test } from "node:test";
import assert from "node:assert/strict";
import net from "node:net";
import fs from "node:fs";
test("no secrets", () => {
  assert.equal(process.env.OPENROUTER_API_KEY, undefined);
  assert.equal(Object.keys(process.env).some((k) => /KEY|TOKEN|SECRET/.test(k)), false);
});
test("no network", async () => {
  await assert.rejects(new Promise((resolve, reject) => {
    const socket = net.connect({ host: "1.1.1.1", port: 443 }, resolve);
    socket.on("error", reject);
  }));
});
test("workspace is read-only", () => {
  assert.throws(() => fs.writeFileSync("/workspace/escape.txt", "x"));
});
""")
    result = workspaces.run_command(grant.id, "test", ["node", "--test", "jail.test.mjs"], worker_id="author")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "# pass 3" in result.stdout


def test_node_templates_refuse_arbitrary_commands_and_files(manager):
    workspaces, grant = manager
    write(workspaces, grant, "app.js", "console.log(1)\n")
    write(workspaces, grant, "app.test.js", "import 'node:test';\n")
    for category, argv in (("test", ["node", "app.js"]), ("test", ["node", "--test", "app.js"]),
                           ("test", ["node", "--test", "../x.test.js"]), ("test", ["node", "-e", "1"]),
                           ("check", ["tsc", "--project", "/"]), ("test", ["npm", "test"]),
                           ("build", ["npm", "run", "test"]), ("test", ["npm", "run", "build"]),
                           ("build", ["npm", "run", "build", "--", "x"])):
        with pytest.raises(SandboxViolation):
            workspaces.run_command(grant.id, category, argv, worker_id="author")
    with pytest.raises(SandboxViolation, match="trusted install"):
        workspaces.run_command(grant.id, "test", ["node", "--test", "app.test.js"], worker_id="author",
                               node_modules=Path("/tmp"))


def test_project_build_runs_its_script_in_the_jail_without_hooks_or_network(manager):
    workspaces, grant = manager
    write(workspaces, grant, "package.json",
          '{"name": "demo", "private": true, "scripts": {"prebuild": "echo PREBUILD-RAN",'
          ' "build": "node build.js"}}\n')
    write(workspaces, grant, "build.js",
          "const fs = require('fs'); fs.mkdirSync('dist'); fs.writeFileSync('dist/out.txt', 'ok');\n"
          "console.log('BUILT', fs.readFileSync('dist/out.txt', 'utf8'));\n"
          "require('net').connect(443, '1.1.1.1').on('error', (e) => console.log('NET', e.code));\n")
    node_modules = workspaces.node_dependencies(grant.id)
    result = workspaces.run_command(grant.id, "build", ["npm", "run", "build"], worker_id="author",
                                    node_modules=node_modules, timeout=120)
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "BUILT ok" in output and "PREBUILD-RAN" not in output
    assert "NET" not in output or "NET EPERM" in output or "NET EACCES" in output
    assert "dist/out.txt" not in workspaces.list_files(grant.id)  # build output stays in scratch


def test_project_build_reports_a_failing_script(manager):
    workspaces, grant = manager
    write(workspaces, grant, "package.json", '{"name": "demo", "private": true, "scripts": {"build": "exit 3"}}\n')
    result = workspaces.run_command(grant.id, "build", ["npm", "run", "build"], worker_id="author",
                                    node_modules=workspaces.node_dependencies(grant.id), timeout=120)
    assert result.returncode != 0


def test_no_package_json_means_no_install(manager):
    workspaces, grant = manager
    assert workspaces.node_dependencies(grant.id) is None


@pytest.mark.skipif(not registry_reachable(), reason="npm registry unreachable")
def test_isolated_install_then_typecheck(manager):
    workspaces, grant = manager
    write(workspaces, grant, "package.json",
          '{"name": "demo", "private": true, "type": "module",'
          ' "devDependencies": {"typescript": "5.9.3"},'
          ' "scripts": {"postinstall": "node -e \\"require(\'fs\').writeFileSync(\'/work/PWNED\', \'x\')\\""}}\n')
    write(workspaces, grant, "tsconfig.json",
          '{"compilerOptions": {"strict": true, "noEmit": true, "module": "nodenext", '
          '"allowImportingTsExtensions": true}, "include": ["*.ts"]}\n')
    write(workspaces, grant, "sum.ts", "export const add = (a: number, b: number): number => a + b;\n")
    node_modules = workspaces.node_dependencies(grant.id)
    assert (node_modules / "typescript/bin/tsc").is_file()
    assert not (node_modules.parent / "PWNED").exists()  # lifecycle scripts never ran
    assert workspaces.node_dependencies(grant.id) == node_modules  # cached by manifest digest
    ok = workspaces.run_command(grant.id, "check", ["tsc"], worker_id="author", node_modules=node_modules,
                                timeout=120)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    write(workspaces, grant, "sum.ts", "export const add = (a: number, b: number): number => a + 'b';\n")
    bad = workspaces.run_command(grant.id, "check", ["tsc"], worker_id="author", node_modules=node_modules,
                                 timeout=120)
    assert bad.returncode != 0 and "error TS" in bad.stdout


def test_every_execution_goes_through_the_backend_seam(tmp_path, monkeypatch):
    from walter.sandbox import CommandResult

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-never-expose-this")
    seen = []

    class RecordingBackend:
        name = "recording"

        def run(self, spec):
            seen.append(spec)
            return CommandResult(0, "ok", "")

    repo = tmp_path / "p"
    repo.mkdir()
    (repo / "a.py").write_text("A = 1\n")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "i"],
                   check=True)
    workspaces = WorkspaceManager(repo, backend=RecordingBackend())
    grant = workspaces.create_candidate("run", "task", "author")
    workspaces.write_file(grant.id, "a.test.js", "import 'node:test';\n", worker_id="author")
    workspaces.run_command(grant.id, "build", ["python3", "-m", "py_compile", "a.py"], worker_id="author")
    workspaces.run_command(grant.id, "test", ["node", "--test", "a.test.js"], worker_id="author")
    assert [spec.network for spec in seen] == [False, False]
    for spec in seen:
        assert ("/tmp" in {target for _, target in spec.writable_mounts})
        assert {target for _, target in spec.readonly_mounts} >= {"/workspace"}
        assert not any("OPENROUTER" in name or "never-expose" in value for name, value in spec.environment)
    assert seen[1].argv[0] == "/opt/node/bin/node" and seen[1].address_space > seen[0].address_space
