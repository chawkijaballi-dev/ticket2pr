"""Bitbucket Cloud (bitbucket.org) and Bitbucket Data Center (self-hosted): pull requests.

Both authenticate with an access token (repository, project or workspace token on Cloud;
HTTP access token on Data Center). Drafts need a recent server: a refused draft flag falls back
to a regular pull request.
"""

from __future__ import annotations

from typing import Any

import httpx

from ticket2pr.config import DEFAULT_SCM_API
from ticket2pr.domain import PullRequest
from ticket2pr.errors import SCMError
from ticket2pr.scm.forge import ForgeClient, path_quote, with_draft_fallback
from ticket2pr.scm.git import GitAuth

_CLOUD_API = DEFAULT_SCM_API["bitbucket"]
_SERVER_API_SUFFIX = "/rest/api/1.0"


class BitbucketCloudClient(ForgeClient):
    name = "Bitbucket"

    default_api_url = _CLOUD_API

    @staticmethod
    def _auth_headers(token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    def git_auth(self) -> GitAuth | None:
        return GitAuth(token=self._token, username="x-token-auth")

    def clone_url(self, repo: str) -> str:
        return f"https://bitbucket.org/{repo}.git"

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
        # Workspace and repository slugs are lower case on Bitbucket Cloud, whatever was typed.
        repo, source_repo = repo.lower(), source_repo.lower()
        prs = f"/repositories/{path_quote(*repo.split('/'))}/pullrequests"
        # Branch and repository names are restricted to safe characters (config, pipeline).
        query = (
            f'state="OPEN" AND source.branch.name="{branch}" '
            f'AND source.repository.full_name="{source_repo}" '
            f'AND destination.branch.name="{base}"'
        )
        existing = await self._request("GET", prs, params={"q": query})
        values = existing.get("values", []) if isinstance(existing, dict) else []
        payload: dict[str, Any] = {"title": title, "description": body, "draft": draft}
        if values:
            number = int(values[0]["id"])

            async def update(sent: dict[str, Any]) -> PullRequest:
                return _cloud_pr(await self._request("PUT", f"{prs}/{number}", json=sent))

            return await with_draft_fallback(
                update, payload, draft_key="draft", refused=httpx.codes.BAD_REQUEST
            )

        async def create(sent: dict[str, Any]) -> PullRequest:
            return _cloud_pr(await self._request("POST", prs, json=sent))

        payload |= {
            "source": {"branch": {"name": branch}, "repository": {"full_name": source_repo}},
            "destination": {"branch": {"name": base}},
        }
        return await with_draft_fallback(
            create, payload, draft_key="draft", refused=httpx.codes.BAD_REQUEST
        )


class BitbucketServerClient(ForgeClient):
    name = "Bitbucket Data Center"

    @staticmethod
    def _auth_headers(token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    def git_auth(self) -> GitAuth | None:
        return GitAuth(token=self._token, scheme="Bearer")  # HTTP access tokens, as for the API

    def clone_url(self, repo: str) -> str:
        project, slug = repo.split("/")
        host = self._api_url.removesuffix(_SERVER_API_SUFFIX)
        return f"{host}/scm/{path_quote(project, slug)}.git"

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
        prs = _pull_requests(repo)
        # From the branch's side: one ref has few open pull requests, so no pagination.
        existing = await self._request(
            "GET",
            _pull_requests(source_repo),
            params={"state": "OPEN", "direction": "OUTGOING", "at": f"refs/heads/{branch}"},
        )
        payload: dict[str, Any] = {"title": title, "description": body, "draft": draft}
        values = existing.get("values", []) if isinstance(existing, dict) else []
        found = next(
            (
                pr
                for pr in values
                if pr.get("toRef", {}).get("id") == f"refs/heads/{base}"
                and _repo_of(pr.get("toRef", {})).casefold() == repo.casefold()
            ),
            None,
        )
        if found is not None:
            path = f"{prs}/{int(found['id'])}"
            kept = {
                "version": found["version"],  # optimistic locking
                # Some versions drop the reviewers a PUT leaves out: send them back.
                "reviewers": [
                    {"user": {"name": reviewer["user"]["name"]}}
                    for reviewer in found.get("reviewers", [])
                    if reviewer.get("user", {}).get("name")
                ],
            }

            async def update(sent: dict[str, Any]) -> PullRequest:
                return _server_pr(await self._request("PUT", path, json=sent | kept))

            return await with_draft_fallback(
                update, payload, draft_key="draft", refused=httpx.codes.BAD_REQUEST
            )

        async def create(sent: dict[str, Any]) -> PullRequest:
            return _server_pr(await self._request("POST", prs, json=sent))

        payload |= {
            "fromRef": _ref(branch, source_repo),
            "toRef": _ref(base, repo),
        }
        return await with_draft_fallback(
            create, payload, draft_key="draft", refused=httpx.codes.BAD_REQUEST
        )


def _pull_requests(repo: str) -> str:
    project, slug = repo.split("/")
    return f"/projects/{path_quote(project)}/repos/{path_quote(slug)}/pull-requests"


def _ref(branch: str, repo: str) -> dict[str, Any]:
    project, slug = repo.split("/")
    return {"id": f"refs/heads/{branch}", "repository": {"slug": slug, "project": {"key": project}}}


def _repo_of(ref: dict[str, Any]) -> str:
    repository = ref.get("repository", {})
    return f"{repository.get('project', {}).get('key', '')}/{repository.get('slug', '')}"


def _cloud_pr(data: Any) -> PullRequest:
    return PullRequest(
        number=int(data["id"]),
        url=str(data["links"]["html"]["href"]),
        draft=bool(data.get("draft")),
    )


def _server_pr(data: Any) -> PullRequest:
    links = data.get("links", {}).get("self", [])
    if not links:
        raise SCMError("Bitbucket Data Center returned a pull request without a link")
    return PullRequest(
        number=int(data["id"]), url=str(links[0]["href"]), draft=bool(data.get("draft"))
    )
