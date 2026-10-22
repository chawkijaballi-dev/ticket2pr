"""Async git wrapper for one job workspace.

Security model: the agent has write access to the working tree, so anything under
``.git/`` must be treated as hostile by the time we commit and push. We therefore

* pass credentials through ``GIT_CONFIG_*`` environment variables, never argv or
  ``.git/config`` (argv is visible to every process; ``.git/config`` to the agent);
* restore ``.git/config`` from a snapshot taken right after cloning, so the agent cannot
  plant ``url.*.insteadOf`` rewrites or credential helpers that would exfiltrate the token;
* disable hooks (``core.hooksPath=/dev/null``) for every command we run after the agent.
"""

from __future__ import annotations

import asyncio
import base64
import os
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from ticket2pr.domain import ChangeSet
from ticket2pr.errors import SCMError

_SAFE_CONFIG = ("-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false")


@dataclass(frozen=True, slots=True)
class GitAuth:
    """HTTPS token auth, e.g. a GitHub installation or fine-grained token."""

    token: str
    username: str = "x-access-token"

    def env(self) -> dict[str, str]:
        basic = base64.b64encode(f"{self.username}:{self.token}".encode()).decode()
        return {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.extraHeader",
            "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
        }


class GitRepo:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._config_snapshot: bytes | None = None

    @classmethod
    async def clone(
        cls, url: str, dest: Path, *, branch: str, auth: GitAuth | None = None, depth: int = 50
    ) -> GitRepo:
        dest.parent.mkdir(parents=True, exist_ok=True)
        await _git(
            dest.parent,
            "clone",
            [
                "--quiet",
                f"--depth={depth}",
                "--single-branch",
                f"--branch={branch}",
                "--",
                url,
                str(dest),
            ],
            env=auth.env() if auth else None,
        )
        repo = cls(dest)
        repo.snapshot_config()
        return repo

    def snapshot_config(self) -> None:
        self._config_snapshot = (self.path / ".git" / "config").read_bytes()

    def restore_config(self) -> None:
        """Undo any change made to ``.git/config`` since the snapshot."""
        if self._config_snapshot is None:
            raise SCMError("restore_config() called before snapshot_config()")
        (self.path / ".git" / "config").write_bytes(self._config_snapshot)

    async def checkout_new_branch(self, name: str) -> None:
        await self._run("checkout", "--quiet", "-B", name)

    async def stage_all(self) -> ChangeSet:
        """Stage every change in the working tree and describe the staged diff."""
        await self._run("add", "--all")
        return await self.staged_changes()

    async def staged_changes(self) -> ChangeSet:
        numstat = await self._run("diff", "--cached", "--numstat", "--no-renames")
        files: list[str] = []
        insertions = deletions = 0
        for line in numstat.splitlines():
            added, removed, name = line.split("\t", 2)
            files.append(name)
            # Binary files report "-" for both counts.
            insertions += int(added) if added.isdigit() else 0
            deletions += int(removed) if removed.isdigit() else 0
        return ChangeSet(files=tuple(files), insertions=insertions, deletions=deletions)

    def exclude(self, *patterns: str) -> None:
        """Keep harness artefacts out of ``git add`` without touching tracked files."""
        exclude_file = self.path / ".git" / "info" / "exclude"
        exclude_file.parent.mkdir(parents=True, exist_ok=True)
        with exclude_file.open("a", encoding="utf-8") as handle:
            handle.writelines(f"{pattern}\n" for pattern in patterns)

    async def unstage(self, paths: list[str]) -> None:
        if paths:
            await self._run("reset", "--quiet", "--", *paths)

    async def commit(self, message: str, *, author_name: str, author_email: str) -> str:
        identity = {
            "GIT_AUTHOR_NAME": author_name,
            "GIT_AUTHOR_EMAIL": author_email,
            "GIT_COMMITTER_NAME": author_name,
            "GIT_COMMITTER_EMAIL": author_email,
        }
        await self._run("commit", "--quiet", "--no-verify", "-m", message, env=identity)
        return (await self._run("rev-parse", "HEAD")).strip()

    async def push(self, url: str, branch: str, *, auth: GitAuth | None = None) -> None:
        # Push to an explicit URL (not a remote name the agent could have repointed), and
        # force: the branch belongs to ticket2pr and a re-run replaces the previous attempt.
        await self._run(
            "push",
            "--quiet",
            "--force",
            "--no-verify",
            url,
            f"HEAD:refs/heads/{branch}",
            env=auth.env() if auth else None,
        )

    async def _run(self, command: str, *args: str, env: dict[str, str] | None = None) -> str:
        return await _git(self.path, command, list(args), config=_SAFE_CONFIG, env=env)


async def _git(
    cwd: Path,
    command: str,
    args: list[str],
    *,
    config: tuple[str, ...] = (),
    env: dict[str, str] | None = None,
) -> str:
    full_env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "LC_ALL": "C",
        **(env or {}),
    }
    process = await asyncio.create_subprocess_exec(
        "git",
        *config,
        command,
        *args,
        cwd=cwd,
        env=full_env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await process.communicate()
    except asyncio.CancelledError:
        # Do not leave an orphaned git process (and its pipes) behind on shutdown.
        with suppress(ProcessLookupError):
            process.kill()
        await process.wait()
        raise
    if process.returncode != 0:
        # Never echo argv (it holds URLs); git's stderr does not contain the auth header.
        raise SCMError(f"git {command} failed ({process.returncode}): {stderr.decode().strip()}")
    return stdout.decode()
