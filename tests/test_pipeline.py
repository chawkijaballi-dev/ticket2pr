from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest

from conftest import (
    REPO,
    MakePipeline,
    ScriptedAgent,
    break_differently,
    claim,
    do_nothing,
    fix_add,
    git,
)
from ticket2pr.agent.base import AgentTask
from ticket2pr.config import Settings
from ticket2pr.domain import AgentOutcome, Job, JobStatus
from ticket2pr.pipeline import RunOptions
from ticket2pr.store import JobStore


def _pr(job: Job) -> dict[str, Any]:
    """The pull request ``LocalHost`` recorded for the job."""
    assert job.pr_url is not None
    record: dict[str, Any] = json.loads(Path(urlparse(job.pr_url).path).read_text())
    return record


def _branch(job: Job) -> str:
    assert job.branch is not None
    return job.branch


async def test_happy_path_opens_pr_and_comments(
    make_pipeline: MakePipeline, store: JobStore, repos_dir: Path, tickets_dir: Path
) -> None:
    agent = ScriptedAgent([fix_add])
    job = claim(store, "CALC-1")
    assert job is not None

    job = await make_pipeline(agent).run(job)

    assert job.status is JobStatus.SUCCEEDED, job.error
    assert job.tests_passed is True
    assert job.branch == "ticket2pr/calc-1-add-subtracts-instead-of-adding"
    assert job.usage.cost_usd == pytest.approx(0.5)
    pr = _pr(job)
    assert pr["draft"] is False
    assert pr["title"] == "[CALC-1] add() subtracts instead of adding"
    assert "✅ passed" in str(pr["body"])
    assert "git push" in str(pr["body"])  # guardrail events are surfaced to reviewers
    # The branch really landed on the "remote", authored by the bot identity.
    origin = repos_dir / REPO
    author, subject = (
        git(origin, "log", "-1", "--format=%an <%ae>|%s", _branch(job)).strip().split("|")
    )
    assert author == "ticket2pr <ticket2pr@users.noreply.github.com>"
    assert subject == "CALC-1: add() subtracts instead of adding"
    assert "return a + b" in git(origin, "show", f"{_branch(job)}:calc.py")
    comments = (tickets_dir / "CALC-1.comments.md").read_text()
    assert "opened a pull request" in comments
    assert "Tests pass" in comments
    # The agent got the repo guidelines and the test command.
    assert "Keep functions pure." in agent.tasks[0].repo_guidelines
    assert agent.tasks[0].test_command is not None
    assert agent.tasks[0].budget_usd == pytest.approx(2.0)
    assert job.id is not None
    stored = store.get(job.id)
    assert stored is not None
    assert stored.status is JobStatus.SUCCEEDED


async def test_failing_tests_trigger_one_retry_with_feedback_then_draft(
    make_pipeline: MakePipeline, store: JobStore
) -> None:
    agent = ScriptedAgent([break_differently, break_differently])
    job = await make_pipeline(agent).run(claim(store, "CALC-1"))

    assert job.status is JobStatus.NEEDS_REVIEW
    assert job.tests_passed is False
    assert len(agent.tasks) == 2
    assert "assert" in agent.tasks[1].feedback
    assert agent.tasks[1].budget_usd == pytest.approx(1.5)  # only what the first run left
    assert job.usage.cost_usd == pytest.approx(1.0)
    pr = _pr(job)
    assert pr["draft"] is True
    assert "❌ failed" in str(pr["body"])


async def test_retry_can_fix_the_tests(make_pipeline: MakePipeline, store: JobStore) -> None:
    agent = ScriptedAgent([break_differently, fix_add])
    job = await make_pipeline(agent).run(claim(store, "CALC-1"))
    assert job.status is JobStatus.SUCCEEDED
    assert len(agent.tasks) == 2


