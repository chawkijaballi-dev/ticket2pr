"""Detect and run a repository's test suite.

The tests execute code the agent just wrote. They run in the bubblewrap sandbox described in
``ticket2pr.sandbox``, with a minimal environment (no credentials), in their own process
group, killed when the run ends. A process that leaves the group dies with the sandbox's PID
namespace, or, unsandboxed, with ``sandbox.kill_descendants`` after the job's agent and tests.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import tempfile
import time
from contextlib import suppress
from pathlib import Path

from ticket2pr.domain import TestReport
from ticket2pr.sandbox import TestSandbox

_TAIL_CHARS = 8_000
_TAIL_BYTES = 4 * _TAIL_CHARS  # UTF-8: at most 4 bytes per character
_DRAIN_TIMEOUT_S = 5.0
# Not VIRTUAL_ENV: it would point tools such as `poetry run` at ticket2pr's own virtualenv.
_ENV_PASSTHROUGH = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "UV_CACHE_DIR")
_TOOLCHAIN_HOMES = {"RUSTUP_HOME": ".rustup", "CARGO_HOME": ".cargo", "PYENV_ROOT": ".pyenv"}


def detect_test_command(workspace: Path) -> str | None:
    """Best-effort guess of the command that runs the repository's tests."""
    if (workspace / "package.json").is_file():
        with suppress(ValueError, OSError, AttributeError):  # not JSON, or not an object
            scripts = json.loads((workspace / "package.json").read_text("utf-8")).get("scripts")
            test = scripts.get("test") if isinstance(scripts, dict) else None
            if isinstance(test, str) and "no test specified" not in test:  # npm init default
                return "npm test --silent"
    python_markers = ("pyproject.toml", "pytest.ini", "setup.cfg", "tox.ini", "setup.py")
    if any((workspace / name).is_file() for name in python_markers) and _has_python_tests(
        workspace
    ):
        return "python3 -m pytest -q"  # Debian/Ubuntu ship no `python` by default
    if (workspace / "go.mod").is_file():
        return "go test ./..."
    if (workspace / "Cargo.toml").is_file():
        return "cargo test --quiet"
    makefile = workspace / "Makefile"
    if makefile.is_file() and any(
        line.startswith("test:") for line in makefile.read_text("utf-8", "replace").splitlines()
    ):
        return "make test"
    return None


def _has_python_tests(workspace: Path) -> bool:
    if (workspace / "tests").is_dir() or (workspace / "test").is_dir():
        return True
    return any(workspace.glob("test_*.py")) or any(workspace.glob("*_test.py"))


async def run_tests(
    command: str | None,
    workspace: Path,
    *,
    timeout_s: float,
    sandbox: TestSandbox | None,
) -> TestReport:
    """Run ``command`` in ``workspace``; inside ``sandbox`` unless it is explicitly ``None``."""
    if command is None:
        return TestReport(command=None, passed=False, output_tail="No test command detected.")

    env = {key: os.environ[key] for key in _ENV_PASSTHROUGH if key in os.environ}
    env |= _toolchain_homes()
    env["PATH"] = _without_worker_venv(env.get("PATH", os.defpath))
    env["CI"] = "true"
    # No bytecode caches: they would end up in the commit, and a stale .pyc (same size, same
    # mtime second) can make a retry test the previous attempt's code.
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONNOUSERSITE"] = "1"
    argv = ["/bin/sh", "-c", command]
    if sandbox is not None:
        env["TMPDIR"] = "/tmp"  # noqa: S108 - the sandbox's own tmpfs; the host's is hidden
        return await _execute(command, sandbox.wrap(argv, workspace), workspace, env, timeout_s)
    # Nothing contains these tests: give each run a fresh HOME and TMPDIR, so what a previous
    # job left there (~/.npmrc, tool configs, caches) cannot run inside this job's tests.
    # Version-manager homes (_toolchain_homes) stay shared: see docs/security.md.
    with tempfile.TemporaryDirectory(prefix="t2p-tests-", ignore_cleanup_errors=True) as scratch:
        env["HOME"] = env["TMPDIR"] = scratch
        return await _execute(command, argv, workspace, env, timeout_s)


async def _execute(
    command: str, argv: list[str], workspace: Path, env: dict[str, str], timeout_s: float
) -> TestReport:
    started = time.monotonic()
    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=workspace,
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,  # own process group, so a timeout kills every child too
    )
    output = bytearray()

    async def drain() -> None:
        assert process.stdout is not None  # noqa: S101 - stdout=PIPE
        while chunk := await process.stdout.read(65_536):
            output.extend(chunk)
            del output[:-_TAIL_BYTES]  # bounded: only the tail is reported

    reader = asyncio.create_task(drain())
    try:
        async with asyncio.timeout(timeout_s):
            await _exited(process)
    except TimeoutError:
        return TestReport(
            command=command,
            passed=False,
            exit_code=None,
            output_tail=f"Timed out after {timeout_s:.0f}s.",
            duration_s=time.monotonic() - started,
        )
    finally:
        _kill_group(process.pid)
        await _exited(process)
        # Killed children close the pipe; one that left the group could hold it forever.
        await asyncio.wait({reader}, timeout=_DRAIN_TIMEOUT_S)
        reader.cancel()
    text = output.decode("utf-8", "replace")
    return TestReport(
        command=command,
        passed=process.returncode == 0,
        exit_code=process.returncode,
        output_tail=text[-_TAIL_CHARS:],
        duration_s=time.monotonic() - started,
    )


def _toolchain_homes() -> dict[str, str]:
    """Where version managers keep their toolchains: they default to $HOME paths, and the
    tests may run with another (empty, or fresh) $HOME."""
    found = {}
    for name, default in _TOOLCHAIN_HOMES.items():
        location = os.environ.get(name) or str(Path.home() / default)
        if Path(location).is_dir():
            found[name] = location
    return found


def _without_worker_venv(path: str) -> str:
    """Drop ticket2pr's own virtualenv from PATH: the repository brings its own toolchain."""
    if sys.prefix == sys.base_prefix:  # not running from a virtualenv
        return path
    venv_bin = str(Path(sys.prefix) / "bin")
    return os.pathsep.join(entry for entry in path.split(os.pathsep) if entry != venv_bin)


async def _exited(process: asyncio.subprocess.Process) -> None:
    """Wait for the shell itself to exit.

    Not ``process.wait()``: it also waits for EOF on the pipe, which a leftover child (a
    server, ``cmd &``) keeps open long after the suite has finished.
    """
    while process.returncode is None:  # noqa: ASYNC110 - asyncio has no exit-only event
        await asyncio.sleep(0.05)


def _kill_group(pid: int) -> None:
    with suppress(ProcessLookupError):
        os.killpg(pid, signal.SIGKILL)
