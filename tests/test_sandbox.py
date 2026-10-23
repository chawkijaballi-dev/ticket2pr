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
from ticket2pr.errors import ConfigurationError, PolicyViolationError
from ticket2pr.redaction import MASK, Redactor
from ticket2pr.sandbox import (
    TestSandbox,
    _bwrap_works,
    _probe_bwrap,
    become_subreaper,
    ensure_sandbox_available,
    harden_service_process,
    hidden_paths,
    kill_descendants,
    make_process_undumpable,
    missing_sandbox_tools,
    remove_git_dir,
    remove_tree,
    sandbox_profile,
    toolchain_paths,
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
    monkeypatch.setattr("ticket2pr.sandbox._bwrap_works", lambda: True)
    assert missing_sandbox_tools(platform) == missing


@needs_bwrap
def test_bwrap_probe_starts_a_real_sandbox() -> None:
    assert _probe_bwrap() is True


def test_bwrap_probe_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "")  # bwrap not found: OSError
    assert _probe_bwrap() is False


def test_only_a_working_probe_is_remembered(monkeypatch: pytest.MonkeyPatch) -> None:
    results = iter([False, True])
    calls: list[bool] = []

    def probe() -> bool:
        calls.append(True)
        return next(results)

    monkeypatch.setattr("ticket2pr.sandbox._bwrap_ok", False)
    monkeypatch.setattr("ticket2pr.sandbox._probe_bwrap", probe)
    assert _bwrap_works() is False  # e.g. a slow first probe: retried next time
    assert _bwrap_works() is True
    assert _bwrap_works() is True
    assert len(calls) == 2


