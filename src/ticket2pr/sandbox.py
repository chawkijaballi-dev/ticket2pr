"""OS-level isolation for code ticket2pr did not write.

Two kinds of untrusted processes run on the worker: the agent's Bash commands (sandboxed by
Claude Code itself) and the repository's test suite (sandboxed here). Both need the same
host tools, and both must **fail closed**: Claude Code silently runs Bash unsandboxed when its
dependencies are missing, so ticket2pr checks them before every job.

The test sandbox uses bubblewrap: fresh PID namespace (so ``/proc`` shows no other process
and no other environment), no network unless allowed, a read-only root filesystem, writable
workspace and private ``/tmp`` only, an empty ``/run`` (no host IPC sockets; with network,
only the resolver config is bound back, read-only), and the
service's home and data directories replaced by empty tmpfs mounts so credentials files are
out of reach.
"""

from __future__ import annotations

import ctypes
import logging
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from ticket2pr.config import Settings
from ticket2pr.errors import ConfigurationError, PolicyViolationError

logger = logging.getLogger(__name__)

# Claude Code's Bash sandbox needs bubblewrap + socat on Linux; the test sandbox needs
# bubblewrap. Other platforms have no test sandbox, so they need an isolated container.
_LINUX_SANDBOX_TOOLS = ("bwrap", "socat")
_PR_SET_DUMPABLE = 4
_PR_SET_CHILD_SUBREAPER = 36


def missing_sandbox_tools(platform: str = sys.platform) -> list[str]:
    """Return what is missing to sandbox the agent *and* the tests (empty when all is there)."""
    if platform != "linux":
        return [f"no supported sandbox on {platform} (Linux with bubblewrap is required)"]
    missing = [tool for tool in _LINUX_SANDBOX_TOOLS if shutil.which(tool) is None]
    if not missing and not _bwrap_works():
        missing.append("permission to create namespaces (bwrap cannot start here)")
    return missing


_bwrap_ok = False  # cached once true: the probe runs before every job, on the event loop


def _bwrap_works() -> bool:
    global _bwrap_ok  # noqa: PLW0603 - a process-wide memo
    if not _bwrap_ok:
        _bwrap_ok = _probe_bwrap()  # a failure is retried at the next job, not remembered
    return _bwrap_ok


