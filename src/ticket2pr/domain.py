"""Domain models shared by every layer. Pure data, no I/O."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]+-[1-9][0-9]*$")
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def utcnow() -> datetime:
    return datetime.now(UTC)


def is_ticket_key(value: str) -> bool:
    """True for Jira-style keys such as ``PROJ-42``."""
    return bool(_KEY_RE.fullmatch(value))


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Ticket(_Frozen):
    key: str = Field(pattern=_KEY_RE.pattern)
    title: str
    description: str = ""
    labels: tuple[str, ...] = ()
    issue_type: str = "Task"
    url: str | None = None

    def branch_slug(self, max_len: int = 40) -> str:
        """``PROJ-42`` + "Fix login bug!" → ``proj-42-fix-login-bug``."""
        slug = _SLUG_RE.sub("-", f"{self.key} {self.title}".lower()).strip("-")
        if len(slug) <= max_len:
            return slug
        cut = slug[: max_len + 1].rsplit("-", 1)[0]  # end on a word boundary
        return cut if len(cut) > len(self.key) else slug[:max_len].rstrip("-")


class Usage(_Frozen):
    """Tokens and dollars spent by one agent run."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cost_usd: float = 0.0
    turns: int = 0
    duration_s: float = 0.0
    cost_capped: bool = False  # the exact cost is unknown; ``cost_usd`` is the budget cap

    def __add__(self, other: Usage) -> Usage:
        summed = {
            name: getattr(self, name) + getattr(other, name)
            for name in type(self).model_fields
            if name != "cost_capped"
        }
        return Usage(**summed, cost_capped=self.cost_capped or other.cost_capped)

    @property
    def cost_label(self) -> str:
        return f"≤ ${self.cost_usd:.2f}" if self.cost_capped else f"${self.cost_usd:.2f}"

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_creation_tokens
        )


class AgentOutcome(StrEnum):
    COMPLETED = "completed"
    BUDGET_EXCEEDED = "budget_exceeded"
    MAX_TURNS = "max_turns"
    TIMEOUT = "timeout"
    ERROR = "error"


class AgentResult(_Frozen):
    outcome: AgentOutcome
    summary: str = ""
    usage: Usage = Usage()
    session_id: str | None = None
    denied_actions: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.outcome is AgentOutcome.COMPLETED


class TestReport(_Frozen):
    __test__: ClassVar[bool] = False  # not a pytest test class, despite the name

    command: str | None
    passed: bool
    exit_code: int | None = None
    output_tail: str = ""
    duration_s: float = 0.0

    @property
    def ran(self) -> bool:
        return self.command is not None


class ChangeSet(_Frozen):
    files: tuple[str, ...]
    insertions: int = 0
    deletions: int = 0

    @property
    def empty(self) -> bool:
        return not self.files


class PullRequest(_Frozen):
    number: int
    url: str
    draft: bool = False


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"  # PR opened, tests green
    NEEDS_REVIEW = "needs_review"  # draft PR opened, tests red or not run
    FAILED = "failed"  # no PR opened
    SKIPPED = "skipped"  # nothing to do (label missing, duplicate, ...)

    @property
    def terminal(self) -> bool:
        return self not in {JobStatus.QUEUED, JobStatus.RUNNING}


class Job(BaseModel):
    """One processing attempt of one ticket. Mutable: the pipeline advances it."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    id: int | None = None
    ticket_key: str
    repo: str | None = None
    status: JobStatus = JobStatus.QUEUED
    branch: str | None = None
    pr_url: str | None = None
    usage: Usage = Usage()
    tests_passed: bool | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
