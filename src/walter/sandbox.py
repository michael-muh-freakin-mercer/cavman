"""Manager-owned worktrees and fail-closed Linux command isolation.

Candidate source remains fully inspectable and fingerprinted.  Only repository
state, dependency caches, and credential-shaped paths are withheld.  Commands
run from a read-only source snapshot with a read-only dependency environment;
their only writable host-backed path is a fresh, bounded scratch directory.
"""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
from dataclasses import asdict, dataclass, replace
import errno
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path, PurePosixPath
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from typing import Protocol
import uuid


class SandboxViolation(PermissionError):
    pass


logger = logging.getLogger(__name__)


class IntegrationStale(SandboxViolation):
    """The candidate was built on a base the integration branch has moved past."""


class SandboxUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class SafetyApprovalVerification:
    """Result returned by a trusted Manager verifier after exact core validation."""
    approval_id: str
    scope_digest: str
    human_id: str
    category: str


class SafetyApprovalVerifier(Protocol):
    """Manager-only capability; implementations must call core.require_approval."""

    def __call__(self, run_id: str, approval_id: str, action: str,
                 scope: dict[str, object]) -> SafetyApprovalVerification: ...


@dataclass(frozen=True)
class WorkspaceGrant:
    id: str
    candidate_id: str
    run_id: str
    task_id: str
    worker_id: str
    author_id: str
    root: str
    repository: str
    branch: str
    base_revision: str
    dependency_root: str
    prohibited_roots: tuple[str, ...]
    command_categories: tuple[str, ...] = ("test", "build", "check", "isolation_probe")
    allow_safety_changes: bool = False
    safety_approval_id: str | None = None
    safety_approval_digest: str | None = None
    safety_allowed_paths: tuple[str, ...] = ()
    safety_operation: str | None = None
    read_only: bool = False
    lifecycle: str = "active"
    # Whether an attempt has actually executed against this workspace. An
    # approved-but-never-delegated escalation candidate is unused (safe to
    # hand to its first attempt); a workspace from a failed attempt is used
    # (dirty — a retry must get a fresh one). 2026-09-21 decision.
    used: bool = False


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


# Repository state and generated dependency trees are not candidate source.
STATE_PARTS = frozenset({".git", ".local", ".venv", "node_modules", "__pycache__", ".pytest_cache"})
SECRET_DIRS = frozenset({".ssh", ".aws", ".gnupg", ".azure", ".kube"})
SECRET_NAMES = frozenset({
    ".env", ".npmrc", ".pypirc", "credentials", "credentials.json", "credentials.yaml",
    "credentials.yml", "secrets.json", "secrets.yaml", "secrets.yml", "token", "token.txt",
    "token.json", "token.yaml", "token.yml", "access_token", "auth_token", "github_token",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "netrc", ".netrc",
})
SAFE_ENV_SUFFIXES = (".example", ".sample", ".template")
SECRET_SUFFIXES = (".pem", ".p12", ".pfx")

# These files define Walter's safety/authority boundary.  They remain readable,
# testable, diffable and fingerprinted, but ordinary developer grants cannot
# mutate them.  A Manager must create a separate signed grant bound to an exact
# human approval digest to authorize a safety-boundary candidate.
#
# The set is every doctrine file plus every control-plane module: anything that
# decides authority, cost, the worker contract, or how Walter is launched.
SAFETY_PATHS = frozenset({
    "AGENTS.md", "doctrine/SYSTEM_PROMPT.md", "doctrine/PERMISSIONS.md",
    "doctrine/AGENT_CREATION.md", "doctrine/QA_PROTOCOL.md", "doctrine/FAILURE_RECOVERY.md",
    "doctrine/TOOLS.md", "doctrine/TASK_PROTOCOL.md", "doctrine/OPERATING_MODEL.md",
    "doctrine/STATE_MODEL.md", "doctrine/CHARTER.md",
    "src/walter/sandbox.py", "src/walter/sandbox_e2b.py", "src/walter/orchestration.py", "src/walter/models.py",
    "src/walter/store.py", "src/walter/adapter.py", "src/walter/runtime.py",
    "src/walter/cli.py", "src/walter/readiness.py",
    # Cost control, the worker/task contract, the package surface, and the
    # console-script/dependency declaration are authority boundaries too: a
    # candidate that quietly disables the usage budget, widens the worker
    # contract, or repoints the `walter` entrypoint must be as conspicuous as
    # one that edits the kernel.
    "src/walter/usage.py", "src/walter/usage_model.py", "src/walter/contracts.py",
    "src/walter/__init__.py", "pyproject.toml",
})

# Inventory limits raised 2026-09-21 so ordinary repositories fit: the old
# 2 MB / 10k-file bounds capped Walter at toy projects. Symlinks remain
# forbidden — making them safe is a separate security design decision.
MAX_FILE_BYTES = 50_000_000
MAX_FILES = 100_000
MAX_SNAPSHOT_BYTES = 256_000_000
MAX_SCRATCH_BYTES = 32_000_000
MAX_PROCESSES = 32
MAX_AGGREGATE_RSS = 1_073_741_824
MAX_OUTPUT_BYTES = 1_000_000


# Internal staging ref holding exactly the accepted, validated candidate bytes of
# a project, one fast-forward commit per accepted developer candidate. It is
# never the user's checked-out branch and is never pushed; promotion beyond it
# remains a human-approved act (see doctrine/PERMISSIONS.md, 2026-09-28 decision).
INTEGRATION_BRANCH = "walter-integration"
INTEGRATION_REF = "refs/heads/" + INTEGRATION_BRANCH

AST_CHECK_SNIPPETS = frozenset({
    "import ast,pathlib; files=list(pathlib.Path('.').rglob('*.py')); assert files, 'No Python sources'; [ast.parse(p.read_text(), filename=str(p)) for p in files]",
    "import ast,pathlib; files=list(pathlib.Path('.').rglob('*.py')); "
    "assert files, 'No Python sources'; "
    "[ast.parse(p.read_text(), filename=str(p)) for p in files]",
})

ISOLATION_PROBE = r'''import os, socket
from pathlib import Path
assert not Path('/workspace/.git').exists()
assert not Path('/workspace/.env').exists()
assert 'FIXTURE_PRIVATE_ENV' not in os.environ
for path in ('/workspace/__probe_write', '/usr/__probe_write', '/opt/walter-env/__probe_write'):
    try: Path(path).write_text('bad')
    except OSError: pass
    else: raise AssertionError('write available: ' + path)
Path('/tmp/probe-output').write_text('isolated')
try: s = socket.socket(); s.settimeout(.1); s.connect(('192.0.2.1', 80))
except OSError: pass
else: raise AssertionError('network available')
print('isolated')
'''

PY_COMPILE = (
    "import pathlib,py_compile,sys; out=pathlib.Path('/tmp/pycompile'); "
    "out.mkdir(); [py_compile.compile(path, cfile=str(out / (str(index)+'.pyc')), doraise=True) "
    "for index,path in enumerate(sys.argv[1:])]"
)

# The project's own build script (`npm run build`). The workspace is mounted
# read-only, and builds write their output, so the sources are copied to scratch
# with the read-only node_modules linked in, and npm runs the script there. Its
# stdio is inherited: the network filter denies the socketpair() a pipe needs.
# Pre- and post-build hooks are not run (--ignore-scripts still runs the named
# script). Output is discarded with the scratch directory.
NPM_BUILD = (
    "const fs=require('fs'),path=require('path'),{spawnSync}=require('child_process');"
    "const dir='/tmp/build';"
    "fs.cpSync('/workspace',dir,{recursive:true,filter:(s)=>s!=='/workspace/node_modules'});"
    "if(fs.existsSync('/workspace/node_modules'))fs.symlinkSync('/workspace/node_modules',path.join(dir,'node_modules'));"
    "for(const f of fs.readdirSync(dir,{recursive:true})){const p=path.join(dir,String(f));"
    "if(!p.startsWith(path.join(dir,'node_modules')))fs.chmodSync(p,fs.statSync(p).isDirectory()?0o755:0o644);}"
    "const r=spawnSync(process.execPath,['/opt/node/lib/node_modules/npm/bin/npm-cli.js','run','build',"
    "'--ignore-scripts','--no-update-notifier'],{cwd:dir,stdio:'inherit'});"
    "if(r.error)console.error(String(r.error));process.exit(r.status===null?1:r.status);"
)

