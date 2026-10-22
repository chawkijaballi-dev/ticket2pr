"""Detect and run a repository's test suite.

The tests execute code the agent just wrote. They run in the bubblewrap sandbox described in
``ticket2pr.sandbox``, with a minimal environment (no credentials), in their own process
group, and are killed with the whole group on timeout.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import time
from contextlib import suppress
from pathlib import Path

from ticket2pr.domain import TestReport
from ticket2pr.sandbox import TestSandbox

_TAIL_CHARS = 8_000
_ENV_PASSTHROUGH = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "VIRTUAL_ENV", "UV_CACHE_DIR")


def detect_test_command(workspace: Path) -> str | None:
    """Best-effort guess of the command that runs the repository's tests."""
    if (workspace / "package.json").is_file():
        with suppress(ValueError, OSError):
            scripts = json.loads((workspace / "package.json").read_text("utf-8")).get("scripts")
            if isinstance(scripts, dict) and "test" in scripts:
                return "npm test --silent"
    python_markers = ("pyproject.toml", "pytest.ini", "setup.cfg", "tox.ini", "setup.py")
    if any((workspace / name).is_file() for name in python_markers) and _has_python_tests(
        workspace
    ):
        return "python -m pytest -q"
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
    env["PATH"] = _without_worker_venv(env.get("PATH", os.defpath))
    env["CI"] = "true"
    # No bytecode caches: they would end up in the commit, and a stale .pyc (same size, same
    # mtime second) can make a retry test the previous attempt's code.
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    argv = ["/bin/sh", "-c", command]
    if sandbox is not None:
        argv = sandbox.wrap(argv, workspace)
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
    try:
        async with asyncio.timeout(timeout_s):
            output, _ = await process.communicate()
    except asyncio.CancelledError:
        _kill_group(process.pid)
        await process.wait()
        raise
    except TimeoutError:
        _kill_group(process.pid)
        await process.wait()
        return TestReport(
            command=command,
            passed=False,
            exit_code=None,
            output_tail=f"Timed out after {timeout_s:.0f}s.",
            duration_s=time.monotonic() - started,
        )
    text = output.decode("utf-8", "replace")
    return TestReport(
        command=command,
        passed=process.returncode == 0,
        exit_code=process.returncode,
        output_tail=text[-_TAIL_CHARS:],
        duration_s=time.monotonic() - started,
    )


def _without_worker_venv(path: str) -> str:
    """Drop ticket2pr's own virtualenv from PATH: the repository brings its own toolchain."""
    if sys.prefix == sys.base_prefix:  # not running from a virtualenv
        return path
    venv_bin = str(Path(sys.prefix) / "bin")
    return os.pathsep.join(entry for entry in path.split(os.pathsep) if entry != venv_bin)


def _kill_group(pid: int) -> None:
    with suppress(ProcessLookupError):
        os.killpg(pid, signal.SIGKILL)
