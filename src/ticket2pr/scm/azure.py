"""Azure DevOps Services (dev.azure.com) and Azure DevOps Server: pull requests.

Repositories are ``<organization>/<project>/<repository>``. On a Server, the API URL is the
server (``https://<host>``, or ``https://<host>/tfs``) and the collection takes the
organization's place. A personal access token authenticates both the API and git.
Fork pull requests are not supported (ticket2pr refuses ``T2P_FORK_OWNER`` with this forge).
"""

from __future__ import annotations

import base64
from typing import Any

import httpx

from ticket2pr.config import DEFAULT_SCM_API
from ticket2pr.domain import PullRequest
from ticket2pr.scm.forge import ForgeClient, path_quote, with_draft_fallback
from ticket2pr.scm.git import GitAuth

_SERVICES_API = DEFAULT_SCM_API["azure"]
_API_VERSION = "7.1"
_MAX_DESCRIPTION = 4_000  # Azure DevOps refuses longer pull-request descriptions
_TRUNCATED = "\n\n_(truncated: Azure DevOps limits descriptions to 4,000 characters)_"


class AzureDevOpsClient(ForgeClient):
    name = "Azure DevOps"

    default_api_url = _SERVICES_API

    @staticmethod
    def _auth_headers(token: str) -> dict[str, str]:
        basic = base64.b64encode(f":{token}".encode()).decode()
        return {"Authorization": f"Basic {basic}"}

    def git_auth(self) -> GitAuth | None:
        return GitAuth(token=self._token, username="")  # a PAT takes any user name

    def clone_url(self, repo: str) -> str:
        organization, project, name = repo.split("/")
        return f"{self._api_url}/{path_quote(organization, project)}/_git/{path_quote(name)}"

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
        organization, project, name = repo.split("/")
        web = self.clone_url(repo)
        prs = (
            f"/{path_quote(organization, project)}/_apis/git/repositories/"
            f"{path_quote(name)}/pullrequests"
        )
        version = {"api-version": _API_VERSION}
        existing = await self._request(
            "GET",
            prs,
            params={
                **version,
                "searchCriteria.sourceRefName": f"refs/heads/{branch}",
                "searchCriteria.targetRefName": f"refs/heads/{base}",
                "searchCriteria.status": "active",
            },
        )
        if len(body) > _MAX_DESCRIPTION:
            body = body[: _MAX_DESCRIPTION - len(_TRUNCATED)] + _TRUNCATED
        payload: dict[str, Any] = {"title": title, "description": body, "isDraft": draft}
        values = existing.get("value", []) if isinstance(existing, dict) else []
        # ticket2pr never opens fork pull requests here: one from a fork is someone else's.
        values = [pr for pr in values if not pr.get("forkSource")]

        def to_pr(data: Any) -> PullRequest:
            number = int(data["pullRequestId"])
            return PullRequest(
                number=number, url=f"{web}/pullrequest/{number}", draft=bool(data.get("isDraft"))
            )

        if values:
            number = int(values[0]["pullRequestId"])

            async def update(sent: dict[str, Any]) -> PullRequest:
                path = f"{prs}/{number}"
                return to_pr(await self._request("PATCH", path, params=version, json=sent))

            return await with_draft_fallback(
                update, payload, draft_key="isDraft", refused=httpx.codes.BAD_REQUEST
            )

        async def create(sent: dict[str, Any]) -> PullRequest:
            return to_pr(await self._request("POST", prs, params=version, json=sent))

        payload |= {"sourceRefName": f"refs/heads/{branch}", "targetRefName": f"refs/heads/{base}"}
        return await with_draft_fallback(
            create, payload, draft_key="isDraft", refused=httpx.codes.BAD_REQUEST
        )