async def test_no_retry_when_budget_is_spent(make_pipeline: MakePipeline, store: JobStore) -> None:
    agent = ScriptedAgent([break_differently], cost_per_run=1.9)
    job = await make_pipeline(agent).run(claim(store, "CALC-1"))
    assert len(agent.tasks) == 1
    assert job.status is JobStatus.NEEDS_REVIEW


async def test_agent_without_changes_fails_and_reports(
    make_pipeline: MakePipeline, store: JobStore, tickets_dir: Path
) -> None:
    job = await make_pipeline(ScriptedAgent([do_nothing])).run(claim(store, "CALC-1"))
    # Tests fail on the untouched repo, so the agent gets a retry before giving up.
    assert job.status is JobStatus.FAILED
    assert job.error == "the agent finished without changing any file"
    assert job.pr_url is None
    assert "could not open a pull request" in (tickets_dir / "CALC-1.comments.md").read_text()


async def test_agent_error_outcome_fails_job(make_pipeline: MakePipeline, store: JobStore) -> None:
    agent = ScriptedAgent([fix_add], outcome=AgentOutcome.BUDGET_EXCEEDED)
    job = await make_pipeline(agent).run(claim(store, "CALC-1"))
    assert job.status is JobStatus.FAILED
    assert "budget_exceeded" in (job.error or "")
    assert job.usage.cost_usd == pytest.approx(0.5)  # spend is recorded even on failure


async def test_protected_paths_are_never_committed(
    make_pipeline: MakePipeline, store: JobStore, repos_dir: Path
) -> None:
    def sneaky(task: AgentTask) -> None:
        fix_add(task)
        (task.workspace / ".github" / "workflows").mkdir(parents=True)
        (task.workspace / ".github" / "workflows" / "ci.yml").write_text("on: push\n")
        (task.workspace / ".env").write_text("SECRET=1\n")

    job = await make_pipeline(ScriptedAgent([sneaky])).run(claim(store, "CALC-1"))

    assert job.status is JobStatus.SUCCEEDED
    files = git(repos_dir / REPO, "show", "--name-only", "--format=", _branch(job)).split()
    assert files == ["calc.py"]
    body = str(_pr(job)["body"])
    assert ".github/workflows/ci.yml" in body
    assert ".env" in body


async def test_git_config_tampering_is_reverted_before_push(
    make_pipeline: MakePipeline, store: JobStore, repos_dir: Path, tmp_path: Path
) -> None:
    attacker = tmp_path / "attacker.git"
    git(tmp_path, "init", "-q", "--bare", str(attacker))

    def tamper(task: AgentTask) -> None:
        fix_add(task)
        origin = (repos_dir / REPO).resolve().as_uri()
        git(task.workspace, "config", f"url.{attacker.as_uri()}.insteadOf", origin)
        hook = task.workspace / ".git" / "hooks" / "pre-push"
        hook.write_text("#!/bin/sh\ntouch /tmp/ticket2pr-pwned\n")
        hook.chmod(0o755)

    job = await make_pipeline(ScriptedAgent([tamper])).run(claim(store, "CALC-1"))

    assert job.status is JobStatus.SUCCEEDED
    assert git(attacker, "branch", "--list").strip() == ""
    assert _branch(job) in git(repos_dir / REPO, "branch", "--list")
    assert not Path("/tmp/ticket2pr-pwned").exists()


async def test_too_many_files_is_a_policy_violation(
    make_pipeline: MakePipeline, store: JobStore
) -> None:
    def sprawl(task: AgentTask) -> None:
        fix_add(task)
        for index in range(3):
            (task.workspace / f"extra_{index}.py").write_text("x = 1\n")

    pipeline = make_pipeline(ScriptedAgent([sprawl]), max_changed_files=2)
    job = await pipeline.run(claim(store, "CALC-1"))
    assert job.status is JobStatus.FAILED
    assert "touches 4 files (limit 2)" in (job.error or "")


