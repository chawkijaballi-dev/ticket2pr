"""Offline evaluation harness, in the spirit of SWE-bench.

A task directory holds::

    ticket.yaml       the ticket the agent receives (``key`` names it, e.g. SHOP-101)
    repo/             the repository before the fix (committed as the base branch)
    hidden_tests/     tests the agent never sees; they decide whether the task is resolved
    solution/         reference files, overlaid on the repo by the ``oracle`` agent

Each task runs through the real pipeline (local tracker and host), then the hidden tests run
against the pushed branch. The oracle agent validates the dataset and the harness for free;
the Claude agent measures the resolve rate and the cost per resolved ticket.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

from ticket2pr.agent.base import AgentTask, CodingAgent
from ticket2pr.config import Settings
from ticket2pr.domain import AgentOutcome, AgentResult, JobStatus, Usage, is_ticket_key
from ticket2pr.errors import ConfigurationError
from ticket2pr.pipeline import RunOptions
from ticket2pr.runtime import open_runtime
from ticket2pr.sandbox import sandbox_profile
from ticket2pr.verify import run_tests


@dataclass(frozen=True, slots=True)
class EvalTask:
    name: str
    key: str
    path: Path

    @classmethod
    def load(cls, path: Path) -> EvalTask:
        raw: Any = yaml.safe_load((path / "ticket.yaml").read_text(encoding="utf-8"))
        key = raw.get("key") if isinstance(raw, dict) else None
        if not isinstance(key, str) or not is_ticket_key(key):
            raise ConfigurationError(f"{path}/ticket.yaml needs a valid `key`")
        for required in ("repo", "hidden_tests", "solution"):
            if not (path / required).is_dir():
                raise ConfigurationError(f"{path} is missing {required}/")
        return cls(name=path.name, key=key, path=path)


@dataclass(frozen=True, slots=True)
class EvalResult:
    task: str
    ticket: str
    status: str
    resolved: bool
    cost_usd: float
    total_tokens: int
    turns: int
    agent_duration_s: float  # the agent's own runs, not cloning, tests or grading
    error: str | None = None
    cost_capped: bool = False  # cost_usd is the budget cap, not a measurement (e.g. a timeout)


@dataclass(frozen=True, slots=True)
class EvalSummary:
    results: tuple[EvalResult, ...]

    @property
    def resolve_rate(self) -> float:
        return sum(r.resolved for r in self.results) / len(self.results) if self.results else 0.0

    @property
    def total_cost_usd(self) -> float:
        return sum(r.cost_usd for r in self.results)

    @property
    def any_cost_capped(self) -> bool:
        return any(r.cost_capped for r in self.results)

    @property
    def cost_per_resolved_usd(self) -> float | None:
        resolved = sum(r.resolved for r in self.results)
        return self.total_cost_usd / resolved if resolved else None

    def to_json(self) -> str:
        return json.dumps(
            {
                "resolve_rate": self.resolve_rate,
                "total_cost_usd": self.total_cost_usd,
                "cost_per_resolved_usd": self.cost_per_resolved_usd,
                "any_cost_capped": self.any_cost_capped,  # then the costs are upper bounds
                "results": [asdict(result) for result in self.results],
            },
            indent=2,
        )


class OracleAgent:
    """Applies each task's reference solution: proves the task is solvable and graded right."""

    def __init__(self, solutions: dict[str, Path]) -> None:
        self._solutions = solutions

    async def run(self, task: AgentTask) -> AgentResult:
        solution = self._solutions[task.ticket.key]
        shutil.copytree(solution, task.workspace, dirs_exist_ok=True)
        return AgentResult(
            outcome=AgentOutcome.COMPLETED,
            summary="## Summary\nApplied the reference solution.",
            usage=Usage(turns=1),
        )


