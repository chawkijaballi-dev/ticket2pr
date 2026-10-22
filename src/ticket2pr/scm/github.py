"""GitHub REST client: just the pull-request calls ticket2pr needs."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from ticket2pr.domain import PullRequest
from ticket2pr.errors import ConfigurationError, SCMError

logger = logging.getLogger(__name__)

_API_VERSION = "2022-11-28"
_PUBLIC_API = "https://api.github.com"


class GitHubClient:
    def __init__(
        self,
        *,
        token: str,
        api_url: str = _PUBLIC_API,
        client: httpx.AsyncClient | None = None,
        timeout_s: float = 30.0,
    ) -> None:
        if not token:
            raise ConfigurationError("GitHub needs T2P_GITHUB_TOKEN")
        self._api_url = api_url.rstrip("/")
        self._client = client or httpx.AsyncClient(timeout=timeout_s)
        self._headers = {
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
        self, repo: str, *, head: str, base: str, title: str, body: str, draft: bool
    ) -> PullRequest:
        """Open a PR for ``head``, or refresh the one already open from a previous run.

        A refreshed PR also gets its draft state synced (red tests → draft, green → ready).
        The REST API cannot change it, hence the GraphQL mutations. When the change is refused
        (draft PRs are unavailable on some plans) the PR keeps its state, and the returned
        ``PullRequest.draft`` says which one it is.
        """
        # ``head`` is a branch of ``repo``, or ``owner:branch`` for a branch of a fork.
        qualified = head if ":" in head else f"{repo.split('/', 1)[0]}:{head}"
        existing = await self._request(
            "GET", f"/repos/{repo}/pulls", params={"head": qualified, "state": "open"}
        )
        if not (isinstance(existing, list) and existing):
            payload = {"title": title, "head": head, "base": base, "body": body, "draft": draft}
            try:
                return _to_pr(await self._request("POST", f"/repos/{repo}/pulls", json=payload))
            except SCMError as exc:
                if not (draft and exc.status_code == httpx.codes.UNPROCESSABLE_ENTITY):
                    raise
                # Draft PRs are unavailable on some plans: open a regular one rather than none.
                logger.warning("draft PRs refused on %s, opening a regular PR: %s", repo, exc)
                payload["draft"] = False
                return _to_pr(await self._request("POST", f"/repos/{repo}/pulls", json=payload))
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

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            url = path if path.startswith(("https://", "http://")) else self._api_url + path
            response = await self._client.request(method, url, headers=self._headers, **kwargs)
        except httpx.HTTPError as exc:
            raise SCMError(f"GitHub {method} {path} failed: {exc}") from exc
        if response.is_error:
            raise SCMError(
                f"GitHub {method} {path} returned {response.status_code}: {response.text[:300]}",
                status_code=response.status_code,
            )
        return response.json()


def _to_pr(data: Any) -> PullRequest:
    return PullRequest(
        number=int(data["number"]), url=str(data["html_url"]), draft=bool(data.get("draft"))
    )