NETWORK_SYSCALLS = (
    b"socket", b"socketpair", b"connect", b"bind", b"listen", b"accept", b"accept4",
    b"sendto", b"sendmsg", b"sendmmsg", b"recvfrom", b"recvmsg", b"recvmmsg",
)


# Node toolchain. Candidate code only ever runs inside the network-denied jail;
# npm installs run in a separate jail with network but with install scripts
# disabled, so no package code executes during installation.
NODE_TEST_SUFFIXES = (".test.js", ".test.mjs", ".test.cjs", ".test.ts", ".test.mts",
                      ".spec.js", ".spec.mjs", ".spec.ts", ".spec.mts")
# V8 reserves far more address space than it uses: Wasm guard regions (disabled
# here) and heap reservations. Node processes therefore get a larger address-space
# cap than Python, while real memory stays bounded by the V8 heap limit below and
# the aggregate-RSS monitor that applies to every sandboxed command.
NODE_FLAGS = "--disable-wasm-trap-handler --max-old-space-size=768"
NODE_ADDRESS_SPACE = 4 * 1024 ** 3
PYTHON_ADDRESS_SPACE = 1024 ** 3
NPM_REGISTRY = "https://registry.npmjs.org/"
MAX_DEPENDENCY_BYTES = 600_000_000
DEPENDENCY_INSTALL_SECONDS = 300
PROXY_ENVIRONMENT = ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy")


def _node_test_path(path: str) -> bool:
    candidate = PurePosixPath(path)
    return (bool(candidate.parts) and not path.startswith("/") and path.endswith(NODE_TEST_SUFFIXES)
            and not any(part in {"", ".", ".."} for part in candidate.parts) and not _excluded(candidate))


_ISOLATION_FLAG: dict[str, str] = {}


def _node_isolation_flag(node_root: Path) -> str:
    """In-process test isolation: the network seccomp filter denies the
    socketpair() a per-file child process would need for its IPC channel."""
    key = str(node_root)
    if key not in _ISOLATION_FLAG:
        help_text = subprocess.run([str(node_root / "bin/node"), "--help"], capture_output=True,
                                   text=True, timeout=30, env={"PATH": "/usr/bin:/bin"}).stdout
        _ISOLATION_FLAG[key] = ("--test-isolation=none" if "--test-isolation=" in help_text
                                and "--experimental-test-isolation=" not in help_text
                                else "--experimental-test-isolation=none")
    return _ISOLATION_FLAG[key]


def _detect_node_root() -> Path | None:
    configured = os.environ.get("CAVMAN_NODE_ROOT")
    if configured:
        root = Path(configured).resolve()
    else:
        import shutil
        found = shutil.which("node")
        if not found:
            return None
        root = Path(found).resolve().parents[1]
    if not (root / "bin/node").is_file():
        return None
    # Type stripping and in-process test isolation need Node 22 or newer.
    try:
        version = subprocess.run([str(root / "bin/node"), "--version"], capture_output=True, text=True,
                                 timeout=15, env={"PATH": "/usr/bin:/bin"}).stdout.strip()
        major = int(version.lstrip("v").split(".")[0])
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    return root if major >= 22 else None


def _excluded(path: str | PurePosixPath) -> bool:
    """Apply path-aware state/credential policy without hiding candidate code."""
    parts = PurePosixPath(path).parts
    if not parts:
        return True
    lowered = tuple(part.lower() for part in parts)
    if any(part in STATE_PARTS or part in SECRET_DIRS for part in lowered):
        return True
    name = lowered[-1]
    if name in SECRET_NAMES or name.endswith(SECRET_SUFFIXES) or name.endswith(".key"):
        return True
    if name.startswith(".env.") and not name.endswith(SAFE_ENV_SUFFIXES):
        return True
    if len(lowered) >= 2 and lowered[-2:] in {
        (".docker", "config.json"), ("gcloud", "application_default_credentials.json"),
    }:
        return True
    return False


def _safety_path(path: str | PurePosixPath) -> bool:
    return PurePosixPath(path).as_posix() in SAFETY_PATHS


def _python_source_path(path: str) -> bool:
    candidate = PurePosixPath(path)
    return bool(candidate.parts) and not path.startswith("/") and candidate.suffix == ".py" and not any(
        part in {"", ".", ".."} for part in candidate.parts
    ) and not _excluded(candidate)


def _safe_relative(path: str) -> PurePosixPath:
    candidate = PurePosixPath(path)
    if (not candidate.parts or path.startswith("/") or
            any(part in {".", "..", ""} for part in candidate.parts) or _excluded(candidate)):
        raise SandboxViolation("Path outside grant or excluded by state/credential policy")
    return candidate


def _loader_dir_target() -> str:
    """The ``/lib64`` symlink target that resolves to this host's dynamic loader.

    Every binary the sandbox execs is dynamically linked, so the kernel resolves
    its interpreter path inside the sandbox root before the program runs. Arch
    keeps the loader in ``/usr/lib`` and makes ``/usr/lib64`` a symlink to it;
    Debian and Ubuntu keep ``/usr/lib64`` as a real directory whose
    ``ld-linux-<arch>.so.<n>`` points into ``/usr/lib/<multiarch>``. ``/usr`` is
    bound wholesale, so reproducing whichever directory this host actually uses
    works on both.

    Getting this wrong fails every sandboxed command with ``execvp <path>: No
    such file or directory``, which reads like a missing binary rather than an
    unresolvable interpreter (found on Debian/Ubuntu, 2026-09-27).
    """
    for candidate in ("usr/lib64", "usr/lib"):
        try:
            if any((Path("/") / candidate).glob("ld-linux*")):
                return candidate
        except OSError:
            continue
    return "usr/lib"


@dataclass(frozen=True)
class ExecutionSpec:
    """One isolated execution, described without reference to how it is isolated.

    Paths on the left of each mount are host paths prepared by trusted code;
    paths on the right are where the command sees them. The backend must give
    the command nothing else: no host environment, no other files, and no
    network unless ``network`` is true.
    """
    argv: tuple[str, ...]
    workdir: str
    readonly_mounts: tuple[tuple[str, str], ...]
    writable_mounts: tuple[tuple[str, str], ...]
    environment: tuple[tuple[str, str], ...]
    network: bool
    timeout: float
    address_space: int | None = None
    cpu_seconds: int = 60
    file_size: int | None = 8_388_608
    open_files: int = 128
    processes: int | None = 32
    monitor_scratch: str | None = None


class ExecutionBackend(Protocol):
    """Isolation seam: Bubblewrap here, or E2B microVMs (``walter.sandbox_e2b``)."""

    name: str

    def run(self, spec: ExecutionSpec) -> CommandResult: ...


