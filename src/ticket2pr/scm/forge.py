"""What every forge client shares: an authenticated JSON API and the draft-refusal fallback.

Each client is a ``RepoHost``: it knows its clone URLs, how git authenticates, and how to open
or refresh a pull (or merge) request.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote

import httpx

from ticket2pr.domain import PullRequest
from ticket2pr.errors import ConfigurationError, SCMError
from ticket2pr.scm.git import GitAuth

logger = logging.getLogger(__name__)


class ForgeClient:
    name = "forge"

    def __init__(
        self,
        *,
        token: str,
        api_url: str,
        headers: dict[str, str],
        client: httpx.AsyncClient | None = None,
        timeout_s: float = 30.0,
    ) -> None:
        if not token:
            raise ConfigurationError(f"{self.name} needs T2P_SCM_TOKEN")
        if not api_url:
            raise ConfigurationError(f"{self.name} needs T2P_SCM_API_URL")
        self._api_url = api_url.rstrip("/")
        self._token = token
        self._headers = headers
        self._client = client or httpx.AsyncClient(timeout=timeout_s)

    def git_auth(self) -> GitAuth | None:
        return GitAuth(token=self._token)

    def clone_url(self, repo: str) -> str:
        raise NotImplementedError

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
        raise NotImplementedError

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        url = path if path.startswith(("https://", "http://")) else self._api_url + path
        try:
            response = await self._client.request(method, url, headers=self._headers, **kwargs)
        except httpx.HTTPError as exc:
            raise SCMError(f"{self.name} {method} {path} failed: {exc}") from exc
        if response.is_error:
            raise SCMError(
                f"{self.name} {method} {path} returned {response.status_code}: "
                f"{response.text[:300]}",
                status_code=response.status_code,
            )
        return response.json() if response.content else None


async def with_draft_fallback(
    send: Callable[[dict[str, Any]], Awaitable[PullRequest]],
    payload: dict[str, Any],
    *,
    draft_key: str,
    refused: int,
) -> PullRequest:
    """Send ``payload``; if the forge refuses its draft flag (old server, plan), send it without.

    A regular PR beats none, and the returned ``PullRequest.draft`` says which one it is.
    """
    try:
        return await send(payload)
    except SCMError as exc:
        if not (payload.get(draft_key) and exc.status_code == refused):
            raise
        logger.warning("draft state refused, sending without it: %s", exc)
        return await send({key: value for key, value in payload.items() if key != draft_key})


def path_quote(*segments: str) -> str:
    """URL path segments, each fully quoted (``/`` included)."""
    return "/".join(quote(segment, safe="") for segment in segments)