def discover(tasks_dir: Path, only: list[str] | None = None) -> list[EvalTask]:
    tasks = [
        EvalTask.load(path)
        for path in sorted(tasks_dir.iterdir())
        if path.is_dir() and (path / "ticket.yaml").is_file()
    ]
    if only:
        unknown = set(only) - {task.name for task in tasks}
        if unknown:
            raise ConfigurationError(f"unknown eval task(s): {', '.join(sorted(unknown))}")
        tasks = [task for task in tasks if task.name in only]
    return tasks


async def run_suite(
    tasks: list[EvalTask], *, settings: Settings, agent: CodingAgent | None = None
) -> EvalSummary:
    """Run every task in an isolated scratch directory. ``agent=None`` means the oracle."""
    oracle = OracleAgent({task.key: task.path / "solution" for task in tasks})
    results = []
    for task in tasks:
        with tempfile.TemporaryDirectory(prefix=f"t2p-eval-{task.name}-") as scratch:
            results.append(await _run_task(task, Path(scratch), settings, agent or oracle))
    return EvalSummary(results=tuple(results))


async def _run_task(
    task: EvalTask, scratch: Path, base: Settings, agent: CodingAgent
) -> EvalResult:
    repo_name = f"evals/{task.name}"
    origin = scratch / "repos" / repo_name
    shutil.copytree(task.path / "repo", origin)
    _git(origin, "init", "-q", "-b", base.base_branch)
    _git(origin, "add", "-A")
    _git(origin, "commit", "-qm", "base")
    _git(origin, "config", "receive.denyCurrentBranch", "ignore")
    tickets = scratch / "tickets"
    tickets.mkdir()
    shutil.copy(task.path / "ticket.yaml", tickets / f"{task.key}.yaml")

    pytest_cmd = f"{sys.executable} -m pytest -q -p no:cacheprovider"
    settings = base.model_copy(
        update={
            "data_dir": scratch / "data",
            "repos": {task.key.split("-")[0]: repo_name},
            "test_command": pytest_cmd,
            "fork_owner": "",  # the scratch host has no forks
        }
    )
    async with open_runtime(
        settings, tickets_dir=tickets, repos_dir=scratch / "repos", agent=agent
    ) as runtime:
        job = runtime.store.claim(task.key, repo_name)
        assert job is not None  # noqa: S101 - fresh database
        job = await runtime.pipeline.run(job, RunOptions(require_label=False))

    resolved = False
    if job.pr_url and job.branch:
        checkout = scratch / "grade"
        _git(scratch, "clone", "-q", "--branch", job.branch, origin.as_uri(), str(checkout))
        if not _has_symlink(checkout / "tests"):  # copying through one writes outside scratch
            shutil.copytree(task.path / "hidden_tests", checkout / "tests", dirs_exist_ok=True)
            report = await run_tests(
                pytest_cmd,
                checkout,
                timeout_s=settings.test_timeout_s,
                sandbox=sandbox_profile(settings),
            )
            resolved = report.passed
    return EvalResult(
        task=task.name,
        ticket=task.key,
        status=job.status.value,
        resolved=resolved,
        cost_usd=job.usage.cost_usd,
        cost_capped=job.usage.cost_capped,
        total_tokens=job.usage.total_tokens,
        turns=job.usage.turns,
        agent_duration_s=job.usage.duration_s,
        error=job.error if job.status is JobStatus.FAILED else None,
    )


def _git(cwd: Path, *args: str) -> None:
    # The developer's own git config (commit signing, global hooks) must not break the suite.
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    identity = ["-c", "user.name=ticket2pr-eval", "-c", "user.email=eval@ticket2pr.invalid"]
    subprocess.run(
        ["git", *identity, *args],  # noqa: S607
        cwd=cwd,
        env=env,
        check=True,
        capture_output=True,
    )


def _has_symlink(path: Path) -> bool:
    """``path`` or anything under it is a symlink (the agent committed it: not graded)."""
    return path.is_symlink() or any(entry.is_symlink() for entry in path.rglob("*"))
