"""File-based tracker for local runs, demos and evals: one YAML file per ticket.

``<dir>/PROJ-42.yaml``::

    title: Fix the off-by-one in pagination
    description: |
      Page 2 repeats the last item of page 1.
    labels: [ticket2pr]

Comments are appended to ``<dir>/PROJ-42.comments.md``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from ticket2pr.domain import Ticket, is_ticket_key
from ticket2pr.errors import TicketNotFoundError, TrackerError


class LocalTracker:
    def __init__(self, directory: Path) -> None:
        self._dir = directory

    async def get_ticket(self, key: str) -> Ticket:
        path = self._path(key, ".yaml")
        if not path.is_file():
            raise TicketNotFoundError(f"no ticket file at {path}")
        try:
            raw: Any = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise TrackerError(f"{path}: invalid YAML: {exc}") from exc
        if not isinstance(raw, dict):
            raise TrackerError(f"{path}: expected a mapping at the top level")
        try:
            return Ticket(
                key=key,
                title=str(raw.get("title") or ""),  # `title:` alone is YAML null
                description=str(raw.get("description") or ""),
                labels=tuple(str(label) for label in raw.get("labels") or ()),
                issue_type=str(raw.get("type") or "Task"),
            )
        except ValidationError as exc:
            raise TrackerError(f"{path}: {exc}") from exc

    async def comment(self, key: str, markdown: str) -> None:
        path = self._path(key, ".comments.md")
        with path.open("a", encoding="utf-8") as handle:
            handle.write(markdown.rstrip() + "\n\n---\n\n")

    async def aclose(self) -> None:
        return None

    def _path(self, key: str, suffix: str) -> Path:
        # Keys reach the filesystem: refuse anything that is not a plain Jira key.
        if not is_ticket_key(key):
            raise TicketNotFoundError(f"invalid ticket key: {key!r}")
        return self._dir / f"{key}{suffix}"
