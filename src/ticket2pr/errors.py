"""Exception hierarchy: every error ticket2pr raises on purpose derives from ``Ticket2PRError``."""

from __future__ import annotations


class Ticket2PRError(Exception):
    """Base class for all ticket2pr errors."""


class ConfigurationError(Ticket2PRError):
    """The deployment is missing a setting required for the requested operation."""


class TrackerError(Ticket2PRError):
    """The issue tracker (Jira, local file) could not be read or written."""


class TicketNotFoundError(TrackerError):
    """The ticket does not exist or is not visible to the configured account."""


class SCMError(Ticket2PRError):
    """A git or forge (pull-request API) operation failed."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code  # HTTP status, for forge API errors


class StaleBaseError(SCMError):
    """The base branch moved too far during the run; a re-run fixes it."""


class AgentError(Ticket2PRError):
    """The coding agent could not complete its run."""


class PolicyViolationError(Ticket2PRError):
    """The agent's change breaks a guardrail (protected path, too many files, ...)."""
