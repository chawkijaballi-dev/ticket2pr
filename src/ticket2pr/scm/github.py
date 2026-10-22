"""GitHub REST client: just the pull-request calls ticket2pr needs."""

from __future__ import annotations

from typing import Any

import httpx

from ticket2pr.domain import PullRequest
from ticket2pr.errors import ConfigurationError, SCMError

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
        """Open a PR for ``head``, or refresh the one already open from a previous run."""
        owner = repo.split("/", 1)[0]
        existing = await self._request(
            "GET", f"/repos/{repo}/pulls", params={"head": f"{owner}:{head}", "state": "open"}
        )
        if isinstance(existing, list) and existing:
            number = int(existing[0]["number"])
            updated = await self._request(
                "PATCH", f"/repos/{repo}/pulls/{number}", json={"title": title, "body": body}
            )
            return _to_pr(updated)
        created = await self._request(
            "POST",
            f"/repos/{repo}/pulls",
            json={"title": title, "head": head, "base": base, "body": body, "draft": draft},
        )
        return _to_pr(created)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = await self._client.request(
                method, self._api_url + path, headers=self._headers, **kwargs
            )
        except httpx.HTTPError as exc:
            raise SCMError(f"GitHub {method} {path} failed: {exc}") from exc
        if response.is_error:
            raise SCMError(
                f"GitHub {method} {path} returned {response.status_code}: {response.text[:300]}"
            )
        return response.json()


def _to_pr(data: Any) -> PullRequest:
    return PullRequest(
        number=int(data["number"]), url=str(data["html_url"]), draft=bool(data.get("draft"))
    )
