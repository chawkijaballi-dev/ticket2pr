"""Where repositories live and pull requests are opened.

The forge clients (``ticket2pr.scm.github``, ``gitlab``, ``bitbucket``, ``azure``, ``gitea``)
are the production implementations. ``LocalHost`` serves repositories from a directory and
records "pull requests" as JSON files, for demos and offline evals.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Protocol, runtime_checkable

from ticket2pr.domain import PullRequest
from ticket2pr.errors import SCMError
from ticket2pr.scm.git import GitAuth


@runtime_checkable
class RepoHost(Protocol):
    def clone_url(self, repo: str) -> str: ...

    def git_auth(self) -> GitAuth | None: ...

    async def open_or_update_pr(
        self,
        repo: str,
        *,
        source_repo: str,
        branch: str,
        base: str,
        title: str,
        body: str,
        draft: bool,
    ) -> PullRequest:
        """Open a pull request from ``source_repo``'s ``branch`` into ``repo``'s ``base``, or
        refresh the open one. ``source_repo`` is ``repo``, or the bot's fork (fork mode)."""
        ...

    async def aclose(self) -> None: ...


class LocalHost:
    """Repos are ``<root>/<owner>/<name>`` git repositories; PRs land in ``<root>/_prs/``."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def clone_url(self, repo: str) -> str:
        path = (self._root / repo).resolve()
        if not path.is_relative_to(self._root) or not (path / ".git").exists():
            raise SCMError(f"no local repository for {repo!r} under {self._root}")
        return path.as_uri()

    def git_auth(self) -> GitAuth | None:
        return None

    async def open_or_update_pr(
        self,
        repo: str,
        *,
        source_repo: str,
        branch: str,
        base: str,
        title: str,
        body: str,
        draft: bool,
    ) -> PullRequest:
        prs = self._root / "_prs"
        prs.mkdir(exist_ok=True)
        slug = re.sub(r"[^A-Za-z0-9._-]", "__", f"{repo}__{source_repo}__{branch}")
        path = prs / f"{slug}.json"
        record: dict[str, str | bool] = {"repo": repo, "source_repo": source_repo}
        record |= {"head": branch, "base": base, "title": title, "body": body, "draft": draft}
        path.write_text(json.dumps(record, indent=2), encoding="utf-8")
        number = sorted(p.name for p in prs.glob("*.json")).index(path.name) + 1
        return PullRequest(number=number, url=path.as_uri(), draft=draft)

    async def aclose(self) -> None:
        return None
