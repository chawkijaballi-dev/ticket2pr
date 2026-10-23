"""Tool-call guardrails, enforced in a ``PreToolUse`` hook before any tool runs.

For Bash it is defence in depth on top of the OS sandbox. For the file tools, which run in
the Claude Code process outside that sandbox, it is the only thing keeping writes inside the
workspace (docs/security.md). It also protects sensitive paths and blocks git commands that
only the harness may run.
It is deliberately conservative: a false positive costs the agent one retry, a false
negative could cost a leaked credential.
"""

from __future__ import annotations

import fnmatch
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

_PATH_KEYS = ("file_path", "path", "notebook_path")
_WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
_FORBIDDEN_GIT = {
    "push", "commit", "reset", "rebase", "remote", "config", "filter-branch", "update-ref",
    "credential", "am", "cherry-pick", "merge", "tag", "worktree", "submodule",
}  # fmt: skip
_SHELL_SPLIT = re.compile(r"&&|\|\||[;|&\n()`]|\$\(")
_SECRET_PATHS = re.compile(r"/proc/[^/\s]*/environ|\.git-credentials|\.netrc\b")
_GIT_OPTIONS_WITH_VALUE = {"-C", "--git-dir", "--work-tree", "--namespace", "--exec-path"}
_ENV_DUMPS = (["env"], ["printenv"], ["export"], ["export", "-p"], ["set"], ["declare", "-x"])


@dataclass(frozen=True, slots=True)
class Policy:
    workspace: Path
    protected_globs: tuple[str, ...] = ()
    denied: list[str] = field(default_factory=list)  # audit trail of blocked calls

    def check(self, tool_name: str, tool_input: dict[str, Any]) -> str | None:
        """Return why the call must be blocked, or ``None`` to let it through."""
        if tool_name == "Bash":
            if tool_input.get("run_in_background"):
                # The file tools run outside the OS sandbox and act after this check: a
                # sandboxed process left running could swap a symlink in between. Without
                # background commands (also disabled in Claude Code, see claude.py), each Bash
                # sandbox (PID namespace) ends with its command.
                return "background commands are not allowed"
            return self._check_bash(str(tool_input.get("command", "")))
        for key in _PATH_KEYS:
            raw = tool_input.get(key)
            if isinstance(raw, str) and raw:
                reason = self._check_path(raw, write=tool_name in _WRITE_TOOLS)
                if reason:
                    return reason
        return None

    def is_protected(self, relative: str) -> bool:
        posix = PurePosixPath(relative).as_posix()
        for pattern in self.protected_globs:
            if pattern.endswith("/"):
                if posix == pattern.rstrip("/") or posix.startswith(pattern):
                    return True
            elif fnmatch.fnmatch(posix, pattern) or fnmatch.fnmatch(f"/{posix}", pattern):
                return True
        return False

    def _check_path(self, raw: str, *, write: bool) -> str | None:
        root = self.workspace.resolve()
        # Claude Code expands a leading ~/ itself (not ~user/, which stays a relative name).
        path = Path.home() / raw[2:] if raw == "~" or raw.startswith("~/") else Path(raw)
        candidate = path if path.is_absolute() else root / path
        resolved = candidate.resolve()
        if not resolved.is_relative_to(root):
            return f"{raw} is outside the workspace"
        relative = resolved.relative_to(root).as_posix()
        if relative == ".git" or relative.startswith(".git/"):
            return "the .git directory is managed by the harness"
        if write and self.is_protected(relative):
            return f"{relative} is a protected path"
        return None

    def _check_bash(self, command: str) -> str | None:
        for segment in _SHELL_SPLIT.split(command):
            try:
                words = shlex.split(segment)
            except ValueError:
                words = segment.split()
            if not words:
                continue
            reason = self._check_words(words)
            if reason:
                return reason
        if _SECRET_PATHS.search(command):
            return "reading process environments or credential stores is not allowed"
        return None

    def _check_words(self, words: list[str]) -> str | None:
        words = _strip_assignments(words)
        if not words:
            return None
        names = [PurePosixPath(word).name for word in words]
        if words in _ENV_DUMPS or names[0] == "printenv" or names == ["env"]:
            return "dumping the environment is not allowed"
        # Wrappers take their own arguments ("timeout 5 git push", "env -i git push"), so
        # rather than parsing each wrapper, inspect every word that names a sensitive program.
        # Over-blocking ("echo git push" unquoted) only costs the agent a rephrase.
        for index, name in enumerate(names):
            if name in {"sudo", "su", "doas"}:
                return "privilege escalation is not allowed"
            if name == "git":
                reason = _check_git(words[index + 1 :])
                if reason:
                    return reason
        if any(".git/" in word or word in {".git", "./.git"} for word in words[1:]):
            return "the .git directory is managed by the harness"
        return None


def _strip_assignments(words: list[str]) -> list[str]:
    """Drop leading ``VAR=value`` assignments (``FOO=1 make test``)."""
    while words and "=" in words[0] and not words[0].startswith("-"):
        words = words[1:]
    return words


def _check_git(args: list[str]) -> str | None:
    index = 0
    while index < len(args) and args[index].startswith("-"):
        option = args[index]
        if option in {"-c", "--config-env"} or option.startswith("--config-env="):
            return "overriding git configuration is reserved for the harness"
        if option in _GIT_OPTIONS_WITH_VALUE:
            index += 1  # skip the option's value
        index += 1
    subcommand = args[index] if index < len(args) else ""
    if subcommand in _FORBIDDEN_GIT:
        return f"`git {subcommand}` is reserved for the harness"
    return None
