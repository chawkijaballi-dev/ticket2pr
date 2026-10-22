"""The ticket → pull request pipeline.

    fetch ticket → clone → (graphify) → agent ⟲ tests → guardrails → commit → push → PR → comment

Every step that can fail maps to a ``Ticket2PRError``; the job then ends ``FAILED`` with the
reason recorded and reported back on the ticket. Spend is capped across retries: a second
agent attempt only gets the budget the first one left unused.
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
from ticket2pr.domain import (
    AgentResult,
    ChangeSet,
    Job,
    JobStatus,
    TestReport,
    Ticket,
)
from ticket2pr.errors import AgentError, ConfigurationError, PolicyViolationError, Ticket2PRError
from ticket2pr.scm.git import GitRepo
from ticket2pr.scm.host import RepoHost
from ticket2pr.store import JobStore
from ticket2pr.trackers.base import Tracker
from ticket2pr.verify import detect_test_command, run_tests

logger = logging.getLogger(__name__)

GRAPHIFY_OUT = "graphify-out"
_MIN_RETRY_BUDGET_USD = 0.25


@dataclass(frozen=True, slots=True)
class RunOptions:
    repo: str | None = None  # overrides the project → repo mapping
    require_label: bool = True
    max_attempts: int = 2
    keep_workspace: bool = False


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

    async def run(self, job: Job, options: RunOptions | None = None) -> Job:
        """Process one claimed job to a terminal status. Never raises for expected failures."""
        options = options or RunOptions()
        job.status = JobStatus.RUNNING
        self._store.save(job)
        workspace: Path | None = None
        tests: TestReport | None = None
        try:
            ticket = await self._tracker.get_ticket(job.ticket_key)
            if options.require_label and self._settings.trigger_label not in ticket.labels:
                job.status = JobStatus.SKIPPED
                job.error = f"ticket lacks the {self._settings.trigger_label!r} label"
                return self._store.save(job)
            job.repo = self._resolve_repo(job, ticket, options)
            workspace = self._settings.workspaces_dir / f"job-{job.id}"
            tests = await self._process(job, ticket, workspace, options)
        except Ticket2PRError as exc:
            job.status = JobStatus.FAILED
            job.error = str(exc)
            logger.warning("job %s (%s) failed: %s", job.id, job.ticket_key, exc)
        except Exception as exc:  # a bug must not kill the worker; record it and move on
            job.status = JobStatus.FAILED
            job.error = f"internal error: {type(exc).__name__}: {exc}"
            logger.exception("job %s (%s) crashed", job.id, job.ticket_key)
        finally:
            if workspace is not None and not options.keep_workspace:
                await asyncio.to_thread(shutil.rmtree, workspace, ignore_errors=True)

        if job.status is not JobStatus.SKIPPED:
            await self._report(job, tests)
        return self._store.save(job)

    def _resolve_repo(self, job: Job, ticket: Ticket, options: RunOptions) -> str:
        repo = options.repo or job.repo or self._settings.repo_for(ticket.key)
        if not repo:
            raise ConfigurationError(f"no repository mapped for {ticket.key} (T2P_REPOS)")
        return repo

    async def _process(
        self, job: Job, ticket: Ticket, workspace: Path, options: RunOptions
    ) -> TestReport:
        assert job.repo is not None  # noqa: S101 - checked by the caller
        settings = self._settings
        await asyncio.to_thread(shutil.rmtree, workspace, ignore_errors=True)  # stale attempt
        repo = await GitRepo.clone(
            self._host.clone_url(job.repo),
            workspace,
            branch=settings.base_branch,
            auth=self._host.git_auth(),
        )
        job.branch = settings.branch_prefix + ticket.branch_slug()
        await repo.checkout_new_branch(job.branch)
        self._store.save(job)
        await self._maybe_build_graph(repo)

        test_command = settings.test_command or detect_test_command(workspace)
        agent, tests = await self._agent_loop(job, ticket, workspace, test_command, options)

        # Everything below treats the workspace as untrusted: the agent had write access to it.
        repo.restore_config()
        changes, excluded = await self._stage(repo)
        draft = not tests.passed and settings.draft_on_test_failure
        await repo.commit(
            render.commit_message(ticket),
            author_name=settings.commit_author_name,
            author_email=settings.commit_author_email,
        )
        await repo.push(self._host.clone_url(job.repo), job.branch, auth=self._host.git_auth())
        pr = await self._host.open_or_update_pr(
            job.repo,
            head=job.branch,
            base=settings.base_branch,
            title=render.pr_title(ticket),
            body=render.pr_body(
                ticket=ticket,
                agent=agent,
                changes=changes,
                tests=tests,
                model=settings.model,
                excluded=excluded,
            ),
            draft=draft,
        )
        job.pr_url = pr.url
        job.tests_passed = tests.passed if tests.ran else None
        job.status = JobStatus.SUCCEEDED if tests.passed else JobStatus.NEEDS_REVIEW
        return tests

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
        result: AgentResult | None = None
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
            self._store.save(job)
            logger.info(
                "job %s attempt %d: %s ($%.2f)",
                job.id,
                attempt,
                result.outcome,
                result.usage.cost_usd,
            )
            if not result.ok:
                break
            tests = await run_tests(test_command, workspace, timeout_s=settings.test_timeout_s)
            if tests.passed or not tests.ran:
                break
            feedback = tests.output_tail

        if result is None:  # pragma: no cover - max_attempts >= 1 guarantees one run
            raise AgentError("the agent never ran")
        if not result.ok:
            raise AgentError(f"agent stopped: {result.outcome.value}. {result.summary[:300]}")
        return result, tests

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
        return changes, excluded

    async def _maybe_build_graph(self, repo: GitRepo) -> None:
        """Give the agent a graphify code graph when the CLI is available (best effort)."""
        if not self._settings.use_graphify or shutil.which("graphify") is None:
            return
        repo.exclude(f"/{GRAPHIFY_OUT}/")
        workspace = repo.path
        try:
            process = await asyncio.create_subprocess_exec(
                "graphify",
                "update",
                ".",
                cwd=workspace,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            async with asyncio.timeout(300):
                await process.wait()
        except (OSError, TimeoutError):
            logger.warning("graphify failed; continuing without a code graph")

    async def _report(self, job: Job, tests: TestReport | None) -> None:
        comment = render.tracker_comment(
            pr_url=job.pr_url, tests=tests, usage=job.usage, error=job.error
        )
        try:
            await self._tracker.comment(job.ticket_key, comment)
        except Ticket2PRError as exc:
            logger.warning("could not comment on %s: %s", job.ticket_key, exc)
