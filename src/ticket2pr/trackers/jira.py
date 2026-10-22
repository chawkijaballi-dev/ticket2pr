"""Jira Cloud tracker (REST API v3, basic auth with an API token)."""

from __future__ import annotations

from typing import Any

import httpx

from ticket2pr.domain import Ticket
from ticket2pr.errors import ConfigurationError, TicketNotFoundError, TrackerError
from ticket2pr.trackers import adf

_FIELDS = "summary,description,labels,issuetype"


class JiraTracker:
    def __init__(
        self,
        *,
        base_url: str,
        email: str,
        api_token: str,
        client: httpx.AsyncClient | None = None,
        timeout_s: float = 30.0,
    ) -> None:
        if not (base_url and email and api_token):
            raise ConfigurationError(
                "Jira needs T2P_JIRA_BASE_URL, T2P_JIRA_EMAIL and T2P_JIRA_API_TOKEN"
            )
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(timeout=timeout_s)
        self._auth = httpx.BasicAuth(email, api_token)

    async def get_ticket(self, key: str) -> Ticket:
        data = await self._request("GET", f"/rest/api/3/issue/{key}", params={"fields": _FIELDS})
        fields: dict[str, Any] = data.get("fields") or {}
        return Ticket(
            key=data.get("key", key),
            title=fields.get("summary") or "",
            description=adf.to_text(fields.get("description")),
            labels=tuple(fields.get("labels") or ()),
            issue_type=(fields.get("issuetype") or {}).get("name") or "Task",
            url=f"{self._base_url}/browse/{key}",
        )

    async def comment(self, key: str, markdown: str) -> None:
        await self._request(
            "POST",
            f"/rest/api/3/issue/{key}/comment",
            json={"body": adf.from_markdown(markdown)},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = await self._client.request(
                method,
                self._base_url + path,
                auth=self._auth,
                headers={"Accept": "application/json"},
                **kwargs,
            )
        except httpx.HTTPError as exc:
            raise TrackerError(f"Jira {method} {path} failed: {exc}") from exc
        if response.status_code == httpx.codes.NOT_FOUND:
            raise TicketNotFoundError(f"Jira: {path} not found (or not visible to this account)")
        if response.is_error:
            raise TrackerError(
                f"Jira {method} {path} returned {response.status_code}: {response.text[:300]}"
            )
        body: dict[str, Any] = response.json() if response.content else {}
        return body
