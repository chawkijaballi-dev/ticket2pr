"""OS-level isolation for code ticket2pr did not write.

Two kinds of untrusted processes run on the worker: the agent's Bash commands (sandboxed by
Claude Code itself) and the repository's test suite (sandboxed here). Both need the same
host tools, and both must **fail closed**: Claude Code silently runs Bash unsandboxed when its
dependencies are missing, so ticket2pr checks them before every job.

The test sandbox uses bubblewrap: fresh PID namespace (so ``/proc`` shows no other process
and no other environment), no network unless allowed, a read-only root filesystem, writable
workspace and private ``/tmp`` only, an empty ``/run`` (no host IPC sockets), and the
service's home and data directories replaced by empty tmpfs mounts so credentials files are
out of reach.
"""

from __future__ import annotations

import ctypes
import logging
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from ticket2pr.config import Settings
from ticket2pr.errors import ConfigurationError

logger = logging.getLogger(__name__)

# Claude Code's Bash sandbox needs bubblewrap + socat on Linux; the test sandbox needs
# bubblewrap. Other platforms have no test sandbox, so they need an isolated container.
_LINUX_SANDBOX_TOOLS = ("bwrap", "socat")
_PR_SET_DUMPABLE = 4


def missing_sandbox_tools(platform: str = sys.platform) -> list[str]:
    """Return what is missing to sandbox the agent *and* the tests (empty when all is there)."""
    if platform != "linux":
        return [f"no supported sandbox on {platform} (Linux with bubblewrap is required)"]
    return [tool for tool in _LINUX_SANDBOX_TOOLS if shutil.which(tool) is None]


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
        for path in self.hidden:
            if path.exists():
                command += ["--tmpfs", str(path)]
        for path in self.readonly:
            if path.exists():
                command += ["--ro-bind", str(path), str(path)]
        workspace = workspace.resolve()
        command += ["--bind", str(workspace), str(workspace), "--chdir", str(workspace), "--"]
        return command + argv


def sandbox_profile(settings: Settings) -> TestSandbox | None:
    """The test sandbox profile for a deployment, or ``None`` when sandboxing is disabled."""
    if not settings.sandbox:
        return None
    hidden = {Path.home(), Path.cwd(), settings.data_dir.resolve()}
    return TestSandbox(
        hidden=tuple(sorted(path for path in hidden if path != Path("/"))),
        # The hidden directories may hold the interpreter (a virtualenv, uv-managed Python).
        readonly=(
            *_interpreter_roots(),
            *(path.resolve() for path in settings.sandbox_readonly_paths),
        ),
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