def _probe_bwrap() -> bool:
    """Installed is not enough: an unprivileged container, for one, forbids user namespaces.

    Probes with the tests' own profile (fresh /proc included, which some containers refuse).
    """
    try:
        with tempfile.TemporaryDirectory(prefix="t2p-probe-") as workspace:
            argv = TestSandbox(hidden=()).wrap(["true"], Path(workspace))
            probe = subprocess.run(argv, capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return probe.returncode == 0


def ensure_sandbox_available() -> None:
    if missing := missing_sandbox_tools():
        raise ConfigurationError(
            f"the sandbox cannot start (missing: {', '.join(missing)}). Install them, or set "
            "T2P_SANDBOX=false if the worker already runs in an isolated container"
        )


@dataclass(frozen=True, slots=True)
class TestSandbox:
    """bubblewrap profile for running a repository's tests."""

    __test__ = False  # not a pytest class

    hidden: tuple[Path, ...]  # replaced by empty tmpfs (home, service data, cwd)
    readonly: tuple[Path, ...] = ()  # re-exposed read-only after hiding (toolchains)
    network: bool = False

    def wrap(self, argv: list[str], workspace: Path) -> list[str]:
        command = ["bwrap", "--die-with-parent", "--new-session", "--unshare-all"]
        if self.network:
            command.append("--share-net")
        # /tmp here is a fresh tmpfs inside the sandbox, not the host's shared /tmp. /run is
        # emptied too: it holds live IPC sockets (the systemd/D-Bus user bus, docker.sock), and
        # connect() works through a read-only bind; `systemd-run --user` would then start a
        # process outside the sandbox.
        command += ["--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc"]
        command += ["--tmpfs", "/tmp", "--tmpfs", "/run"]  # noqa: S108
        resolv = Path(os.path.realpath("/etc/resolv.conf"))
        if self.network and resolv.is_relative_to("/run") and resolv.is_file():
            # systemd-resolved: /etc/resolv.conf points into the /run just emptied. The file
            # alone, not its directory (which also holds the resolver's varlink socket).
            command += ["--ro-bind", str(resolv), str(resolv)]
        for path in self.hidden:
            if path.exists():
                command += ["--tmpfs", str(path)]
        for path in self.readonly:
            if path.exists():
                command += ["--ro-bind", str(path), str(path)]
        workspace = workspace.resolve()
        command += ["--bind", str(workspace), str(workspace)]
        # An unusable .git (an empty file): the tests can neither read nor create a repository
        # whose config a later, unsandboxed git (Claude Code's own) would execute.
        command += ["--ro-bind", "/dev/null", str(workspace / ".git")]
        command += ["--chdir", str(workspace), "--"]
        return command + argv


def remove_git_dir(workspace: Path) -> None:
    """Remove ``workspace/.git`` whatever it is (directory, file or symlink, never followed).

    Untrusted code must never hold a repository that a trusted git (ticket2pr's, or Claude
    Code's own, which runs unsandboxed) would later read: its config can run commands.
    """
    path = workspace / ".git"
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def remove_tree(path: Path) -> None:
    """Best-effort ``shutil.rmtree`` that also clears read-only directories.

    Untrusted runs leave them (Go's module cache is read-only on purpose), and a plain rmtree
    gives up on their contents. Symlinks are never followed.
    """

    def make_writable(_func: object, name: str, exc: BaseException) -> None:
        if not isinstance(exc, PermissionError):
            return
        entry = Path(name)
        try:
            entry.parent.chmod(stat.S_IRWXU)
            if entry.is_dir() and not entry.is_symlink():
                entry.chmod(stat.S_IRWXU)
                remove_tree(entry)
            else:
                entry.unlink()
        except OSError:
            return

    shutil.rmtree(path, onexc=make_writable)


def hidden_paths(settings: Settings) -> tuple[Path, ...]:
    """Directories untrusted code must not read: the service's home, data and working dirs."""
    hidden = {Path.home(), Path.cwd(), settings.data_dir}
    if config_dir := os.environ.get("CLAUDE_CONFIG_DIR"):  # where the `claude` login lives
        hidden.add(Path(config_dir).expanduser().resolve())
    return tuple(sorted(path for path in hidden if path != Path("/")))


def toolchain_paths(settings: Settings) -> tuple[Path, ...]:
    """Directories re-exposed read-only inside hidden ones (interpreters, configured tools)."""
    candidates = (*_interpreter_roots(), *(p.resolve() for p in settings.sandbox_readonly_paths))
    hidden = hidden_paths(settings)
    kept = []
    for path in candidates:
        # Mounted after the hidden tmpfs: a path holding a hidden one would re-expose it.
        if any(secret.is_relative_to(path) for secret in hidden):
            logger.warning("not exposing %s to the sandbox: it contains a hidden directory", path)
        else:
            kept.append(path)
    return tuple(kept)


def sandbox_profile(settings: Settings) -> TestSandbox | None:
    """The test sandbox profile for a deployment, or ``None`` when sandboxing is disabled."""
    if not settings.sandbox:
        return None
    return TestSandbox(
        hidden=hidden_paths(settings),
        readonly=toolchain_paths(settings),
        network=settings.test_network,
    )


def _interpreter_roots() -> tuple[Path, ...]:
    """Install roots of this Python, following every symlink hop of the executable.

    ``.venv/bin/python`` may point to ``…/cpython-3.12-…/bin/python3.12`` where
    ``cpython-3.12-…`` is itself a link: each hop must stay visible in the sandbox.
    """
    roots = {Path(sys.prefix), Path(sys.base_prefix)}
    hop = Path(sys.executable)
    for _ in range(16):  # bounded: symlink loops are an OS error anyway
        if not hop.is_symlink():
            break
        hop = hop.parent / hop.readlink()
        roots.add(hop.parent.parent)
    roots.add(Path(sys.executable).resolve().parent.parent)
    return tuple(sorted(roots))


def harden_service_process(settings: Settings) -> None:
    """Process-level protections for the process that holds the secrets.

    It always becomes non-dumpable. It only becomes a child subreaper without the sandbox:
    that is when it reaps its descendants between jobs, and an adopted orphan nobody reaps
    would otherwise linger as a zombie.
    """
    make_process_undumpable()
    if not settings.sandbox:
        become_subreaper()


def become_subreaper() -> None:
    """Adopt every orphaned descendant, so ``kill_descendants`` can find daemonised ones.

    A test or an unsandboxed agent command can ``setsid`` and double-fork to outlive its
    process group; as a subreaper, the service still sees it as a descendant.
    """
    if sys.platform != "linux":
        return
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        logger.warning("prctl(PR_SET_CHILD_SUBREAPER) failed: errno %s", ctypes.get_errno())


def kill_descendants(root: int | None = None, *, deadline_s: float = 10.0) -> int:
    """SIGKILL every descendant of ``root`` (default: this process). Returns how many died.

    Sweeps until a pass finds nothing left, freezing (SIGSTOP) each pass's processes before
    killing them so they cannot fork in between. Raises ``PolicyViolationError`` if some are
    still alive at the deadline: the caller must not go on to publish with its token.

    Only safe when this process runs no other children it wants to keep, i.e. one job at a
    time; ``Settings`` enforces a single worker when the sandbox is disabled.
    """
    if sys.platform != "linux":
        return 0
    root = os.getpid() if root is None else root
    killed: set[int] = set()
    deadline = time.monotonic() + deadline_s
    while targets := _descendants(root):
        live = [pid for pid, state in targets if _alive(pid, state)]
        if time.monotonic() > deadline:
            if live:
                raise PolicyViolationError(
                    f"{len(live)} leftover untrusted process(es) survived the sweep; not "
                    "publishing (restart the worker if this persists)"
                )
            break  # zombies another descendant still has to reap: harmless
        # Every pid, zombies included: a "zombie" leader whose other threads still run dies
        # with its whole process; signalling a true zombie does nothing.
        for sig in (signal.SIGSTOP, signal.SIGKILL):
            for pid, _state in targets:
                with suppress(ProcessLookupError, PermissionError):
                    os.kill(pid, sig)
        killed.update(live)
        # Reap them (as their (sub)reaper we are the parent), never blocking past the deadline.
        for pid, _state in targets:
            with suppress(ChildProcessError, OSError):
                os.waitpid(pid, os.WNOHANG)
        time.sleep(0.01)
    return len(killed)


def _alive(pid: int, state: str) -> bool:
    """Not a zombie, or a zombie thread-group leader whose other threads still run."""
    if state != "Z":
        return True
    try:
        return sum(1 for _ in Path(f"/proc/{pid}/task").iterdir()) > 1
    except OSError:
        return False


def _descendants(root: int) -> list[tuple[int, str]]:
    """``(pid, state)`` of every descendant of ``root``, from ``/proc/<pid>/stat``."""
    children: dict[int, list[tuple[int, str]]] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        with suppress(OSError, ValueError, IndexError):
            stat = (entry / "stat").read_text()
            # Fields after the ")" that closes the command name: state, ppid, ...
            state, ppid = stat[stat.rindex(")") + 2 :].split()[:2]
            children.setdefault(int(ppid), []).append((int(entry.name), state))
    found: list[tuple[int, str]] = []
    stack = [root]
    while stack:
        for child in children.get(stack.pop(), []):
            found.append(child)
            stack.append(child[0])
    return found


def make_process_undumpable() -> None:
    """Make this process's memory and ``/proc/<pid>/environ`` unreadable by same-uid peers.

    The agent and the tests run as the service's user. Without this, any of them can read
    the service's environment (API tokens) from ``/proc``. Children reset the flag on exec,
    so it only protects the service process itself, which is the one holding secrets.
    """
    if sys.platform != "linux":
        return
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
        logger.warning("prctl(PR_SET_DUMPABLE, 0) failed: errno %s", ctypes.get_errno())