async def test_unlabelled_ticket_is_skipped_without_comment(
    make_pipeline: MakePipeline, store: JobStore, tickets_dir: Path
) -> None:
    agent = ScriptedAgent([fix_add])
    job = await make_pipeline(agent).run(claim(store, "CALC-2"))
    assert job.status is JobStatus.SKIPPED
    assert agent.tasks == []
    assert not (tickets_dir / "CALC-2.comments.md").exists()


async def test_label_check_can_be_disabled(make_pipeline: MakePipeline, store: JobStore) -> None:
    job = await make_pipeline(ScriptedAgent([fix_add])).run(
        claim(store, "CALC-2"), RunOptions(require_label=False)
    )
    assert job.status is JobStatus.SUCCEEDED


async def test_unmapped_project_fails(
    make_pipeline: MakePipeline, store: JobStore, tickets_dir: Path
) -> None:
    (tickets_dir / "OTHER-1.yaml").write_text("title: x\nlabels: [ticket2pr]\n")
    job = await make_pipeline(ScriptedAgent([fix_add])).run(claim(store, "OTHER-1"))
    assert job.status is JobStatus.FAILED
    assert "no repository mapped for OTHER-1" in (job.error or "")


async def test_missing_ticket_fails(make_pipeline: MakePipeline, store: JobStore) -> None:
    job = await make_pipeline(ScriptedAgent([fix_add])).run(claim(store, "CALC-99"))
    assert job.status is JobStatus.FAILED
    assert "no ticket file" in (job.error or "")


async def test_unexpected_exception_is_recorded(
    make_pipeline: MakePipeline, store: JobStore
) -> None:
    def boom(task: AgentTask) -> None:
        raise RuntimeError("kaboom")

    job = await make_pipeline(ScriptedAgent([boom])).run(claim(store, "CALC-1"))
    assert job.status is JobStatus.FAILED
    assert job.error == "internal error: RuntimeError: kaboom"


async def test_workspace_is_removed_unless_kept(
    make_pipeline: MakePipeline, store: JobStore, settings: Settings
) -> None:
    pipeline = make_pipeline(ScriptedAgent([fix_add]))
    job = await pipeline.run(claim(store, "CALC-1"))
    assert not (settings.workspaces_dir / f"job-{job.id}").exists()

    kept = await pipeline.run(claim(store, "CALC-1"), RunOptions(keep_workspace=True))
    assert (settings.workspaces_dir / f"job-{kept.id}" / "calc.py").exists()


async def test_rerun_updates_the_existing_pr(make_pipeline: MakePipeline, store: JobStore) -> None:
    pipeline = make_pipeline(ScriptedAgent([fix_add]))
    first = await pipeline.run(claim(store, "CALC-1"))
    second = await pipeline.run(claim(store, "CALC-1"))
    assert first.pr_url == second.pr_url


async def test_without_test_command_pr_is_draft(
    make_pipeline: MakePipeline, store: JobStore, repos_dir: Path
) -> None:
    git(repos_dir / REPO, "rm", "-q", "-r", "tests", "pyproject.toml")
    git(repos_dir / REPO, "commit", "-q", "-m", "drop tests")
    job = await make_pipeline(ScriptedAgent([fix_add]), test_command="").run(claim(store, "CALC-1"))
    assert job.status is JobStatus.NEEDS_REVIEW
    assert job.tests_passed is None
    assert _pr(job)["draft"] is True


async def test_graphify_runs_when_available(
    make_pipeline: MakePipeline,
    store: JobStore,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repos_dir: Path,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "graphify"
    fake.write_text("#!/bin/sh\nmkdir -p graphify-out && echo '{}' > graphify-out/graph.json\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    seen: list[bool] = []

    def check_graph(task: AgentTask) -> None:
        seen.append((task.workspace / "graphify-out" / "graph.json").exists())
        fix_add(task)

    job = await make_pipeline(ScriptedAgent([check_graph]), use_graphify=True).run(
        claim(store, "CALC-1")
    )
    assert seen == [True]
    files = git(repos_dir / REPO, "show", "--name-only", "--format=", _branch(job)).split()
    assert files == ["calc.py"]  # the graph is never committed
