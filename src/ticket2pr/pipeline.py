"""The ticket → pull request pipeline.

    fetch ticket → clone → agent ⟲ sandboxed tests → publish clone → guardrails
    → commit → push → PR → comment

Every step that can fail maps to a ``Ticket2PRError``; the job then ends ``FAILED`` with the
reason recorded and reported back on the ticket. Spend is capped across retries: a second
agent attempt only gets the budget the first one left unused.

The agent's workspace is untrusted once the agent has run: ticket2pr never runs git in it
again. The result is copied (tree only, no ``.git``) into a fresh *publish clone*, and the
commit and push happen there.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

from ticket2pr import render
from ticket2pr.agent.base import AgentTask, CodingAgent
from ticket2pr.agent.guard import Policy
from ticket2pr.agent.prompts import read_guidelines
from ticket2pr.config import Settings
from ticket2pr.domain import AgentResult, ChangeSet, Job, JobStatus, PullRequest, TestReport, Ticket
from ticket2pr.errors import (
    AgentError,
    ConfigurationError,
    PolicyViolationError,
    SCMError,
    Ticket2PRError,
)
from ticket2pr.redaction import Redactor
from ticket2pr.sandbox import ensure_sandbox_available, sandbox_profile
from ticket2pr.scm.git import GitRepo, sync_tree
from ticket2pr.scm.host import RepoHost
from ticket2pr.store import JobStore
from ticket2pr.trackers.base import Tracker
from ticket2pr.verify import detect_test_command, run_tests

logger = logging.getLogger(__name__)

_MIN_RETRY_BUDGET_USD = 0.25


@dataclass(frozen=True, slots=True)
class RunOptions:
    require_label: bool = True
    max_attempts: int = 2
    keep_workspace: bool = False


@dataclass(frozen=True, slots=True)
class _Outcome:
    tests: TestReport
    pr: PullRequest


class Pipeline:
    def __init__(
        self,
        *,
        settings: Settings,
        tracker: Tracker,
        host: RepoHost,
        agent: CodingAgent,
        store: JobStore,
    ) -> None:
        self._settings = settings
        self._tracker = tracker
        self._host = host
        self._agent = agent
        self._store = store
        self._redactor = Redactor.from_settings(settings)

    async def run(self, job: Job, options: RunOptions | None = None) -> Job:
        """Process one claimed job to a terminal status. Never raises for expected failures."""
        options = options or RunOptions()
        job.status = JobStatus.RUNNING
        self._store.save(job)
        job_dir: Path | None = None
        outcome: _Outcome | None = None
        try:
            ticket = await self._tracker.get_ticket(job.ticket_key)
            if options.require_label and self._settings.trigger_label not in ticket.labels:
                job.status = JobStatus.SKIPPED
                job.error = f"ticket lacks the {self._settings.trigger_label!r} label"
                return self._store.save(job)
            job.repo = self._resolve_repo(job, ticket)
            if self._settings.sandbox:
                ensure_sandbox_available()  # before any clone or spend
            job_dir = self._settings.workspaces_dir / f"job-{job.id}"
            outcome = await self._process(job, ticket, job_dir, options)
        except asyncio.CancelledError:
            # Shutdown or Ctrl-C: leave a terminal status so the ticket is not blocked forever.
            job.status = JobStatus.FAILED
            job.error = "interrupted before completion"
            self._store.save(job)
            raise
        except Ticket2PRError as exc:
            job.status = JobStatus.FAILED
            job.error = self._redactor.redact(str(exc))
            logger.warning("job %s (%s) failed: %s", job.id, job.ticket_key, job.error)
        except Exception as exc:  # a bug must not kill the worker; record it and move on
            job.status = JobStatus.FAILED
            job.error = self._redactor.redact(f"internal error: {type(exc).__name__}: {exc}")
            logger.exception("job %s (%s) crashed", job.id, job.ticket_key)
        finally:
            if job_dir is not None and not options.keep_workspace:
                await asyncio.to_thread(shutil.rmtree, job_dir, ignore_errors=True)

        if job.status is not JobStatus.SKIPPED:
            await self._report(job, outcome)
        return self._store.save(job)

    def _resolve_repo(self, job: Job, ticket: Ticket) -> str:
        repo = job.repo or self._settings.repo_for(ticket.key)
        if not repo:
            raise ConfigurationError(f"no repository mapped for {ticket.key} (T2P_REPOS)")
        return repo

    async def _process(
        self, job: Job, ticket: Ticket, job_dir: Path, options: RunOptions
    ) -> _Outcome:
        assert job.repo is not None  # noqa: S101 - set by the caller
        settings = self._settings
        clone_url = self._host.clone_url(job.repo)
        auth = self._host.git_auth()
        await asyncio.to_thread(shutil.rmtree, job_dir, ignore_errors=True)  # stale attempt

        work = await GitRepo.clone(
            clone_url, job_dir / "work", branch=settings.base_branch, auth=auth
        )
        base_sha = await work.head()
        job.branch = settings.branch_prefix + ticket.branch_slug()
        self._store.save(job)

        test_command = settings.test_command or detect_test_command(work.path)
        agent, tests = await self._agent_loop(job, ticket, work.path, test_command, options)

        # From here on, the agent's workspace is data, not a repository we trust.
        publish = await GitRepo.clone(
            clone_url, job_dir / "publish", branch=settings.base_branch, auth=auth
        )
        try:
            await publish.checkout_new_branch(job.branch, base_sha)
        except SCMError:  # the base branch moved past the shallow history since the first clone
            raise SCMError(
                f"{settings.base_branch} moved too far during the run; re-run the ticket"
            ) from None
        await asyncio.to_thread(sync_tree, work.path, publish.path)
        # Untracked build artefacts the agent's own runs may leave (repos without a .gitignore).
        publish.exclude("__pycache__/", "*.py[co]")
        changes, excluded = await self._stage(publish)

        await publish.commit(
            render.commit_message(ticket),
            author_name=settings.commit_author_name,
            author_email=settings.commit_author_email,
        )
        await publish.push(clone_url, job.branch, auth=auth)
        pr = await self._host.open_or_update_pr(
            job.repo,
            head=job.branch,
            base=settings.base_branch,
            title=render.pr_title(ticket),
            body=self._redactor.redact(
                render.pr_body(
                    ticket=ticket,
                    agent=agent,
                    usage=job.usage,
                    changes=changes,
                    tests=tests,
                    model=settings.model,
                    excluded=excluded,
                )
            ),
            draft=settings.draft_on_test_failure and not tests.passed,
        )
        job.pr_url = pr.url
        job.tests_passed = tests.passed if tests.ran else None
        job.status = JobStatus.SUCCEEDED if tests.passed else JobStatus.NEEDS_REVIEW
        return _Outcome(tests=tests, pr=pr)

    async def _agent_loop(
        self,
        job: Job,
        ticket: Ticket,
        workspace: Path,
        test_command: str | None,
        options: RunOptions,
    ) -> tuple[AgentResult, TestReport]:
        settings = self._settings
        guidelines = read_guidelines(workspace)
        feedback = ""
        completed: AgentResult | None = None  # last attempt that finished normally
        denied: list[str] = []  # guardrail events across every attempt
        tests = TestReport(command=test_command, passed=False)
        for attempt in range(1, options.max_attempts + 1):
            remaining = settings.max_budget_usd - job.usage.cost_usd
            if attempt > 1 and remaining < _MIN_RETRY_BUDGET_USD:
                logger.info("job %s: budget exhausted, no retry", job.id)
                break
            task = AgentTask(
                ticket=ticket,
                workspace=workspace,
                test_command=test_command,
                repo_guidelines=guidelines,
                feedback=feedback,
                budget_usd=remaining,
            )
            result = await self._agent.run(task)
            job.usage += result.usage
            denied += result.denied_actions
            self._store.save(job)
            logger.info(
                "job %s attempt %d: %s (%s)",
                job.id,
                attempt,
                result.outcome,
                result.usage.cost_label,
            )
            if not result.ok:
                if completed is None:
                    raise AgentError(
                        f"agent stopped: {result.outcome.value}. {result.summary[:300]}"
                    )
                # Keep the earlier, completed attempt (plus whatever the retry left behind)
                # and let the tests decide; a failed retry must not throw a draft PR away.
                completed = completed.model_copy(
                    update={
                        "summary": f"{completed.summary}\n\n_A retry to fix the failing tests "
                        f"stopped early ({result.outcome.value})._"
                    }
                )
                tests = await self._run_tests(test_command, workspace)
                break
            completed = result
            tests = await self._run_tests(test_command, workspace)
            if tests.passed or not tests.ran:
                break
            feedback = self._redactor.redact(tests.output_tail)

        if completed is None:  # pragma: no cover - max_attempts >= 1 guarantees one run
            raise AgentError("the agent never ran")
        return completed.model_copy(update={"denied_actions": tuple(denied)}), tests

    async def _run_tests(self, command: str | None, workspace: Path) -> TestReport:
        return await run_tests(
            command,
            workspace,
            timeout_s=self._settings.test_timeout_s,
            sandbox=sandbox_profile(self._settings),
        )

    async def _stage(self, repo: GitRepo) -> tuple[ChangeSet, tuple[str, ...]]:
        policy = Policy(workspace=repo.path, protected_globs=tuple(self._settings.protected_paths))
        changes = await repo.stage_all()
        excluded = tuple(path for path in changes.files if policy.is_protected(path))
        if excluded:
            await repo.unstage(list(excluded))
            changes = await repo.staged_changes()
        if changes.empty:
            raise AgentError("the agent finished without changing any file")
        if len(changes.files) > self._settings.max_changed_files:
            raise PolicyViolationError(
                f"the change touches {len(changes.files)} files "
                f"(limit {self._settings.max_changed_files})"
            )
        if self._redactor.contains_secret(await repo.staged_diff()):
            raise PolicyViolationError("the change contains a configured secret; not pushing it")
        return changes, excluded

    async def _report(self, job: Job, outcome: _Outcome | None) -> None:
        comment = render.tracker_comment(
            pr=outcome.pr if outcome else None,
            tests=outcome.tests if outcome else None,
            usage=job.usage,
            error=job.error,
        )
        try:
            await self._tracker.comment(job.ticket_key, self._redactor.redact(comment))
        except Ticket2PRError as exc:
            logger.warning("could not comment on %s: %s", job.ticket_key, exc)
