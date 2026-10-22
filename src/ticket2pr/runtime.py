"""Composition root: builds the concrete objects a deployment runs with."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ticket2pr.agent.base import CodingAgent
from ticket2pr.agent.claude import ClaudeAgent
from ticket2pr.config import Settings
from ticket2pr.pipeline import Pipeline
from ticket2pr.sandbox import hidden_paths, toolchain_paths
from ticket2pr.scm.github import GitHubClient
from ticket2pr.scm.host import GitHubHost, LocalHost, RepoHost
from ticket2pr.store import JobStore
from ticket2pr.trackers.base import Tracker
from ticket2pr.trackers.jira import JiraTracker
from ticket2pr.trackers.local import LocalTracker

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@dataclass(slots=True)
class Runtime:
    settings: Settings
    store: JobStore
    pipeline: Pipeline
    tracker: Tracker
    host: RepoHost

    async def aclose(self) -> None:
        await self.tracker.aclose()
        await self.host.aclose()
        self.store.close()


def build_agent(settings: Settings) -> CodingAgent:
    return ClaudeAgent(
        model=settings.model,
        effort=settings.effort,
        max_budget_usd=settings.max_budget_usd,
        max_turns=settings.max_turns,
        timeout_s=settings.agent_timeout_s,
        protected_globs=tuple(settings.protected_paths),
        network_allowlist=tuple(settings.network_allowlist),
        sandbox=settings.sandbox,
        read_denied=hidden_paths(settings),
        read_allowed=toolchain_paths(settings),
        api_key=settings.anthropic_api_key.get_secret_value(),
    )


def build_runtime(
    settings: Settings,
    *,
    tickets_dir: Path | None = None,
    repos_dir: Path | None = None,
    agent: CodingAgent | None = None,
) -> Runtime:
    """Wire a runtime. ``tickets_dir`` / ``repos_dir`` switch to the offline implementations."""
    tracker: Tracker
    if tickets_dir is not None:
        tracker = LocalTracker(tickets_dir)
    else:
        tracker = JiraTracker(
            base_url=settings.jira_base_url,
            email=settings.jira_email,
            api_token=settings.jira_api_token.get_secret_value(),
        )
    host: RepoHost
    if repos_dir is not None:
        host = LocalHost(repos_dir)
    else:
        token = settings.github_token.get_secret_value()
        host = GitHubHost(GitHubClient(token=token, api_url=settings.github_api_url), token)
    store = JobStore(settings.db_path)
    pipeline = Pipeline(
        settings=settings,
        tracker=tracker,
        host=host,
        agent=agent or build_agent(settings),
        store=store,
    )
    return Runtime(settings=settings, store=store, pipeline=pipeline, tracker=tracker, host=host)


@asynccontextmanager
async def open_runtime(
    settings: Settings,
    *,
    tickets_dir: Path | None = None,
    repos_dir: Path | None = None,
    agent: CodingAgent | None = None,
) -> AsyncIterator[Runtime]:
    runtime = build_runtime(settings, tickets_dir=tickets_dir, repos_dir=repos_dir, agent=agent)
    try:
        yield runtime
    finally:
        await runtime.aclose()
