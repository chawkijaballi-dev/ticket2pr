"""Gitea and Forgejo (Codeberg included): pull requests.

A draft is a pull request whose title starts with ``WIP:``. Gitea cannot filter open pull
requests by head branch, so the lookup pages through them.
"""

from __future__ import annotations

from typing import Any

import httpx

from ticket2pr.domain import PullRequest
from ticket2pr.scm.forge import ForgeClient, path_quote

_DRAFT_PREFIX = "WIP: "
_PAGE_SIZE = 50
_MAX_PAGES = 20  # 1,000 open pull requests


class GiteaClient(ForgeClient):
    name = "Gitea"

    def __init__(
        self,
        *,
        token: str,
        api_url: str,
        client: httpx.AsyncClient | None = None,
        timeout_s: float = 30.0,
    ) -> None:
        super().__init__(
            token=token,
            api_url=api_url,
            headers={"Authorization": f"token {token}"},
            client=client,
            timeout_s=timeout_s,
        )

    def clone_url(self, repo: str) -> str:
        return f"{self._api_url.removesuffix('/api/v1')}/{repo}.git"

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
        pulls = f"/repos/{path_quote(*repo.split('/'))}/pulls"
        full_title = _DRAFT_PREFIX + title if draft else title
        for page in range(1, _MAX_PAGES + 1):
            batch = await self._request(
                "GET", pulls, params={"state": "open", "limit": _PAGE_SIZE, "page": page}
            )
            for pr in batch or []:
                head, target = pr.get("head") or {}, pr.get("base") or {}
                head_repo = (head.get("repo") or {}).get("full_name")
                if (
                    head.get("ref") == branch
                    and head_repo == source_repo
                    and target.get("ref") == base
                ):
                    data = await self._request(
                        "PATCH",
                        f"{pulls}/{int(pr['number'])}",
                        json={"title": full_title, "body": body},
                    )
                    return _to_pr(data)
            if len(batch or []) < _PAGE_SIZE:
                break
        head = branch if source_repo == repo else f"{source_repo.split('/', 1)[0]}:{branch}"
        payload = {"head": head, "base": base, "title": full_title, "body": body}
        return _to_pr(await self._request("POST", pulls, json=payload))


def _to_pr(data: Any) -> PullRequest:
    title = str(data.get("title", ""))
    draft = title.startswith(_DRAFT_PREFIX) or bool(data.get("draft"))
    return PullRequest(number=int(data["number"]), url=str(data["html_url"]), draft=draft)