def test_wrap_binds_the_resolver_config_only_with_network(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    resolv = "/run/systemd/resolve/stub-resolv.conf"
    realpath = os.path.realpath
    monkeypatch.setattr(
        "ticket2pr.sandbox.os.path.realpath",
        lambda path, **kw: resolv if path == "/etc/resolv.conf" else realpath(path, **kw),
    )
    monkeypatch.setattr(Path, "is_file", lambda self: str(self) == resolv)
    networked = TestSandbox(hidden=(), network=True).wrap(["x"], tmp_path)
    assert ["--ro-bind", resolv, resolv] == networked[networked.index(resolv) - 1 :][:3]
    assert resolv not in TestSandbox(hidden=()).wrap(["x"], tmp_path)


def test_installed_bwrap_that_cannot_start_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """In an unprivileged container bwrap is installed but cannot create namespaces."""
    monkeypatch.setattr("shutil.which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr("ticket2pr.sandbox._bwrap_works", lambda: False)
    assert missing_sandbox_tools("linux") == [
        "permission to create namespaces (bwrap cannot start here)"
    ]


def test_ensure_sandbox_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ticket2pr.sandbox.missing_sandbox_tools", lambda: ["socat"])
    with pytest.raises(ConfigurationError, match="T2P_SANDBOX=false"):
        ensure_sandbox_available()
    monkeypatch.setattr("ticket2pr.sandbox.missing_sandbox_tools", list)
    ensure_sandbox_available()


def test_profile_follows_settings(settings: Settings, tmp_path: Path) -> None:
    assert sandbox_profile(settings) is None  # the shared fixture disables it
    tools = tmp_path / "tools"  # tmp_path itself is the (hidden) working directory
    enabled = settings.model_copy(
        update={"sandbox": True, "test_network": True, "sandbox_readonly_paths": [tools]}
    )
    profile = sandbox_profile(enabled)
    assert profile is not None
    assert profile.network is True
    assert Path.home() in profile.hidden
    assert Path("/") not in profile.hidden
    assert Path(sys.prefix) in profile.readonly
    assert tools.resolve() in profile.readonly


def test_toolchain_paths_never_re_expose_a_hidden_directory(
    settings: Settings, tmp_path: Path
) -> None:
    home_parent = Path.home().parent
    tools = tmp_path / "tools"
    exposed = settings.model_copy(update={"sandbox_readonly_paths": [home_parent, tools]})
    paths = toolchain_paths(exposed)
    assert home_parent.resolve() not in paths  # would undo the tmpfs over $HOME
    assert tools.resolve() in paths


def test_readonly_paths_expand_the_home_directory(settings: Settings) -> None:
    configured = Settings.model_validate(
        {**settings.model_dump(), "sandbox_readonly_paths": ["~/.rustup"]}
    )
    assert configured.sandbox_readonly_paths == [Path.home() / ".rustup"]


def test_claude_config_dir_is_hidden(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))  # the `claude` login
    assert (tmp_path / "claude").resolve() in hidden_paths(settings)


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


_PROBE = """
import asyncio, sys
from pathlib import Path
from ticket2pr.sandbox import TestSandbox
from ticket2pr.verify import run_tests

sandbox = None if sys.argv[2] == "none" else TestSandbox(
    hidden=(), readonly=(Path(sys.prefix), Path(sys.base_prefix))
)
command = "grep -qs ghp_must_not_leak /proc/*/environ && echo LEAK || echo CLEAN"
report = asyncio.run(run_tests(command, Path(sys.argv[1]), timeout_s=30, sandbox=sandbox))
print(report.output_tail.strip())
"""


def _probe(workspace: Path, mode: str) -> str:
    """Run the check from a process whose *exec-time* environment holds the secret.

    ``os.environ`` changes never reach ``/proc/<pid>/environ``, so the secret must be in
    the environment the probing process was started with.
    """
    env = {**os.environ, "T2P_GITHUB_TOKEN": "ghp_must_not_leak"}
    done = subprocess.run(
        [sys.executable, "-c", _PROBE, str(workspace), mode],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


@needs_bwrap
def test_sandbox_hides_the_parent_environment(workspace: Path) -> None:
    assert _probe(workspace, "none") == "LEAK"  # control: without the sandbox, it leaks
    assert _probe(workspace, "sandbox") == "CLEAN"


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
async def test_offline_tests_get_no_proxy_settings(
    workspace: Path, sandbox: TestSandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://bot:pw@proxy.test:3128")
    assert not sandbox.network
    output = await _run('echo "proxy=$HTTPS_PROXY"', workspace, sandbox)
    assert output.strip() == "proxy="


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
@pytest.mark.skipif(
    not Path(os.path.realpath("/etc/resolv.conf")).is_relative_to("/run"),
    reason="needs systemd-resolved's /etc/resolv.conf -> /run/...",
)
async def test_networked_sandbox_keeps_the_resolver_config(workspace: Path) -> None:
    """Emptying /run must not take /etc/resolv.conf (and every DNS lookup) with it."""
    networked = TestSandbox(hidden=(), network=True)
    report = await run_tests(
        "grep -q nameserver /etc/resolv.conf", workspace, timeout_s=30, sandbox=networked
    )
    assert report.passed, report.output_tail


def test_remove_tree_clears_read_only_directories(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("x")
    job = tmp_path / "job"
    pkg = job / "gomod" / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "a.go").write_text("package a\n")
    (job / "gomod" / "link").symlink_to(outside)
    for mode in (0o000, 0o300):  # unreadable: rmtree cannot even list them
        locked = job / f"locked-{mode:o}"
        (locked / "inner").mkdir(parents=True)
        (locked / "inner" / "f").write_text("x")
        (locked / "inner" / "f").chmod(0o000)
        locked.chmod(mode)
    for directory in (pkg, pkg.parent):  # what Go does to its module cache
        directory.chmod(0o555)
    remove_tree(job)
    assert not job.exists()
    assert (outside / "keep").exists()
    remove_tree(job)  # already gone: no error


def test_remove_tree_gives_up_quietly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    locked = tmp_path / "job" / "locked"
    locked.mkdir(parents=True)
    (locked / "f").write_text("x")
    locked.chmod(0o555)

    def refuse(self: Path, mode: int) -> None:
        raise PermissionError(mode)

    monkeypatch.setattr(Path, "chmod", refuse)
    remove_tree(tmp_path / "job")  # best effort: no error
    monkeypatch.undo()
    assert (locked / "f").exists()
    locked.chmod(0o755)


def test_remove_git_dir_never_follows_a_symlink(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("x")
    for kind in ("dir", "file", "symlink"):
        ws = tmp_path / kind
        ws.mkdir()
        target = ws / ".git"
        if kind == "dir":
            (target / "objects").mkdir(parents=True)
        elif kind == "file":
            target.write_text("gitdir: elsewhere\n")
        else:
            target.symlink_to(outside)
        remove_git_dir(ws)
        assert not target.exists()
        assert not target.is_symlink()
    assert (outside / "keep").exists()


@needs_bwrap
async def test_sandboxed_tests_run_in_a_clone(workspace: Path, sandbox: TestSandbox) -> None:
    """The eval grader runs hidden tests in a fresh clone, which has a .git directory."""
    (workspace / ".git" / "objects").mkdir(parents=True)
    report = await run_tests("true", workspace, timeout_s=30, sandbox=sandbox)
    assert report.passed, report.output_tail


@needs_bwrap
async def test_sandboxed_tests_cannot_create_a_repository(
    workspace: Path, sandbox: TestSandbox
) -> None:
    report = await run_tests(
        "git init -q . && git config core.fsmonitor 'touch pwned'",
        workspace,
        timeout_s=30,
        sandbox=sandbox,
    )
    assert not report.passed
    assert not (workspace / ".git").is_dir()


@needs_bwrap
async def test_sandbox_tmpdir_exists(
    workspace: Path, sandbox: TestSandbox, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host_only = tmp_path / "host-tmp"  # exists on the host, not inside the sandbox
    host_only.mkdir()
    monkeypatch.setenv("TMPDIR", str(host_only))
    report = await run_tests("mktemp", workspace, timeout_s=30, sandbox=sandbox)
    assert report.passed, report.output_tail


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


def _forbid_libc(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setattr("ticket2pr.sandbox.ctypes.CDLL", lambda *_a, **_k: pytest.fail("prctl"))


def test_undumpable_is_a_noop_elsewhere(monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_libc(monkeypatch)
    make_process_undumpable()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux prctl")
def test_subreaper_finds_and_kills_daemonised_descendants(tmp_path: Path) -> None:
    """A double-forked, setsid daemon escapes its process group but not a subreaper."""
    marker = tmp_path / "daemon.pid"
    script = (
        "import os, subprocess, sys, time\n"
        "from ticket2pr.sandbox import become_subreaper, kill_descendants\n"
        "become_subreaper()\n"
        f"subprocess.run(['sh', '-c', 'setsid sh -c \"echo $$ > {marker}; exec sleep 60\" &'])\n"
        "for _ in range(100):\n"
        f"    if os.path.exists('{marker}') and open('{marker}').read().strip(): break\n"
        "    time.sleep(0.05)\n"
        f"pid = int(open('{marker}').read())\n"
        "assert kill_descendants() >= 1\n"
        "try:\n"
        "    os.kill(pid, 0)\n"
        "except ProcessLookupError:\n"
        "    sys.exit(0)\n"
        "sys.exit(1)\n"
    )
    assert subprocess.run([sys.executable, "-c", script], check=False).returncode == 0


@pytest.mark.skipif(sys.platform != "linux", reason="Linux prctl")
def test_subreaper_kills_a_process_that_keeps_reforking() -> None:
    """A fork-and-exit chain changes PID constantly: each sweep must catch the new one."""
    script = (
        "import os, sys, time\n"
        "from ticket2pr.sandbox import become_subreaper, kill_descendants, _descendants\n"
        "become_subreaper()\n"
        "if os.fork() == 0:\n"
        "    end = time.monotonic() + 20\n"
        "    while time.monotonic() < end:\n"
        "        if os.fork():\n"
        "            os._exit(0)\n"
        "    os._exit(0)\n"
        "time.sleep(0.3)\n"
        "kill_descendants()\n"
        "time.sleep(0.3)\n"
        "live = [p for p, s in _descendants(os.getpid()) if s != 'Z']\n"
        "sys.exit(1 if live else 0)\n"
    )
    assert subprocess.run([sys.executable, "-c", script], check=False, timeout=60).returncode == 0


@pytest.mark.skipif(sys.platform != "linux", reason="Linux prctl")
def test_subreaper_kills_a_process_whose_leader_thread_exited() -> None:
    """Its main thread gone, /proc shows a zombie, yet another thread still runs."""
    script = (
        "import ctypes, os, sys, threading, time\n"
        "from ticket2pr.sandbox import become_subreaper, kill_descendants\n"
        "become_subreaper()\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        "    threading.Thread(target=time.sleep, args=(60,)).start()\n"
        "    ctypes.CDLL(None).pthread_exit(None)\n"
        "time.sleep(0.5)\n"
        "start = time.monotonic()\n"
        "assert kill_descendants(deadline_s=5) == 1\n"
        "assert time.monotonic() - start < 5\n"
        "try:\n"
        "    os.kill(pid, 0)\n"
        "except ProcessLookupError:\n"
        "    sys.exit(0)\n"
        "sys.exit(1)\n"
    )
    assert subprocess.run([sys.executable, "-c", script], check=False, timeout=60).returncode == 0


@pytest.mark.skipif(sys.platform != "linux", reason="Linux prctl")
def test_survivors_refuse_publishing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ticket2pr.sandbox._descendants", lambda _root: [(999_999, "R")])
    monkeypatch.setattr("ticket2pr.sandbox.os.kill", lambda *_args: None)  # never a real pid
    with pytest.raises(PolicyViolationError, match="not publishing"):
        kill_descendants(deadline_s=-1)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux prctl")
def test_zombies_are_reaped_but_not_counted_as_killed() -> None:
    script = (
        "import os, sys, time\n"
        "from ticket2pr.sandbox import kill_descendants\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        "    os._exit(0)\n"
        "time.sleep(0.2)  # the child is now a zombie\n"
        "assert kill_descendants() == 0\n"
        "try:\n"
        "    os.waitpid(pid, os.WNOHANG)\n"
        "except ChildProcessError:\n"
        "    sys.exit(0)  # already reaped\n"
        "sys.exit(1)\n"
    )
    assert subprocess.run([sys.executable, "-c", script], check=False).returncode == 0


@pytest.mark.skipif(sys.platform != "linux", reason="Linux prctl")
def test_git_leaves_no_background_process_under_a_subreaper(tmp_path: Path) -> None:
    """Git's detached auto-maintenance would otherwise pile up as adopted zombies."""
    origin = tmp_path / "origin"
    origin.mkdir()
    for args in (["init", "-q", "-b", "main"], ["commit", "-q", "--allow-empty", "-m", "init"]):
        subprocess.run(["git", *args], cwd=origin, check=True, env=_GIT_ENV)
    script = (
        "import asyncio, sys, time\n"
        "from pathlib import Path\n"
        "from ticket2pr.sandbox import _descendants, become_subreaper\n"
        "from ticket2pr.scm.git import GitRepo\n"
        "become_subreaper()\n"
        "async def main():\n"
        "    for i in range(5):\n"
        f"        repo = await GitRepo.clone('{origin.as_uri()}', Path('{tmp_path}') / f'c{{i}}',"
        " branch='main')\n"
        "        (repo.path / 'f').write_text(str(i))\n"
        "        await repo.stage_all()\n"
        "        await repo.commit('m', author_name='a', author_email='a@b.c')\n"
        "asyncio.run(main())\n"
        "time.sleep(0.5)\n"
        "sys.exit(len(_descendants(__import__('os').getpid())))\n"
    )
    assert subprocess.run([sys.executable, "-c", script], check=False).returncode == 0


_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def test_hardening_only_becomes_a_subreaper_without_the_sandbox(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    calls: list[str] = []
    monkeypatch.setattr("ticket2pr.sandbox.make_process_undumpable", lambda: calls.append("dump"))
    monkeypatch.setattr("ticket2pr.sandbox.become_subreaper", lambda: calls.append("reaper"))
    harden_service_process(settings.model_copy(update={"sandbox": True}))
    assert calls == ["dump"]
    harden_service_process(settings.model_copy(update={"sandbox": False}))
    assert calls == ["dump", "dump", "reaper"]


def test_kill_descendants_is_a_noop_elsewhere(monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_libc(monkeypatch)
    assert kill_descendants() == 0
    become_subreaper()


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


def test_redactor_masks_claude_oauth_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-abcdefgh")
    redactor = Redactor.from_settings(Settings(_env_file=None))
    assert redactor.redact("t=sk-ant-oat01-abcdefgh") == f"t={MASK}"


def test_redactor_prefers_longest_match() -> None:
    redactor = Redactor(["abcdefgh", "abcdefgh-longer"])
    assert redactor.redact("x abcdefgh-longer y") == f"x {MASK} y"


def test_redactor_masks_proxy_passwords(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://bot:s3cretpass@proxy.test:3128")
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.test:3128")  # no password: not a secret
    redactor = Redactor.from_settings(settings)
    assert redactor.redact("p=s3cretpass") == f"p={MASK}"
    assert redactor.redact("http://proxy.test:3128") == "http://proxy.test:3128"
    monkeypatch.setenv("HTTPS_PROXY", "http://ghp_tokenonly1@proxy.test:3128")
    assert Redactor.from_settings(settings).redact("u=ghp_tokenonly1") == f"u={MASK}"
    monkeypatch.setenv("HTTPS_PROXY", "user:pa55word99@proxy.test:3128")
    assert Redactor.from_settings(settings).redact("pa55word99") == MASK


def test_redactor_masks_provider_credentials(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")  # else the agent never sees AWS_*
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "wJalrXUtnFEMIK7MDENG")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "authorization=Bearer otlp-token-123,x=1")
    redactor = Redactor.from_settings(settings)
    assert redactor.contains_secret("key = 'wJalrXUtnFEMIK7MDENG'")
    assert redactor.redact("got Bearer otlp-token-123") == f"got {MASK}"
    monkeypatch.setenv("ANTHROPIC_CUSTOM_HEADERS", "X-Org: acme\nAuthorization: Bearer gw-tok-456")
    assert Redactor.from_settings(settings).redact("Bearer gw-tok-456") == MASK
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "Authorization=Bearer%20otlp-enc-789")
    assert Redactor.from_settings(settings).redact("token otlp-enc-789") == f"token {MASK}"
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "Authorization=Api-Token%20dt0c01.ABCDEFGH")
    assert Redactor.from_settings(settings).contains_secret("dt0c01.ABCDEFGH")


def test_redactor_ignores_variables_that_are_not_credentials(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    monkeypatch.setenv("UV_KEYRING_PROVIDER", "subprocess")  # KEY, but not a whole word
    monkeypatch.setenv("SERVICE_API_KEY", "blanked-for-the-agent")  # never seen by the agent
    monkeypatch.setenv("ANTHROPIC_CUSTOM_HEADERS", "X-Team: platform engineering")
    redactor = Redactor.from_settings(settings)
    assert not redactor.contains_secret("import subprocess")
    assert not redactor.contains_secret("blanked-for-the-agent")
    assert redactor.redact("software engineering") == "software engineering"
    monkeypatch.setenv("CLAUDE_CODE_API_KEY_HELPER_TTL_MS", "86400000")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "x-api-key=k3y-v4lue-1,x-dataset=claude-code")
    redactor = Redactor.from_settings(settings)
    assert not redactor.contains_secret("ms = 86400000")
    assert not redactor.contains_secret("service: claude-code")
    assert redactor.contains_secret("k3y-v4lue-1")


def test_wrap_empties_a_real_var_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = Path.is_symlink
    monkeypatch.setattr(Path, "is_dir", lambda self: True)
    monkeypatch.setattr(
        Path, "is_symlink", lambda self: False if str(self) == "/var/run" else real(self)
    )
    argv = TestSandbox(hidden=()).wrap(["x"], tmp_path)
    assert argv[argv.index("/var/run") - 1] == "--tmpfs"


def test_redactor_masks_a_secret_cut_by_a_tail() -> None:
    redactor = Redactor(["ghp_0123456789abcdef"])
    assert redactor.redact("23456789abcdef rest") == f"{MASK} rest"
    assert redactor.redact("9abcdef rest") == "9abcdef rest"  # under 8 characters: words
