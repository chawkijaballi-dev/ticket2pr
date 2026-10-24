"""GitLab (gitlab.com and self-managed): merge requests.

GitLab has no draft flag in its API: a draft is a merge request whose title starts with
``Draft:``. Fork mode creates the merge request on the fork, targeting the upstream project.
"""

from __future__ import annotations

from typing import Any

from ticket2pr.config import DEFAULT_SCM_API
from ticket2pr.domain import PullRequest
from ticket2pr.scm.forge import ForgeClient, path_quote
from ticket2pr.scm.git import GitAuth

_PUBLIC_API = DEFAULT_SCM_API["gitlab"]
_DRAFT_PREFIX = "Draft: "


class GitLabClient(ForgeClient):
    name = "GitLab"

    default_api_url = _PUBLIC_API

    @staticmethod
    def _auth_headers(token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    def git_auth(self) -> GitAuth | None:
        return GitAuth(token=self._token, username="oauth2")

    def clone_url(self, repo: str) -> str:
        return f"{self._api_url.removesuffix('/api/v4')}/{repo}.git"

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
        target = f"/projects/{path_quote(repo)}"
        full_title = _DRAFT_PREFIX + title if draft else title
        source_id: int | None = None
        if source_repo != repo:
            source_id = int(
                (await self._request("GET", f"/projects/{path_quote(source_repo)}"))["id"]
            )
        existing = await self._request(
            "GET",
            f"{target}/merge_requests",
            params={"state": "opened", "source_branch": branch, "target_branch": base},
        )
        for mr in existing or []:
            # Without a fork, the merge request must come from the project itself: one from
            # someone's fork with the same (predictable) branch name is not ours.
            wanted = source_id if source_id is not None else mr.get("target_project_id")
            if mr.get("source_project_id") == wanted:
                data = await self._request(
                    "PUT",
                    f"{target}/merge_requests/{int(mr['iid'])}",
                    json={"title": full_title, "description": body},
                )
                return _to_pr(data)
        payload: dict[str, Any] = {
            "source_branch": branch,
            "target_branch": base,
            "title": full_title,
            "description": body,
        }
        if source_id is None:
            return _to_pr(await self._request("POST", f"{target}/merge_requests", json=payload))
        # A fork's merge request is created on the fork, pointing at the upstream project.
        target_id = int((await self._request("GET", target))["id"])
        data = await self._request(
            "POST",
            f"/projects/{source_id}/merge_requests",
            json={**payload, "target_project_id": target_id},
        )
        return _to_pr(data)


def _to_pr(data: Any) -> PullRequest:
    title = str(data.get("title", ""))
    draft = data.get("draft", data.get("work_in_progress", title.startswith(_DRAFT_PREFIX)))
    return PullRequest(number=int(data["iid"]), url=str(data["web_url"]), draft=bool(draft))