class BubblewrapBackend:
    """Fail-closed local isolation: namespaces, dropped capabilities, a cleared
    environment, prlimit resource caps, a network-deny seccomp filter when the
    spec forbids network, and a monitor for wall time, process count, aggregate
    RSS and scratch usage."""

    name = "bubblewrap"

    def __init__(self, network_filter):
        self._network_filter = network_filter

    def run(self, spec: ExecutionSpec) -> CommandResult:
        if not Path("/usr/bin/bwrap").is_file():
            raise SandboxUnavailable("bubblewrap unavailable; install/fix /usr/bin/bwrap; host fallback prohibited")
        command = ["/usr/bin/bwrap", "--unshare-user", "--unshare-pid", "--unshare-ipc",
                   "--unshare-uts", "--unshare-cgroup-try", "--die-with-parent", "--new-session",
                   "--cap-drop", "ALL", "--ro-bind", "/usr", "/usr", "--symlink", "usr/bin", "/bin",
                   "--symlink", "usr/lib", "/lib", "--symlink", _loader_dir_target(), "/lib64",
                   "--proc", "/proc", "--dev", "/dev"]
        if not any(target == "/tmp" for _, target in spec.writable_mounts):
            command += ["--tmpfs", "/tmp"]
        for source, target in spec.writable_mounts:
            command += ["--bind", source, target]
        for source, target in spec.readonly_mounts:
            command += ["--ro-bind", source, target]
        command += ["--chdir", spec.workdir, "--clearenv"]
        for name, value in spec.environment:
            command += ["--setenv", name, value]
        limits = ["/usr/bin/prlimit", f"--cpu={spec.cpu_seconds}", f"--nofile={spec.open_files}"]
        if spec.address_space is not None:
            limits.append(f"--as={spec.address_space}")
        if spec.file_size is not None:
            limits.append(f"--fsize={spec.file_size}")
        if spec.processes is not None:
            limits.append(f"--nproc={spec.processes}")
        with (self._network_filter() if not spec.network else _no_filter()) as network_filter:
            pass_fds: tuple[int, ...] = ()
            if network_filter is not None:
                command += ["--seccomp", str(network_filter.fileno())]
                pass_fds = (network_filter.fileno(),)
            command += ["--", *limits, "--", *spec.argv]
            with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
                proc = subprocess.Popen(command, stdout=out, stderr=err, start_new_session=True,
                                        env={"PATH": "/usr/bin:/bin"}, pass_fds=pass_fds)
                deadline = time.monotonic() + spec.timeout
                violation = None
                scratch = Path(spec.monitor_scratch) if spec.monitor_scratch else None
                while proc.poll() is None:
                    if time.monotonic() >= deadline:
                        violation = "Sandbox command exceeded wall-time budget"; break
                    if scratch is not None:
                        processes, aggregate_rss, storage = WorkspaceManager._usage(proc.pid, scratch)
                        if processes > MAX_PROCESSES:
                            violation = "Sandbox aggregate process limit exceeded"; break
                        if aggregate_rss > MAX_AGGREGATE_RSS:
                            violation = "Sandbox aggregate memory limit exceeded"; break
                        if storage > MAX_SCRATCH_BYTES:
                            violation = "Sandbox aggregate scratch-storage limit exceeded"; break
                    time.sleep(0.02)
                if violation:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                proc.wait()
                out.seek(0); err.seek(0)
                stdout = out.read(MAX_OUTPUT_BYTES).decode(errors="replace")
                stderr = err.read(MAX_OUTPUT_BYTES).decode(errors="replace")
        if violation:
            raise SandboxViolation(violation)
        if proc.returncode and stderr.startswith("bwrap:"):
            raise SandboxUnavailable(
                "bubblewrap isolation backend failed; verify user namespaces/outer sandbox: " + stderr.strip())
        return CommandResult(proc.returncode, stdout, stderr)


@contextmanager
def _no_filter():
    yield None


