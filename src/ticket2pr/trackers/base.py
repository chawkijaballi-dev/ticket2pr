"""The interface every issue tracker implements."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ticket2pr.domain import Ticket


@runtime_checkable
class Tracker(Protocol):
    """Reads tickets and reports back on them."""

    async def get_ticket(self, key: str) -> Ticket:
        """Fetch one ticket. Raises ``TicketNotFoundError`` when it does not exist."""
        ...

    async def comment(self, key: str, markdown: str) -> None:
        """Post a comment on the ticket. ``markdown`` uses a small, portable subset."""
        ...

    async def aclose(self) -> None:
        """Release network resources."""
        ...
