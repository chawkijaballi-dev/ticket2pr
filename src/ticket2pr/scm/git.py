"""Async git wrapper.

Security model: the agent can write anywhere in its workspace, including ``.git/`` (config,
hooks, ``commondir``, alternates, attributes) and, outside a sandbox, ``~/.gitconfig``.
ticket2pr therefore never runs git in the agent's workspace after the agent has started.
Commits and pushes happen in a *publish clone* made after the agent stopped
(``GitRepo.clone`` + ``sync_tree``), and every git command ticket2pr runs:

* ignores system and global configuration (``GIT_CONFIG_NOSYSTEM``, ``GIT_CONFIG_GLOBAL``),
* disables hooks, fsmonitor and external attribute files, and only allows https/file
  transports,
* receives credentials through ``GIT_CONFIG_*`` environment variables, never argv
  (visible to every process) or a config file.
"""

from __future__ import annotations

import asyncio
import base64
import os
import shutil
import stat
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from ticket2pr.domain import ChangeSet
from ticket2pr.errors import PolicyViolationError, SCMError

_SAFE_CONFIG = (
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.fsmonitor=false",
    "-c", "core.attributesFile=/dev/null",
    "-c", "core.quotePath=false",
    "-c", "commit.gpgsign=false",
    "-c", "maintenance.auto=false",  # no detached background maintenance processes
    "-c", "gc.auto=0",
    "-c", "protocol.allow=never",
    "-c", "protocol.https.allow=always",
    "-c", "protocol.file.allow=always",
)  # fmt: skip


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

    @classmethod
    async def clone(
        cls, url: str, dest: Path, *, branch: str, auth: GitAuth | None = None, depth: int = 50
    ) -> GitRepo:
        dest = await asyncio.to_thread(dest.resolve)  # git runs in dest.parent
        dest.parent.mkdir(parents=True, exist_ok=True)
        await _git(
            dest.parent,
            "clone",
            [
                "--quiet",
                f"--depth={depth}",
                "--single-branch",
                "--no-tags",
                f"--branch={branch}",
                "--",
                url,
                str(dest),
            ],
            env=auth.env() if auth else None,
        )
        return cls(dest)

    def config_bytes(self) -> bytes:
        return (self.path / ".git" / "config").read_bytes()

    async def head(self) -> str:
        return (await self._run("rev-parse", "HEAD")).strip()

    async def checkout_new_branch(self, name: str, start: str = "HEAD") -> None:
        await self._run("checkout", "--quiet", "-B", name, start)

    async def stage_all(self) -> ChangeSet:
        """Stage every change in the working tree and describe the staged diff."""
        await self._run("add", "--all")
        return await self.staged_changes()

    async def staged_changes(self) -> ChangeSet:
        # -z: paths are NUL-terminated and never quoted or escaped, whatever they contain.
        # fsdecode: a non-UTF-8 name must round-trip to git and the filesystem unchanged.
        numstat = os.fsdecode(
            await _git_bytes(self.path, "diff", ["--cached", "--numstat", "--no-renames", "-z"])
        )
        files: list[str] = []
        insertions = deletions = 0
        for record in numstat.split("\0"):
            if not record:
                continue
            added, removed, name = record.split("\t", 2)
            files.append(name)
            # Binary files report "-" for both counts.
            insertions += int(added) if added.isdigit() else 0
            deletions += int(removed) if removed.isdigit() else 0
        return ChangeSet(files=tuple(files), insertions=insertions, deletions=deletions)

    async def staged_diff(self, max_bytes: int) -> bytes:
        """The staged change as raw bytes, binary files included (``--text``), for scanning.

        Raises ``PolicyViolationError`` past ``max_bytes`` rather than buffer a huge diff.
        """
        return await _git_bytes(
            self.path,
            "diff",
            ["--cached", "--text", "--no-color", "--no-ext-diff"],
            max_bytes=max_bytes,
        )

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
        return await self.head()

    async def push(self, url: str, branch: str, *, auth: GitAuth | None = None) -> None:
        # Force: the branch belongs to ticket2pr and a re-run replaces the previous attempt.
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
        return await _git(self.path, command, list(args), env=env)


def sync_tree(source: Path, dest: Path) -> None:
    """Make ``dest``'s working tree (not its ``.git``) an exact copy of ``source``'s.

    Symlinks are copied as links, never followed, special files are dropped, and any
    ``.git`` entry at any depth is skipped, so nothing from the agent's git metadata
    reaches the publish clone.
    """
    for entry in dest.iterdir():
        if entry.name == ".git":
            continue
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()
    shutil.copytree(
        source,
        dest,
        symlinks=True,
        dirs_exist_ok=True,
        ignore=_not_publishable,
    )


def _not_publishable(directory: str, names: list[str]) -> list[str]:
    """``.git`` entries, and sockets/FIFOs/devices a test run left behind (git ignores them)."""
    kinds = (stat.S_ISREG, stat.S_ISDIR, stat.S_ISLNK)
    return [
        name
        for name in names
        if name == ".git"
        or not any(is_kind((Path(directory) / name).lstat().st_mode) for is_kind in kinds)
    ]


async def _git(
    cwd: Path, command: str, args: list[str], *, env: dict[str, str] | None = None
) -> str:
    return (await _git_bytes(cwd, command, args, env=env)).decode("utf-8", "replace")


async def _git_bytes(
    cwd: Path,
    command: str,
    args: list[str],
    *,
    env: dict[str, str] | None = None,
    max_bytes: int | None = None,
) -> bytes:
    full_env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_ATTR_NOSYSTEM": "1",
        # Paths come from the agent's workspace: never read ":x/.env" as pathspec magic.
        "GIT_LITERAL_PATHSPECS": "1",
        "LC_ALL": "C",
        **(env or {}),
    }
    process = await asyncio.create_subprocess_exec(
        "git",
        *_SAFE_CONFIG,
        command,
        *args,
        cwd=cwd,
        env=full_env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        if max_bytes is None:
            stdout, stderr = await process.communicate()
        else:
            stdout, stderr = await _communicate_bounded(process, command, max_bytes)
    except (asyncio.CancelledError, PolicyViolationError):
        # Do not leave an orphaned git process (and its pipes) behind.
        with suppress(ProcessLookupError):
            process.kill()
        await process.wait()
        raise
    if process.returncode != 0:
        # Never echo argv (it holds URLs); git's stderr does not contain the auth header.
        message = stderr.decode("utf-8", "replace").strip()
        raise SCMError(f"git {command} failed ({process.returncode}): {message}")
    return stdout


async def _communicate_bounded(
    process: asyncio.subprocess.Process, command: str, max_bytes: int
) -> tuple[bytes, bytes]:
    assert process.stdout is not None  # noqa: S101 - PIPE
    assert process.stderr is not None  # noqa: S101
    stderr = asyncio.create_task(process.stderr.read())
    try:
        await process.stdout.readexactly(max_bytes + 1)
    except asyncio.IncompleteReadError as exc:  # EOF within the limit
        await process.wait()
        return exc.partial, await stderr
    stderr.cancel()
    raise PolicyViolationError(f"git {command} output exceeds {max_bytes} bytes")
