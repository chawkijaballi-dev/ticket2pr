from __future__ import annotations

from pathlib import Path

import pytest

from ticket2pr.agent.guard import Policy

PROTECTED = (".github/", "**/.env", "**/*.pem", "secrets.txt")


@pytest.fixture
def policy(tmp_path: Path) -> Policy:
    (tmp_path / "src").mkdir()
    return Policy(workspace=tmp_path, protected_globs=PROTECTED)


@pytest.mark.parametrize(
    "command",
    [
        "pytest -q",
        "python -m pytest tests/ && ruff check .",
        "git status",
        "git diff --stat",
        "git -C sub log -1",
        "git log --oneline | head",
        "npm test",
        "env FOO=1 python script.py",
        "FOO=bar make test",
        "echo 'git push' > notes.txt",  # quoted text is not a command
    ],
)
def test_allows_ordinary_commands(policy: Policy, command: str) -> None:
    assert policy.check("Bash", {"command": command}) is None


@pytest.mark.parametrize(
    ("command", "reason"),
    [
        ("git push origin main", "reserved"),
        ("git commit -am wip", "reserved"),
        ("git -C . push", "reserved"),
        ("git --git-dir=.git config user.name x", "reserved"),
        ("git -c core.hooksPath=/tmp status", "overriding git configuration"),
        ("nohup git push &", "reserved"),
        ("FOO=1 git remote add evil https://x", "reserved"),
        ("pytest && git push", "reserved"),
        ("echo $(git push)", "reserved"),
        ("/usr/bin/git reset --hard", "reserved"),
        ("env", "dumping the environment"),
        ("env | grep TOKEN", "dumping the environment"),
        ("printenv GITHUB_TOKEN", "dumping the environment"),
        ("export -p", "dumping the environment"),
        ("cat /proc/self/environ", "process environments"),
        ("cat ~/.git-credentials", "credential stores"),
        ("sudo rm -rf /", "privilege escalation"),
        ("cat .git/config", ".git directory"),
        ("timeout 5 git push", "reserved"),
        ("env -i git push", "reserved"),
    ],
)
def test_blocks_dangerous_commands(policy: Policy, command: str, reason: str) -> None:
    verdict = policy.check("Bash", {"command": command})
    assert verdict is not None
    assert reason in verdict


def test_unbalanced_quotes_fall_back_to_plain_split(policy: Policy) -> None:
    assert policy.check("Bash", {"command": "git push 'oops"}) is not None


@pytest.mark.parametrize(
    ("tool", "path", "blocked"),
    [
        ("Write", "src/app.py", False),
        ("Edit", "README.md", False),
        ("Read", ".github/workflows/ci.yml", False),  # reading a protected file is fine
        ("Edit", ".github/workflows/ci.yml", True),
        ("Write", "config/.env", True),
        ("Write", ".env", True),
        ("Edit", "certs/server.pem", True),
        ("Write", "secrets.txt", True),
        ("Read", "/etc/passwd", True),
        ("Read", "../outside.txt", True),
        ("Read", "~/.claude/.credentials.json", True),
        ("Write", "~/x", True),
        ("Write", "~nosuchuser/x", False),  # a relative name, not expanded
        ("Glob", ".git", True),
        ("Read", ".git/config", True),
        ("Grep", "src", False),
    ],
)
def test_file_tools(policy: Policy, tool: str, path: str, blocked: bool) -> None:
    key = "path" if tool in {"Glob", "Grep"} else "file_path"
    assert (policy.check(tool, {key: path}) is not None) is blocked


def test_absolute_paths_inside_workspace_are_allowed(policy: Policy, tmp_path: Path) -> None:
    assert policy.check("Write", {"file_path": str(tmp_path / "src" / "x.py")}) is None


def test_symlink_escape_is_blocked(policy: Policy, tmp_path: Path) -> None:
    (tmp_path / "link").symlink_to("/etc")
    verdict = policy.check("Read", {"file_path": "link/passwd"})
    assert verdict is not None
    assert "outside the workspace" in verdict


def test_tools_without_paths_pass(policy: Policy) -> None:
    assert policy.check("Bash", {"command": "ls"}) is None


@pytest.mark.parametrize(
    ("path", "protected"),
    [(".github", True), (".github/x", True), ("a/.env", True), ("docs/github.md", False)],
)
def test_is_protected(policy: Policy, path: str, protected: bool) -> None:
    assert policy.is_protected(path) is protected


def test_background_commands_are_blocked(policy: Policy) -> None:
    """A process outliving its command could race the (unsandboxed) file tools' writes."""
    verdict = policy.check("Bash", {"command": "pytest", "run_in_background": True})
    assert verdict == "background commands are not allowed"
    assert policy.check("Bash", {"command": "pytest", "run_in_background": False}) is None
