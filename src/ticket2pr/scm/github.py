"""GitHub (github.com and GitHub Enterprise Server): just the pull-request calls ticket2pr needs."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from ticket2pr.config import DEFAULT_SCM_API
from ticket2pr.domain import PullRequest
from ticket2pr.errors import SCMError
from ticket2pr.scm.forge import ForgeClient, fork_head, with_draft_fallback

logger = logging.getLogger(__name__)

_API_VERSION = "2022-11-28"
_PUBLIC_API = DEFAULT_SCM_API["github"]


class GitHubClient(ForgeClient):
    name = "GitHub"

    default_api_url = _PUBLIC_API

    @staticmethod
    def _auth_headers(token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": _API_VERSION,
        }

    def clone_url(self, repo: str) -> str:
        """HTTPS clone URL, for github.com or a GitHub Enterprise Server (``<host>/api/v3``)."""
        if self._api_url == _PUBLIC_API:
            return f"https://github.com/{repo}.git"
        return f"{self._api_url.removesuffix('/api/v3')}/{repo}.git"

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
        """Open a PR for ``branch``, or refresh the one already open from a previous run.

        A refreshed PR also gets its draft state synced (red tests → draft, green → ready).
        The REST API cannot change it, hence the GraphQL mutations. When the change is refused
        (draft PRs are unavailable on some plans) the PR keeps its state, and the returned
        ``PullRequest.draft`` says which one it is.
        """
        qualified = f"{source_repo.split('/', 1)[0]}:{branch}"
        head = fork_head(repo, source_repo, branch)
        existing = await self._request(
            "GET", f"/repos/{repo}/pulls", params={"head": qualified, "state": "open"}
        )
        if not (isinstance(existing, list) and existing):

            async def create(payload: dict[str, Any]) -> PullRequest:
                return _to_pr(await self._request("POST", f"/repos/{repo}/pulls", json=payload))

            payload = {"title": title, "head": head, "base": base, "body": body, "draft": draft}
            # Draft PRs are unavailable on some plans: open a regular one rather than none.
            return await with_draft_fallback(
                create, payload, draft_key="draft", refused=httpx.codes.UNPROCESSABLE_ENTITY
            )
        number = int(existing[0]["number"])
        updated = _to_pr(
            await self._request(
                "PATCH", f"/repos/{repo}/pulls/{number}", json={"title": title, "body": body}
            )
        )
        if updated.draft == draft:
            return updated
        try:
            await self._set_draft(str(existing[0]["node_id"]), draft=draft)
        except SCMError as exc:
            logger.warning("could not change the draft state of %s#%s: %s", repo, number, exc)
            return updated
        return updated.model_copy(update={"draft": draft})

    async def _set_draft(self, node_id: str, *, draft: bool) -> None:
        mutation = "convertPullRequestToDraft" if draft else "markPullRequestReadyForReview"
        query = (
            f"mutation($id: ID!) {{ {mutation}(input: {{pullRequestId: $id}}) "
            "{ clientMutationId } }"
        )
        data = await self._request(
            "POST", self._graphql_url, json={"query": query, "variables": {"id": node_id}}
        )
        if isinstance(data, dict) and data.get("errors"):
            raise SCMError(f"GitHub GraphQL {mutation} failed: {data['errors']}")

    @property
    def _graphql_url(self) -> str:
        # Absolute: https://api.github.com/graphql, or https://<host>/api/graphql on GHES.
        if self._api_url == _PUBLIC_API:
            return f"{_PUBLIC_API}/graphql"
        return f"{self._api_url.removesuffix('/api/v3')}/api/graphql"


def _to_pr(data: Any) -> PullRequest:
    return PullRequest(
        number=int(data["number"]), url=str(data["html_url"]), draft=bool(data.get("draft"))
    )
