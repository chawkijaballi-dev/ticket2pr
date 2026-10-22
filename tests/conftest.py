"""Shared fixtures: an offline world with a local git host, YAML tickets and a scripted agent."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from ticket2pr.agent.base import AgentTask
from ticket2pr.config import Settings
from ticket2pr.domain import AgentOutcome, AgentResult, Job, Usage
from ticket2pr.pipeline import Pipeline
from ticket2pr.scm.host import LocalHost
from ticket2pr.store import JobStore
from ticket2pr.trackers.local import LocalTracker

REPO = "acme/calc"

CALC_PY = """\
def add(a, b):
    return a - b  # bug: should add
"""

TEST_CALC_PY = """\
from calc import add


def test_add():
    assert add(2, 3) == 5
"""


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep the developer's real configuration (and .env) out of every test."""
    monkeypatch.chdir(tmp_path)
    for name in list(os.environ):
        if name.startswith("T2P_"):
            monkeypatch.delenv(name)
    # The shared settings fixture disables the sandbox; CLI tests build Settings from the
    # environment, so they get the same default. tests/test_sandbox.py covers the sandbox.
    monkeypatch.setenv("T2P_SANDBOX", "false")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    (tmp_path / "gitconfig").write_text(
        "[user]\n\tname = Test\n\temail = test@example.com\n[init]\n\tdefaultBranch = main\n"
    )


@pytest.fixture
def repos_dir(tmp_path: Path) -> Path:
    """``<repos_dir>/acme/calc``: a tiny Python project whose single test fails."""
    root = tmp_path / "repos"
    repo = root / REPO
    repo.mkdir(parents=True)
    (repo / "calc.py").write_text(CALC_PY)
    (repo / "tests").mkdir()
    (repo / "tests" / "test_calc.py").write_text(TEST_CALC_PY)
    (repo / "pyproject.toml").write_text('[tool.pytest.ini_options]\npythonpath = ["."]\n')
    (repo / "AGENTS.md").write_text("Keep functions pure.\n")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    # Let the pipeline push to a checked-out branch of this non-bare repo.
    git(repo, "config", "receive.denyCurrentBranch", "ignore")
    return root


@pytest.fixture
def tickets_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "tickets"
    directory.mkdir()
    (directory / "CALC-1.yaml").write_text(
        "title: add() subtracts instead of adding\n"
        "description: |\n  `add(2, 3)` returns -1.\n"
        "labels: [ticket2pr]\n"
    )
    (directory / "CALC-2.yaml").write_text("title: Unlabelled ticket\nlabels: []\n")
    return directory


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        repos={"CALC": REPO},
        test_command=f"{sys.executable} -m pytest -q -p no:cacheprovider",
        sandbox=False,  # the sandbox itself is covered by tests/test_sandbox.py
        max_budget_usd=2.0,
        _env_file=None,
    )


Edit = Callable[[AgentTask], None]
MakePipeline = Callable[..., Pipeline]


@dataclass
class ScriptedAgent:
    """A ``CodingAgent`` that applies one scripted edit per call and records its tasks."""

    edits: list[Edit]
    outcome: AgentOutcome = AgentOutcome.COMPLETED
    cost_per_run: float = 0.5
    tasks: list[AgentTask] = field(default_factory=list)

    async def run(self, task: AgentTask) -> AgentResult:
        self.tasks.append(task)
        edit = self.edits[min(len(self.tasks), len(self.edits)) - 1]
        edit(task)
        return AgentResult(
            outcome=self.outcome,
            summary="## Summary\nFixed add().",
            usage=Usage(input_tokens=1000, output_tokens=200, cost_usd=self.cost_per_run, turns=4),
            denied_actions=("Bash: `git push` is reserved for the harness",),
        )


def fix_add(task: AgentTask) -> None:
    (task.workspace / "calc.py").write_text("def add(a, b):\n    return a + b\n")


def break_differently(task: AgentTask) -> None:
    (task.workspace / "calc.py").write_text("def add(a, b):\n    return a * b\n")


def do_nothing(task: AgentTask) -> None:
    return None


def claim(store: JobStore, key: str) -> Job:
    job = store.claim(key, None)
    assert job is not None
    return job


@pytest.fixture
def store(settings: Settings) -> JobStore:
    return JobStore(settings.db_path)


@pytest.fixture
def make_pipeline(
    settings: Settings, store: JobStore, repos_dir: Path, tickets_dir: Path
) -> MakePipeline:
    def factory(agent: ScriptedAgent, **overrides: object) -> Pipeline:
        effective = settings.model_copy(update=overrides) if overrides else settings
        return Pipeline(
            settings=effective,
            tracker=LocalTracker(tickets_dir),
            host=LocalHost(repos_dir),
            agent=agent,
            store=store,
        )

    return factory