class WorkspaceManager:
    def __init__(self, repository: str | Path, state_root: str | Path | None = None,
                 dependency_root: str | Path | None = None,
                 approval_verifier: SafetyApprovalVerifier | None = None,
                 node_root: str | Path | None = None,
                 backend: ExecutionBackend | None = None):
        self.repository = Path(repository).resolve(strict=True)
        self.backend = backend or BubblewrapBackend(self._network_filter)
        self.node_root = Path(node_root).resolve() if node_root is not None else _detect_node_root()
        self.state_root = Path(state_root or self.repository / ".local/sandboxes").absolute()
        if not self.state_root.is_relative_to(self.repository):
            raise SandboxViolation("State must remain inside this repository")
        self.state_root.mkdir(parents=True, exist_ok=True)
        if self.state_root.resolve() != self.state_root:
            raise SandboxViolation("Symlinked state root")
        self.dependency_root = self._dependency_root(dependency_root)
        # This capability is intentionally memory-only: workers cannot recover it
        # from a grant or the signed manifest and absent injection means deny.
        self._approval_verifier = approval_verifier
        self._lock = threading.RLock()
        self._grants: dict[str, WorkspaceGrant] = {}
        self._consumed_safety_approvals: dict[str, str] = {}
        self._manifest = self.state_root / "grants.json"
        self._key_path = self.state_root / "manifest.key"
        self._key = self._load_key()
        if self._manifest.exists():
            self._load_manifest()

    def _dependency_root(self, configured: str | Path | None) -> Path:
        choices = [Path(configured)] if configured is not None else [Path(sys.prefix)]
        if configured is None:
            choices.append(Path(__file__).resolve().parents[2] / ".venv")
        for choice in choices:
            try:
                root = choice.resolve(strict=True)
            except FileNotFoundError:
                continue
            if (root / "bin/python").exists() and root != Path("/usr"):
                return root
        # The system interpreter is sufficient for stdlib checks, but provider/test
        # dependencies will correctly fail rather than borrowing writable host state.
        return Path("/usr")

    def _load_key(self) -> bytes:
        if self._key_path.exists():
            info = self._key_path.stat()
            if info.st_mode & 0o077 or not stat.S_ISREG(info.st_mode):
                raise SandboxViolation("Unsafe workspace manifest key permissions")
            key = self._key_path.read_bytes()
            if len(key) != 32:
                raise SandboxViolation("Invalid workspace manifest key")
            return key
        if self._manifest.exists():
            raise SandboxViolation("Workspace manifest key missing")
        fd = os.open(self._key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        key = os.urandom(32)
        with os.fdopen(fd, "wb") as stream:
            stream.write(key)
            stream.flush()
            os.fsync(stream.fileno())
        return key

    def _signed_payload(self) -> bytes:
        records = [asdict(self._grants[key]) for key in sorted(self._grants)]
        state = {"grants": records, "consumed_safety_approvals":
                 dict(sorted(self._consumed_safety_approvals.items()))}
        return json.dumps(state, sort_keys=True, separators=(",", ":")).encode()

    def _load_manifest(self):
        try:
            envelope = json.loads(self._manifest.read_text())
            payload = envelope["payload"].encode()
            signature = envelope["hmac"]
            expected = hmac.new(self._key, payload, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                raise SandboxViolation("Workspace grant manifest authentication failed")
            state = json.loads(payload)
            # Read manifests created before the consumed-approval ledger existed.
            records = state if isinstance(state, list) else state["grants"]
            consumed = {} if isinstance(state, list) else state["consumed_safety_approvals"]
            if (not isinstance(consumed, dict) or any(
                    not isinstance(key, str) or not isinstance(value, str)
                    for key, value in consumed.items())):
                raise SandboxViolation("Invalid consumed safety-approval ledger")
            self._consumed_safety_approvals = consumed
            for raw in records:
                raw["prohibited_roots"] = tuple(raw["prohibited_roots"])
                raw["command_categories"] = tuple(raw["command_categories"])
                raw["safety_allowed_paths"] = tuple(raw.get("safety_allowed_paths", ()))
                grant = WorkspaceGrant(**raw)
                if grant.id in self._grants:
                    raise SandboxViolation("Duplicate workspace grant")
                self._grants[grant.id] = grant
            # Structural binding mismatches indicate tampering and must always raise,
            # before environmental reconciliation can mask them.
            for grant in self._grants.values():
                if grant.lifecycle == "active":
                    self._verify_grant_shape(grant)
                if (grant.allow_safety_changes and
                        self._consumed_safety_approvals.get(grant.safety_approval_id or "") !=
                        grant.safety_approval_digest):
                    raise SandboxViolation("Safety grant is missing its consumed approval record")
            # A signed grant that no longer matches this host environment is stale,
            # not tampered: close it so a removed worktree or dependency environment
            # cannot block manager construction.
            reconciled = False
            for ident, grant in list(self._grants.items()):
                if grant.lifecycle != "active":
                    continue
                reason = self._stale_reason(grant)
                if reason is None:
                    continue
                self._grants[ident] = replace(grant, lifecycle="closed")
                reconciled = True
                logger.warning("Closing stale workspace grant %s: %s", ident, reason)
            if reconciled:
                self._save()
        except SandboxViolation:
            raise
        except Exception as exc:
            raise SandboxViolation("Invalid workspace grant manifest") from exc

    def _save(self):
        payload = self._signed_payload()
        envelope = {"payload": payload.decode(),
                    "hmac": hmac.new(self._key, payload, hashlib.sha256).hexdigest()}
        temporary = self._manifest.with_suffix(".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(envelope, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(self._manifest)

    def _git_result(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["/usr/bin/git", "-c", "core.hooksPath=/dev/null",
            "-c", "core.fsmonitor=false", "-c", "diff.external=", *args],
            env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent",
                 "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
                 "GIT_TERMINAL_PROMPT": "0"}, capture_output=True, text=True, timeout=30)

    def _git(self, *args: str) -> str:
        result = self._git_result(*args)
        if result.returncode:
            raise SandboxViolation(result.stderr.strip() or "Git operation denied")
        return result.stdout

    def create_candidate(self, run_id: str, task_id: str, worker_id: str,
                         command_categories: tuple[str, ...] | None = None, *,
                         base_revision: str | None = None) -> WorkspaceGrant:
        if base_revision is not None and self._git(
                "-C", str(self.repository), "cat-file", "-t", base_revision).strip() != "commit":
            raise SandboxViolation("Candidate base must be a commit")
        return self._create_candidate(run_id, task_id, worker_id, command_categories,
                                      allow_safety_changes=False, safety_approval_id=None,
                                      safety_approval_digest=None, safety_allowed_paths=(),
                                      safety_operation=None, base_revision=base_revision)

    # Integration ---------------------------------------------------------

    def integration_head(self, *, create: bool = False) -> str | None:
        """Current integration commit; optionally start the branch at HEAD."""
        with self._lock:
            found = self._git_result("-C", str(self.repository), "rev-parse", "--verify",
                                     "--quiet", INTEGRATION_REF + "^{commit}")
            if found.returncode == 0:
                return found.stdout.strip()
            if not create:
                return None
            head = self._git("-C", str(self.repository), "rev-parse", "HEAD").strip()
            # Empty old value: create only if the ref does not exist yet.
            self._git("-C", str(self.repository), "update-ref", INTEGRATION_REF, head, "")
            return head

    def tracked_files(self, revision: str | None = None) -> list[str]:
        """Policy-visible files at ``revision`` (default: integration head, else HEAD)."""
        with self._lock:
            if revision is None:
                found = self._git_result("-C", str(self.repository), "rev-parse", "--verify",
                                         "--quiet", INTEGRATION_REF + "^{commit}")
                revision = found.stdout.strip() if found.returncode == 0 else "HEAD"
            listing = self._git_result("-C", str(self.repository), "-c", "core.quotePath=false",
                                       "ls-tree", "-r", "-z", "--name-only", "--full-tree", revision)
        if listing.returncode:
            return []
        return sorted(path for path in listing.stdout.split("\0") if path and not _excluded(path))

    def _commit_candidate(self, grant: WorkspaceGrant, message: str) -> str:
        """Commit exactly the candidate's policy-visible inventory onto its base.

        Idempotent: a candidate already committed on its base is reused after
        checking that nothing outside that commit is pending.
        """
        root = grant.root
        inventory = self._inventory(grant, contents=False)
        baseline = {path for path in self._git("-C", root, "-c", "core.quotePath=false", "ls-tree", "-r", "--name-only",
                                               grant.base_revision).splitlines() if not _excluded(path)}
        current = self._git("-C", root, "rev-parse", "HEAD").strip()
        if current == grant.base_revision:
            modified = {path for path in self._git("-C", root, "-c", "core.quotePath=false", "diff", "--name-only",
                                                   grant.base_revision).splitlines() if not _excluded(path)}
            changed = sorted({name for name in inventory if name not in baseline} |
                             {name for name in modified if name in inventory})
            deleted = sorted(baseline - set(inventory))
            if not changed and not deleted:
                return current
            for start in range(0, len(changed), 200):
                self._git("-C", root, "add", "--force", "--", *changed[start:start + 200])
            for start in range(0, len(deleted), 200):
                self._git("-C", root, "rm", "-q", "--cached", "--ignore-unmatch", "--",
                          *deleted[start:start + 200])
            self._git("-C", root, "-c", "user.name=Cavman", "-c", "user.email=cavman@localhost",
                      "commit", "-q", "--no-verify", "--no-gpg-sign", "-m", message)
            current = self._git("-C", root, "rev-parse", "HEAD").strip()
        parents = self._git("-C", root, "rev-list", "--parents", "-n", "1", current).split()
        if parents[1:] != [grant.base_revision]:
            raise SandboxViolation("Candidate history does not sit directly on its base")
        committed = {path for path in self._git("-C", root, "-c", "core.quotePath=false", "ls-tree", "-r", "--name-only",
                                                current).splitlines() if not _excluded(path)}
        pending = [line for line in self._git("-C", root, "-c", "core.quotePath=false", "status", "--porcelain",
                                              "--untracked-files=all").splitlines()
                   if len(line) >= 4 and not _excluded(line[3:])]
        if committed != set(inventory) or pending:
            raise SandboxViolation("Candidate commit does not match the candidate inventory")
        return current

    def integrate(self, workspace_id: str, expected_fingerprint: str, message: str) -> str:
        """Fast-forward the integration branch to exactly the accepted candidate.

        The candidate must have been built on the current integration head, so
        the integrated tree is byte-for-byte the tree that was validated and
        reviewed. A moved head raises IntegrationStale instead of merging.
        """
        with self._lock:
            grant = self._get(workspace_id)
            if self.fingerprint(workspace_id) != expected_fingerprint:
                raise SandboxViolation("Candidate changed after acceptance; refusing to integrate")
            head = self.integration_head(create=True)
            if grant.base_revision != head:
                raise IntegrationStale("Integration moved since this candidate was created")
            commit = self._commit_candidate(grant, message)
            if commit == head:
                raise SandboxViolation("Candidate has no changes to integrate")
            # Compare-and-swap: never overwrite a concurrent integration.
            self._git("-C", str(self.repository), "update-ref", INTEGRATION_REF, commit, head)
            return commit

    def carry_over(self, source_workspace_id: str, target_workspace_id: str) -> bool:
        """Replay a previous attempt's changes onto a fresh candidate, uncommitted.

        Returns False, leaving the target clean, when there was nothing to carry
        or the changes conflict with the target's newer base.
        """
        with self._lock:
            source = self._grants.get(source_workspace_id)
            if not source or source.lifecycle != "active":
                return False
            self._verify_worktree(source)
            target = self._get(target_workspace_id)
            if target.read_only:
                raise SandboxViolation("Cannot carry changes into a read-only grant")
            commit = self._commit_candidate(source, "Previous attempt (carried over)")
            if commit == source.base_revision:
                return False
            applied = self._git_result("-C", target.root, "-c", "user.name=Cavman",
                                       "-c", "user.email=cavman@localhost",
                                       "cherry-pick", "--no-commit", commit)
            if applied.returncode:
                self._git_result("-C", target.root, "cherry-pick", "--abort")
                self._git("-C", target.root, "reset", "-q", "--hard", target.base_revision)
                self._git("-C", target.root, "clean", "-fdq")
                return False
            # Leave the replay as working-tree changes, like any fresh edit.
            self._git("-C", target.root, "reset", "-q", "--mixed", target.base_revision)
            self._inventory(target, contents=False)  # re-validate object policy
            return True

    def create_safety_candidate(self, run_id: str, task_id: str, worker_id: str,
                                approval_id: str, allowed_paths: tuple[str, ...],
                                intended_operation: str,
                                command_categories: tuple[str, ...] | None = None) -> WorkspaceGrant:
        """Consume one exact core-approved request and create its bounded candidate.

        RESERVED FACILITY (2026-09-21 decision): no Manager tool, CLI command, or
        construction site injects an approval_verifier, so in the shipped runtime
        this method can only deny. Candidates therefore cannot mutate SAFETY_PATHS
        at all — a deliberately stricter posture than the designed flow. Do not
        wire this to a Manager tool without a fresh security design review; when
        that review happens, doctrine/TOOLS.md and this note must be updated together.
        """
        with self._lock:
            if self._approval_verifier is None:
                raise SandboxViolation("Safety changes require a Manager approval verifier")
            if not approval_id or not run_id or not task_id or not worker_id:
                raise SandboxViolation("Safety approval identity is incomplete")
            if intended_operation not in {"write", "delete"}:
                raise SandboxViolation("Safety approval operation must be write or delete")
            canonical_paths = tuple(sorted(set(allowed_paths)))
            if (not canonical_paths or len(canonical_paths) != len(allowed_paths) or
                    any(path not in SAFETY_PATHS for path in canonical_paths)):
                raise SandboxViolation("Safety approval paths must be exact protected paths")
            if approval_id in self._consumed_safety_approvals:
                raise SandboxViolation("Safety approval was already consumed")
            base = self._git("-C", str(self.repository), "rev-parse", "HEAD").strip()
            scope: dict[str, object] = {
                "action": "safety_boundary_change",
                "category": "safety_boundary_change",
                "run_id": run_id,
                "task_id": task_id,
                "worker_id": worker_id,
                "repository": str(self.repository),
                "base_commit": base,
                "allowed_paths": list(canonical_paths),
                "operation": intended_operation,
            }
            canonical = json.dumps(scope, sort_keys=True, separators=(",", ":"), allow_nan=False)
            digest = hashlib.sha256(canonical.encode()).hexdigest()
            try:
                verification = self._approval_verifier(
                    run_id, approval_id, "safety_boundary_change", scope)
            except Exception as exc:
                raise SandboxViolation("Exact scoped human approval required") from exc
            if (not isinstance(verification, SafetyApprovalVerification) or
                    verification.approval_id != approval_id or
                    verification.scope_digest != digest or
                    verification.category != "safety_boundary_change" or
                    not verification.human_id.strip() or
                    verification.human_id in ({worker_id} |
                        {actor for grant in self._grants.values()
                         for actor in (grant.author_id, grant.worker_id)})):
                raise SandboxViolation("Invalid safety approval verification")
            grant = self._create_candidate(
                run_id, task_id, worker_id, command_categories,
                allow_safety_changes=True, safety_approval_id=approval_id,
                safety_approval_digest=digest, safety_allowed_paths=canonical_paths,
                safety_operation=intended_operation, base_revision=base, persist=False)
            self._consumed_safety_approvals[approval_id] = digest
            self._save()
            self._verify_worktree(grant)
            return grant

    def _create_candidate(self, run_id: str, task_id: str, worker_id: str,
                          command_categories: tuple[str, ...] | None, *,
                          allow_safety_changes: bool,
                          safety_approval_id: str | None,
                          safety_approval_digest: str | None,
                          safety_allowed_paths: tuple[str, ...],
                          safety_operation: str | None,
                          base_revision: str | None = None,
                          persist: bool = True) -> WorkspaceGrant:
        with self._lock:
            ident = uuid.uuid4().hex
            root = self.state_root / ("candidate-" + ident)
            branch = "walter-candidate/" + ident
            base = base_revision or self._git(
                "-C", str(self.repository), "rev-parse", "HEAD").strip()
            self._git("-C", str(self.repository), "worktree", "add", "-b", branch, str(root), base)
            grant = WorkspaceGrant(
                id=ident, candidate_id=ident, run_id=run_id, task_id=task_id,
                worker_id=worker_id, author_id=worker_id, root=str(root),
                repository=str(self.repository), branch=branch, base_revision=base,
                dependency_root=str(self.dependency_root),
                prohibited_roots=(str(self.repository),),
                command_categories=command_categories or WorkspaceGrant.__dataclass_fields__["command_categories"].default,
                allow_safety_changes=allow_safety_changes,
                safety_approval_id=safety_approval_id,
                safety_approval_digest=safety_approval_digest,
                safety_allowed_paths=safety_allowed_paths,
                safety_operation=safety_operation,
            )
            self._grants[ident] = grant
            if persist:
                self._save()
                self._verify_worktree(grant)
            return grant

    def mark_used(self, workspace_id: str) -> None:
        """Record that an attempt has begun against this workspace. Idempotent.

        Called by trusted adapter code when a delegation starts, never by the
        worker. A used workspace can no longer be mistaken for the exact,
        untouched candidate a capability escalation approved.
        """
        with self._lock:
            grant = self._get(workspace_id)
            if not grant.used:
                self._grants[grant.id] = replace(grant, used=True)
                self._save()

    def reviewer_grant(self, workspace_id: str, worker_id: str) -> WorkspaceGrant:
        with self._lock:
            original = self._get(workspace_id)
            if worker_id == original.author_id:
                raise SandboxViolation("Reviewer must differ from original author")
            grant = replace(original, id=uuid.uuid4().hex, worker_id=worker_id, read_only=True)
            self._grants[grant.id] = grant
            self._save()
            return grant

    def _verify_grant_shape(self, grant: WorkspaceGrant):
        expected_root = self.state_root / ("candidate-" + grant.candidate_id)
        if (grant.repository != str(self.repository) or grant.root != str(expected_root) or
                grant.branch != "walter-candidate/" + grant.candidate_id or
                not grant.run_id or not grant.task_id or not grant.worker_id or not grant.author_id or
                grant.prohibited_roots != (str(self.repository),)):
            raise SandboxViolation("Workspace grant binding is invalid")
        safety_bound = bool(grant.safety_approval_id and grant.safety_approval_digest and
                            grant.safety_allowed_paths and grant.safety_operation)
        if grant.allow_safety_changes != safety_bound:
            raise SandboxViolation("Safety-change authority is not bound to an approval scope")
        if grant.safety_approval_digest and (
                len(grant.safety_approval_digest) != 64 or
                any(character not in "0123456789abcdef"
                    for character in grant.safety_approval_digest)):
            raise SandboxViolation("Invalid safety approval scope digest")
        if grant.allow_safety_changes and (
                grant.safety_operation not in {"write", "delete"} or
                tuple(sorted(set(grant.safety_allowed_paths))) != grant.safety_allowed_paths or
                any(path not in SAFETY_PATHS for path in grant.safety_allowed_paths)):
            raise SandboxViolation("Invalid safety approval mutation scope")
        root = Path(grant.root)
        if root.parent != self.state_root or root.resolve() != root:
            raise SandboxViolation("Invalid workspace root")

    def _stale_reason(self, grant: WorkspaceGrant) -> str | None:
        """Describe why an active grant no longer matches this host, or None.

        Only environmental drift is reported here; structural/binding mismatches
        are integrity violations and remain the strict shape check's concern.
        """
        if not Path(grant.root).is_dir():
            return "candidate worktree is missing"
        if grant.dependency_root != str(self.dependency_root):
            return "dependency root changed"
        if not (Path(grant.dependency_root) / "bin/python").exists():
            return "dependency environment is missing"
        return None

    def _verify_worktree(self, grant: WorkspaceGrant):
        self._verify_grant_shape(grant)
        root = Path(grant.root)
        if not root.is_dir():
            raise SandboxViolation("Candidate worktree is missing")
        top = self._git("-C", grant.root, "rev-parse", "--show-toplevel").strip()
        branch = self._git("-C", grant.root, "branch", "--show-current").strip()
        base_type = self._git("-C", str(self.repository), "cat-file", "-t", grant.base_revision).strip()
        worktrees = self._git("-C", str(self.repository), "worktree", "list", "--porcelain")
        expected = f"worktree {grant.root}\n"
        if top != grant.root or branch != grant.branch or base_type != "commit" or expected not in worktrees:
            raise SandboxViolation("Workspace no longer matches its signed candidate binding")

    def _get(self, workspace_id: str, worker_id: str | None = None) -> WorkspaceGrant:
        grant = self._grants.get(workspace_id)
        if not grant or grant.lifecycle != "active":
            raise SandboxViolation("Unknown or inactive workspace")
        if worker_id is not None and grant.worker_id != worker_id:
            raise SandboxViolation("Workspace belongs to a different worker")
        self._verify_worktree(grant)
        return grant

    @contextmanager
    def _parent(self, grant: WorkspaceGrant, path: str, create: bool = False):
        parts = _safe_relative(path).parts
        fd = os.open(grant.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in parts[:-1]:
                if create:
                    try:
                        os.mkdir(part, dir_fd=fd)
                    except FileExistsError:
                        pass
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = next_fd
            yield fd, parts[-1]
        except OSError as exc:
            raise SandboxViolation(str(exc)) from exc
        finally:
            os.close(fd)

    def _read_bytes(self, workspace_id: str, path: str, *, worker_id: str | None = None) -> bytes:
        with self._lock:
            grant = self._get(workspace_id, worker_id)
            with self._parent(grant, path) as (parent, name):
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
                with os.fdopen(fd, "rb") as stream:
                    info = os.fstat(stream.fileno())
                    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
                            info.st_size > MAX_FILE_BYTES):
                        raise SandboxViolation("Only bounded regular files with one link are accessible")
                    return stream.read()

    def read_file(self, workspace_id: str, path: str, *, worker_id: str | None = None) -> str:
        return self._read_bytes(workspace_id, path, worker_id=worker_id).decode()

    def _inventory(self, grant: WorkspaceGrant, *, contents: bool = True) -> dict[str, tuple[str, int, bytes]]:
        """Walk the candidate, enforcing object policy and optionally reading bytes.

        Identity, diff, and snapshot need file bytes; listing and status only
        need names and policy checks. Reading every byte of a large repository
        to answer "what files exist" wasted memory and time, so callers that do
        not need contents pass contents=False and get empty payloads with the
        same safety validation (2026-09-21 decision).
        """
        inventory: dict[str, tuple[str, int, bytes]] = {}
        for base, dirs, files in os.walk(grant.root, followlinks=False):
            relative_base = Path(base).relative_to(grant.root)
            for name in dirs:
                item = Path(base, name)
                if item.is_symlink():
                    relative = item.relative_to(grant.root).as_posix()
                    raise SandboxViolation(f"Candidate symlink is prohibited: {relative}")
            dirs[:] = [name for name in dirs
                       if not _excluded(PurePosixPath(relative_base.as_posix(), name))]
            for name in files:
                item = Path(base, name)
                relative = item.relative_to(grant.root).as_posix()
                if _excluded(relative):
                    continue
                info = item.lstat()
                mode = stat.S_IMODE(info.st_mode)
                if stat.S_ISLNK(info.st_mode):
                    raise SandboxViolation(f"Candidate symlink is prohibited: {relative}")
                elif stat.S_ISREG(info.st_mode):
                    if info.st_nlink != 1 or info.st_size > MAX_FILE_BYTES:
                        raise SandboxViolation(f"Unsafe candidate file: {relative}")
                    inventory[relative] = ("file", mode, item.read_bytes() if contents else b"")
                else:
                    raise SandboxViolation(f"Unsupported candidate object: {relative}")
                if len(inventory) > MAX_FILES:
                    raise SandboxViolation("Candidate file count exceeded")
        return inventory

    def fingerprint(self, workspace_id: str) -> str:
        with self._lock:
            grant = self._get(workspace_id)
            inventory = self._inventory(grant)
            baseline = set(self._git("-C", grant.root, "ls-tree", "-r", "--name-only",
                                     grant.base_revision).splitlines())
            digest = hashlib.sha256()
            digest.update(b"base\0" + grant.base_revision.encode() + b"\0")
            for name in sorted(inventory):
                kind, mode, data = inventory[name]
                digest.update(name.encode() + b"\0" + kind.encode() + b"\0" +
                              oct(mode).encode() + b"\0" + str(len(data)).encode() + b"\0" + data)
            for name in sorted(path for path in baseline if not _excluded(path) and path not in inventory):
                digest.update(name.encode() + b"\0deleted\0")
            return digest.hexdigest()

    def freeze(self, workspace_id: str) -> str:
        with self._lock:
            grant = self._get(workspace_id)
            for ident, other in list(self._grants.items()):
                if other.candidate_id == grant.candidate_id:
                    self._grants[ident] = replace(other, read_only=True)
            self._save()
            return self.fingerprint(workspace_id)

    def write_file(self, workspace_id: str, path: str, content: str, *, worker_id: str | None = None):
        with self._lock:
            grant = self._get(workspace_id, worker_id)
            relative = _safe_relative(path)
            data = content.encode()
            if (grant.read_only or len(data) > MAX_FILE_BYTES or
                    (grant.allow_safety_changes and (
                        grant.safety_operation != "write" or
                        relative.as_posix() not in grant.safety_allowed_paths)) or
                    (_safety_path(relative) and not grant.allow_safety_changes)):
                raise SandboxViolation("Write denied")
            with self._parent(grant, relative.as_posix(), create=True) as (parent, name):
                fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                             0o600, dir_fd=parent)
                with os.fdopen(fd, "wb") as stream:
                    info = os.fstat(stream.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        raise SandboxViolation("Unsafe write target")
                    os.ftruncate(stream.fileno(), 0)
                    stream.write(data)

    def delete_file(self, workspace_id: str, path: str, *, worker_id: str | None = None):
        with self._lock:
            grant = self._get(workspace_id, worker_id)
            relative = _safe_relative(path)
            if (grant.read_only or
                    (grant.allow_safety_changes and (
                        grant.safety_operation != "delete" or
                        relative.as_posix() not in grant.safety_allowed_paths)) or
                    (_safety_path(relative) and not grant.allow_safety_changes)):
                raise SandboxViolation("Read-only grant")
            with self._parent(grant, relative.as_posix()) as (parent, name):
                info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise SandboxViolation("Unsafe deletion target")
                os.unlink(name, dir_fd=parent)

    def list_files(self, workspace_id: str, *, worker_id: str | None = None) -> list[str]:
        grant = self._get(workspace_id, worker_id)
        return sorted(name for name, (kind, _, _) in self._inventory(grant, contents=False).items()
                      if kind == "file")

    def changed_paths(self, workspace_id: str, *, worker_id: str | None = None) -> list[str]:
        """Return the candidate's added/modified relative paths after grant validation.

        Additions are inventory names absent from the signed base tree; modifications
        are tracked paths reported by ``git diff --name-only`` against the base
        revision that still exist in the inventory.  Read-only.
        """
        with self._lock:
            grant = self._get(workspace_id, worker_id)
            inventory = self._inventory(grant, contents=False)
            baseline = {path for path in self._git(
                "-C", grant.root, "ls-tree", "-r", "--name-only",
                grant.base_revision).splitlines() if not _excluded(path)}
            modified = {path for path in self._git(
                "-C", grant.root, "diff", "--name-only",
                grant.base_revision).splitlines() if not _excluded(path)}
            changed = {name for name in inventory if name not in baseline}
            changed.update(name for name in modified if name in inventory)
            return sorted(changed)

    def inspect_grant(self, workspace_id: str, *,
                      worker_id: str | None = None) -> WorkspaceGrant:
        """Return an immutable copy after validating grant, worker, and worktree binding."""
        with self._lock:
            return replace(self._get(workspace_id, worker_id))

    def status(self, workspace_id: str, *, worker_id: str | None = None) -> str:
        grant = self._get(workspace_id, worker_id)
        self._inventory(grant, contents=False)
        lines = self._git("-C", grant.root, "status", "--short", "--untracked-files=all").splitlines()
        return "\n".join(line for line in lines if len(line) >= 4 and not _excluded(line[3:]))

    def diff(self, workspace_id: str, *, worker_id: str | None = None) -> str:
        grant = self._get(workspace_id, worker_id)
        inventory = self._inventory(grant)
        baseline = set(self._git("-C", grant.root, "ls-tree", "-r", "--name-only",
                                 grant.base_revision).splitlines())
        tracked = sorted((baseline | set(inventory)) & baseline -
                         {path for path in baseline if _excluded(path)})
        tracked_diff = self._git("-C", grant.root, "diff", "--binary", "--no-ext-diff",
                                 "--no-textconv", grant.base_revision, "--", *tracked) if tracked else ""
        additions = []
        for name in sorted(set(inventory) - baseline):
            kind, mode, data = inventory[name]
            if kind == "file":
                try:
                    content = data.decode()
                except UnicodeDecodeError:
                    content = f"Binary candidate sha256={hashlib.sha256(data).hexdigest()}"
                additions.append(f"diff --git a/{name} b/{name}\nnew file mode {mode:06o}\n"
                                 f"--- /dev/null\n+++ b/{name}\n@@ candidate @@\n{content}")
            else:
                additions.append(f"diff --git a/{name} b/{name}\nnew symlink mode {mode:06o}\n"
                                 f"target {data.decode(errors='replace')}\n")
        return tracked_diff + ("\n" if tracked_diff and additions else "") + "\n".join(additions)

    def _command(self, grant: WorkspaceGrant, category: str, argv: list[str]) -> list[str]:
        if category not in grant.command_categories or not argv:
            raise SandboxViolation("Command category denied")
        python = "/opt/walter-env/bin/python"
        executable = Path(argv[0]).name
        rest = argv[1:]
        if category == "isolation_probe" and argv == ["sandbox-probe"]:
            return [python, "-c", ISOLATION_PROBE]
        if argv[0] in {"node", "tsc", "npm"}:
            if self.node_root is None:
                raise SandboxUnavailable("Node toolchain unavailable; host fallback prohibited")
            node = "/opt/node/bin/node"
            if category == "test" and argv[0] == "node" and len(rest) >= 2 and rest[0] == "--test":
                inventory = self._inventory(grant, contents=False)
                if not all(_node_test_path(path) and path in inventory for path in rest[1:]):
                    raise SandboxViolation("Node test arguments exceed the manager template")
                return [node, "--test", _node_isolation_flag(self.node_root), *rest[1:]]
            if category == "build" and argv == ["npm", "run", "build"]:
                return [node, "-e", NPM_BUILD]
            if category == "check" and argv == ["tsc"]:
                return [node, "/workspace/node_modules/typescript/bin/tsc", "--noEmit",
                        "--incremental", "false", "--pretty", "false", "-p", "/workspace"]
            raise SandboxViolation("Command does not match a manager-defined Node template")
        if executable not in {"python", "python3"}:
            raise SandboxViolation("Only the immutable Python environment and trusted Node templates are executable")
        if category in {"test", "check"} and len(rest) == 2 and rest[0] == "-c" and rest[1] in AST_CHECK_SNIPPETS:
            return [python, *rest]
        if category == "test" and len(rest) >= 2 and rest[:2] == ["-m", "pytest"]:
            allowed = {"-q", "-x", "--maxfail=1", "-p", "no:cacheprovider"}
            inventory = self._inventory(grant)
            for arg in rest[2:]:
                if arg in allowed:
                    continue
                if _python_source_path(arg) and arg in inventory:
                    continue
                raise SandboxViolation("Pytest arguments exceed the manager template")
            return [python, *rest]
        if category == "build" and len(rest) >= 3 and rest[:2] == ["-m", "py_compile"]:
            sources = rest[2:]
            if not all(_python_source_path(path) for path in sources):
                raise SandboxViolation("Build paths exceed the manager py_compile template")
            return [python, "-c", PY_COMPILE, *sources]
        raise SandboxViolation("Command does not match a manager-defined template")

    @staticmethod
    def _usage(root_pid: int, scratch: Path) -> tuple[int, int, int]:
        parent_map: dict[int, int] = {}
        rss: dict[int, int] = {}
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                fields = (entry / "stat").read_text().split()
                parent_map[int(entry.name)] = int(fields[3])
                status = (entry / "status").read_text().splitlines()
                value = next((line.split()[1] for line in status if line.startswith("VmRSS:")), "0")
                rss[int(entry.name)] = int(value) * 1024
            except (FileNotFoundError, ProcessLookupError, PermissionError, ValueError, StopIteration):
                continue
        descendants = {root_pid}
        changed = True
        while changed:
            changed = False
            for pid, parent in parent_map.items():
                if parent in descendants and pid not in descendants:
                    descendants.add(pid); changed = True
        storage = 0
        for base, _, files in os.walk(scratch):
            for name in files:
                try:
                    storage += Path(base, name).stat().st_size
                except FileNotFoundError:
                    pass
        return len(descendants), sum(rss.get(pid, 0) for pid in descendants), storage

    @contextmanager
    def _network_filter(self):
        """Build a classic BPF seccomp profile denying network syscalls.

        This gives the child deterministic network denial without relying on
        creation of a network namespace, which some nested sandbox runtimes
        prohibit even while allowing the other required namespaces.
        """
        try:
            library = ctypes.CDLL("libseccomp.so.2", use_errno=True)
            library.seccomp_init.argtypes = [ctypes.c_uint32]
            library.seccomp_init.restype = ctypes.c_void_p
            library.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
                                                 ctypes.c_int, ctypes.c_uint]
            library.seccomp_rule_add.restype = ctypes.c_int
            library.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
            library.seccomp_syscall_resolve_name.restype = ctypes.c_int
            library.seccomp_export_bpf.argtypes = [ctypes.c_void_p, ctypes.c_int]
            library.seccomp_export_bpf.restype = ctypes.c_int
            library.seccomp_release.argtypes = [ctypes.c_void_p]
        except (OSError, AttributeError) as exc:
            raise SandboxUnavailable("libseccomp unavailable; network-denied execution required") from exc
        context = library.seccomp_init(0x7FFF0000)  # SCMP_ACT_ALLOW
        if not context:
            raise SandboxUnavailable("Unable to initialize network seccomp profile")
        profile = tempfile.TemporaryFile()
        try:
            deny = 0x00050000 | errno.EPERM  # SCMP_ACT_ERRNO(EPERM)
            for name in NETWORK_SYSCALLS:
                syscall = library.seccomp_syscall_resolve_name(name)
                if syscall < 0 or library.seccomp_rule_add(context, deny, syscall, 0) != 0:
                    raise SandboxUnavailable(f"Unable to deny network syscall {name.decode()}")
            if library.seccomp_export_bpf(context, profile.fileno()) != 0:
                raise SandboxUnavailable("Unable to export network seccomp profile")
            profile.seek(0)
            yield profile
        finally:
            library.seccomp_release(context)
            profile.close()

    def run_command(self, workspace_id: str, category: str, argv: list[str], *,
                    worker_id: str | None = None, timeout: float = 30,
                    node_modules: Path | None = None) -> CommandResult:
        with self._lock:
            grant = self._get(workspace_id, worker_id)
            if not 0 < timeout <= 120:
                raise SandboxViolation("Command time budget denied")
            inner = self._command(grant, category, argv)
            dependency = Path(grant.dependency_root)
            if not (dependency / "bin/python").exists():
                raise SandboxUnavailable("Immutable Python dependency environment unavailable")
            with tempfile.TemporaryDirectory(dir=self.state_root, prefix="execution-") as temporary:
                temporary_root = Path(temporary)
                snapshot = temporary_root / "workspace"
                scratch = temporary_root / "scratch"
                snapshot.mkdir(); scratch.mkdir()
                inventory = self._inventory(grant)
                total = 0
                for name, (kind, mode, data) in inventory.items():
                    if kind != "file":
                        raise SandboxViolation(f"Snapshot refuses candidate symlink: {name}")
                    total += len(data)
                    if total > MAX_SNAPSHOT_BYTES:
                        raise SandboxViolation("Snapshot size exceeded")
                    target = snapshot / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
                    target.chmod(mode & 0o555 or 0o444)
                readonly = [(str(snapshot), "/workspace"), (str(dependency), "/opt/walter-env")]
                path_prefix = "/opt/walter-env/bin"
                node = inner[0].startswith("/opt/node/")
                if node:
                    readonly.append((str(self.node_root), "/opt/node"))
                    path_prefix = "/opt/node/bin:" + path_prefix
                    if node_modules is not None:
                        if not self._is_dependency_dir(node_modules):
                            raise SandboxViolation("Dependency directory is not a trusted install")
                        (snapshot / "node_modules").mkdir(exist_ok=True)
                        readonly.append((str(node_modules), "/workspace/node_modules"))
                environment = [("PATH", path_prefix + ":/usr/bin:/bin"), ("HOME", "/tmp"),
                               ("PYTHONPATH", "/workspace/src"), ("PYTHONDONTWRITEBYTECODE", "1")]
                if node:
                    environment.append(("NODE_OPTIONS", NODE_FLAGS))
                return self.backend.run(ExecutionSpec(
                    argv=tuple(inner), workdir="/workspace", readonly_mounts=tuple(readonly),
                    writable_mounts=((str(scratch), "/tmp"),), environment=tuple(environment),
                    network=False, timeout=timeout,
                    address_space=NODE_ADDRESS_SPACE if node else PYTHON_ADDRESS_SPACE,
                    monitor_scratch=str(scratch)))

    def _is_dependency_dir(self, path: Path) -> bool:
        root = (self.state_root / "node-deps").resolve()
        resolved = Path(path).resolve()
        return (resolved.name == "node_modules" and resolved.parent.parent == root
                and (resolved.parent / ".complete").is_file())

    def node_dependencies(self, workspace_id: str, *, worker_id: str | None = None) -> Path | None:
        """Install (or reuse) the candidate's npm dependencies; None without package.json.

        Runs npm in its own jail: network is available so the registry can be
        reached, but lifecycle scripts are disabled (no package code executes),
        the environment is cleared, and only the manifest, an npm cache and the
        Node runtime are visible. The result is cached by manifest digest and
        later mounted read-only into the network-denied execution jail.
        """
        with self._lock:
            grant = self._get(workspace_id, worker_id)
            inventory = self._inventory(grant)
            manifest = inventory.get("package.json")
            if manifest is None:
                return None
            if self.node_root is None:
                raise SandboxUnavailable("Node toolchain unavailable; host fallback prohibited")
            lock = inventory.get("package-lock.json")
            digest = hashlib.sha256(b"package.json\0" + manifest[2] + b"\0package-lock.json\0" +
                                    (lock[2] if lock else b"")).hexdigest()
            target = self.state_root / "node-deps" / digest
            if (target / ".complete").is_file():
                return target / "node_modules"
            cache = self.state_root / "npm-cache"
            cache.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=self.state_root, prefix="install-") as temporary:
                work = Path(temporary) / "work"
                work.mkdir()
                (work / "package.json").write_bytes(manifest[2])
                if lock is not None:
                    (work / "package-lock.json").write_bytes(lock[2])
                readonly = [(name, name) for name in
                            ("/etc/resolv.conf", "/etc/hosts", "/etc/nsswitch.conf", "/etc/ssl")
                            if Path(name).exists()]
                readonly.append((str(self.node_root), "/opt/node"))
                environment = [("PATH", "/opt/node/bin:/usr/bin:/bin"), ("HOME", "/tmp"),
                               ("npm_config_cache", "/npm-cache"), ("npm_config_update_notifier", "false"),
                               ("NODE_OPTIONS", NODE_FLAGS)]
                environment += [(name, os.environ[name]) for name in PROXY_ENVIRONMENT if os.environ.get(name)]
                extra_ca = os.environ.get("NODE_EXTRA_CA_CERTS")
                if extra_ca and Path(extra_ca).is_file():
                    readonly.append((extra_ca, "/etc/cavman-extra-ca.pem"))
                    environment.append(("NODE_EXTRA_CA_CERTS", "/etc/cavman-extra-ca.pem"))
                argv = ("/opt/node/bin/node", "/opt/node/lib/node_modules/npm/bin/npm-cli.js",
                        "ci" if lock is not None else "install", "--ignore-scripts", "--no-audit",
                        "--no-fund", f"--registry={NPM_REGISTRY}")
                completed = self.backend.run(ExecutionSpec(
                    argv=argv, workdir="/work", readonly_mounts=tuple(readonly),
                    writable_mounts=((str(work), "/work"), (str(cache), "/npm-cache")),
                    environment=tuple(environment), network=True, timeout=DEPENDENCY_INSTALL_SECONDS,
                    address_space=None, cpu_seconds=DEPENDENCY_INSTALL_SECONDS, file_size=None,
                    open_files=512, processes=None))
                if completed.returncode:
                    raise SandboxViolation("Dependency installation failed: "
                                           + (completed.stderr or completed.stdout).strip()[-1500:])
                installed = work / "node_modules"
                if not installed.is_dir():
                    installed.mkdir()
                size = 0
                for base, dirs, files in os.walk(installed, followlinks=False):
                    for name in files:
                        size += Path(base, name).lstat().st_size
                    if size > MAX_DEPENDENCY_BYTES:
                        raise SandboxViolation("Installed dependencies exceed the size limit")
                target.mkdir(parents=True, exist_ok=True)
                if not (target / "node_modules").exists():
                    installed.rename(target / "node_modules")
                (target / ".complete").write_text(digest)
            return target / "node_modules"

    def _retire_candidate(self, grant: WorkspaceGrant) -> dict[str, object]:
        """Remove one candidate's worktree and branch and close its grants.

        Every step is conditional so retirement is idempotent and survives a
        worktree that was removed out of band: such a candidate still leaves a
        branch and a prunable worktree registration behind. Only
        ``walter-candidate/<candidate_id>`` branches and worktrees under this
        manager's own state root are ever touched; the live checkout is not
        reachable from here.
        """
        root = Path(grant.root)
        record: dict[str, object] = {
            "candidate_id": grant.candidate_id, "branch": grant.branch,
            "worktree_removed": False, "branch_deleted": False,
        }
        if root.is_dir():
            self._git("-C", str(self.repository), "worktree", "remove", "--force", grant.root)
            record["worktree_removed"] = True
        # Drop administrative entries for worktrees whose directory is already
        # gone. Repository-wide, but it can only forget absent directories.
        self._git_result("-C", str(self.repository), "worktree", "prune")
        if self._git_result("-C", str(self.repository), "rev-parse", "--verify",
                            "refs/heads/" + grant.branch).returncode == 0:
            self._git("-C", str(self.repository), "branch", "-D", grant.branch)
            record["branch_deleted"] = True
        for ident, other in list(self._grants.items()):
            if other.candidate_id == grant.candidate_id:
                self._grants[ident] = replace(other, lifecycle="closed")
        return record

    def cleanup(self, workspace_id: str):
        with self._lock:
            grant = self._get(workspace_id)
            self._retire_candidate(grant)
            self._save()

    def retire_run(self, run_id: str) -> list[dict[str, object]]:
        """Retire every candidate recorded for one run. Idempotent.

        Covers candidates whose worktree is still present and ones already
        reconciled to closed because their worktree vanished, which would
        otherwise leave an orphan branch that no command reclaimed. A grant
        whose signed binding no longer holds is refused rather than acted on.
        """
        with self._lock:
            retired, seen = [], set()
            for grant in sorted(self._grants.values(), key=lambda item: item.candidate_id):
                if grant.run_id != run_id or grant.candidate_id in seen:
                    continue
                seen.add(grant.candidate_id)
                self._verify_grant_shape(grant)
                retired.append(self._retire_candidate(grant))
            if retired:
                self._save()
            return retired
