"""The interface between the pipeline and whatever writes the code."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from ticket2pr.domain import AgentResult, Ticket


@dataclass(frozen=True, slots=True)
class AgentTask:
    ticket: Ticket
    workspace: Path
    test_command: str | None = None
    repo_guidelines: str = ""
    feedback: str = ""  # failing test output from a previous attempt, if any
    budget_usd: float | None = None  # spend cap for this run; ``None`` = the agent's default


@runtime_checkable
class CodingAgent(Protocol):
    async def run(self, task: AgentTask) -> AgentResult:
        """Edit ``task.workspace`` to resolve ``task.ticket``. Must not commit or push."""
        ...
