"""The test sandbox, process hardening and secret redaction.

The bubblewrap tests run the real thing and are skipped where bubblewrap is unavailable.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import SecretStr

from ticket2pr.config import Settings
from ticket2pr.errors import ConfigurationError
from ticket2pr.redaction import MASK, Redactor
from ticket2pr.sandbox import (
    TestSandbox,
    ensure_sandbox_available,
    make_process_undumpable,
    missing_sandbox_tools,
    sandbox_profile,
)
from ticket2pr.verify import run_tests

needs_bwrap = pytest.mark.skipif(
    sys.platform != "linux" or shutil.which("bwrap") is None, reason="needs bubblewrap"
)


# ── availability ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("platform", "which", "missing"),
    [
        ("linux", {"bwrap": "/usr/bin/bwrap", "socat": "/usr/bin/socat"}, []),
        ("linux", {"bwrap": "/usr/bin/bwrap"}, ["socat"]),
        ("darwin", {}, ["no supported sandbox on darwin (Linux with bubblewrap is required)"]),
    ],
)
def test_missing_sandbox_tools(
    monkeypatch: pytest.MonkeyPatch, platform: str, which: dict[str, str], missing: list[str]
) -> None:
    monkeypatch.setattr("shutil.which", which.get)
    assert missing_sandbox_tools(platform) == missing


def test_ensure_sandbox_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ticket2pr.sandbox.missing_sandbox_tools", lambda: ["socat"])
    with pytest.raises(ConfigurationError, match="T2P_SANDBOX=false"):
        ensure_sandbox_available()
    monkeypatch.setattr("ticket2pr.sandbox.missing_sandbox_tools", list)
    ensure_sandbox_available()


def test_profile_follows_settings(settings: Settings, tmp_path: Path) -> None:
    assert sandbox_profile(settings) is None  # the shared fixture disables it
    enabled = settings.model_copy(
        update={"sandbox": True, "test_network": True, "sandbox_readonly_paths": [tmp_path]}
    )
    profile = sandbox_profile(enabled)
    assert profile is not None
    assert profile.network is True
    assert Path.home() in profile.hidden
    assert Path("/") not in profile.hidden
    assert Path(sys.prefix) in profile.readonly
    assert tmp_path.resolve() in profile.readonly


def test_wrap_builds_the_bwrap_command(tmp_path: Path) -> None:
    sandbox = TestSandbox(hidden=(tmp_path, tmp_path / "missing"), readonly=(tmp_path,))
    argv = sandbox.wrap(["/bin/true"], tmp_path)
    assert argv[:4] == ["bwrap", "--die-with-parent", "--new-session", "--unshare-all"]
    assert "--share-net" not in argv
    assert argv.count("--tmpfs") == 3  # /tmp, /run and the existing hidden path only
    assert argv[-2:] == ["--", "/bin/true"]
    assert "--share-net" in TestSandbox(hidden=(), network=True).wrap(["x"], tmp_path)


# ── the real sandbox ────────────────────────────────────────────────────


@pytest.fixture
def sandbox(tmp_path: Path) -> TestSandbox:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text("[user]\n\tname = secret\n")
    return TestSandbox(hidden=(home,), readonly=(Path(sys.prefix), Path(sys.base_prefix)))


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    path = tmp_path / "work"
    path.mkdir()
    return path


async def _run(command: str, workspace: Path, sandbox: TestSandbox) -> str:
    report = await run_tests(command, workspace, timeout_s=30, sandbox=sandbox)
    return report.output_tail


@needs_bwrap
async def test_sandbox_hides_the_parent_environment(
    workspace: Path, sandbox: TestSandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("T2P_GITHUB_TOKEN", "ghp_must_not_leak")
    # Every /proc/<pid>/environ the tests can see, including their parent's.
    output = await _run("cat /proc/*/environ 2>/dev/null; echo; ls /proc", workspace, sandbox)
    assert "ghp_must_not_leak" not in output


@needs_bwrap
async def test_sandbox_hides_home_and_blocks_network(
    workspace: Path, sandbox: TestSandbox, tmp_path: Path
) -> None:
    output = await _run(
        f"cat {tmp_path}/home/.gitconfig || echo HIDDEN; "
        "python3 -c 'import socket; socket.create_connection((\"1.1.1.1\", 80), 2)' "
        "2>&1 | tail -1",
        workspace,
        sandbox,
    )
    assert "HIDDEN" in output
    assert "secret" not in output
    assert "Network is unreachable" in output or "Errno" in output


@needs_bwrap
async def test_sandbox_allows_workspace_writes_only(
    workspace: Path, sandbox: TestSandbox, tmp_path: Path
) -> None:
    outside = tmp_path / "outside.txt"  # under the host /tmp, a private tmpfs in the sandbox
    report = await run_tests(
        f"echo ok > result.txt && echo x > {outside} && (touch /usr/pwned || echo READONLY)",
        workspace,
        timeout_s=30,
        sandbox=sandbox,
    )
    assert (workspace / "result.txt").read_text() == "ok\n"
    assert not outside.exists()
    assert "READONLY" in report.output_tail


@needs_bwrap
async def test_sandbox_exposes_no_host_ipc_socket(workspace: Path, sandbox: TestSandbox) -> None:
    """A user D-Bus socket would let `systemd-run --user` start a process outside the sandbox."""
    output = await _run(
        'test -e "/run/user/$(id -u)/bus" && echo EXPOSED; '
        "find /run -type s 2>/dev/null | head -1; echo DONE",
        workspace,
        sandbox,
    )
    assert output.strip() == "DONE"


@needs_bwrap
async def test_sandbox_keeps_the_interpreter(workspace: Path, sandbox: TestSandbox) -> None:
    report = await run_tests(
        f"{sys.executable} -c 'print(40 + 2)'", workspace, timeout_s=30, sandbox=sandbox
    )
    assert report.passed, report.output_tail
    assert "42" in report.output_tail


# ── process hardening ───────────────────────────────────────────────────


@pytest.mark.skipif(sys.platform != "linux", reason="Linux /proc semantics")
def test_undumpable_process_hides_its_environment_from_children() -> None:
    script = (
        "import os, subprocess, sys\n"
        "from ticket2pr.sandbox import make_process_undumpable\n"
        "make_process_undumpable()\n"
        "child = subprocess.run(['cat', f'/proc/{os.getpid()}/environ'], capture_output=True)\n"
        "sys.exit(0 if b'canary-value' not in child.stdout and child.returncode else 1)\n"
    )
    env = {**os.environ, "CANARY": "canary-value"}
    assert subprocess.run([sys.executable, "-c", script], env=env, check=False).returncode == 0


def test_undumpable_is_a_noop_elsewhere(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.platform", "darwin")
    make_process_undumpable()


# ── redaction ───────────────────────────────────────────────────────────


def test_redactor_masks_and_detects(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-0123456789")
    settings = Settings(
        github_token=SecretStr("ghp_abcdefghij"),
        jira_api_token=SecretStr("short"),  # too short to mask safely
        _env_file=None,
    )
    redactor = Redactor.from_settings(settings)
    text = "token ghp_abcdefghij and key sk-ant-0123456789, short stays"
    assert redactor.redact(text) == f"token {MASK} and key {MASK}, short stays"
    assert redactor.contains_secret(b"...ghp_abcdefghij...")
    assert not redactor.contains_secret("nothing here")


def test_redactor_prefers_longest_match() -> None:
    redactor = Redactor(["abcdefgh", "abcdefgh-longer"])
    assert redactor.redact("x abcdefgh-longer y") == f"x {MASK} y"
