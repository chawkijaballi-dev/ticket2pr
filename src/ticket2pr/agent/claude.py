"""``CodingAgent`` backed by the Claude Agent SDK (the Claude Code harness as a library)."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, cast

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKError,
    HookContext,
    HookInput,
    HookJSONOutput,
    HookMatcher,
    ResultMessage,
    SandboxSettings,
    TextBlock,
    query,
)

from ticket2pr.agent.base import AgentTask
from ticket2pr.agent.guard import Policy
from ticket2pr.agent.prompts import SYSTEM_PROMPT, build_prompt
from ticket2pr.config import Effort, secret_env_names
from ticket2pr.domain import AgentOutcome, AgentResult, Usage
from ticket2pr.sandbox import ensure_sandbox_available

logger = logging.getLogger(__name__)
_SHARED_TMP = "/tmp/claude"  # noqa: S108 - Claude Code's default, shared by every session

# Built-in tools the agent may use. Web access and sub-agents are left out: the ticket and the
# repository are the whole context, and fewer tools means fewer ways to exfiltrate data.
ALLOWED_TOOLS = ["Read", "Write", "Edit", "Glob", "Grep", "Bash"]
DISALLOWED_TOOLS = ["WebFetch", "WebSearch", "Task", "NotebookEdit"]

_OUTCOMES = {
    "success": AgentOutcome.COMPLETED,
    "error_max_turns": AgentOutcome.MAX_TURNS,
    "error_max_budget_usd": AgentOutcome.BUDGET_EXCEEDED,
}

# Seam for tests: same signature as ``claude_agent_sdk.query``.
QueryFn = Callable[..., AsyncIterator[Any]]


class ClaudeAgent:
    def __init__(
        self,
        *,
        model: str,
        effort: Effort,
        max_budget_usd: float,
        max_turns: int,
        timeout_s: float,
        protected_globs: tuple[str, ...] = (),
        network_allowlist: tuple[str, ...] = (),
        sandbox: bool = True,
        read_denied: tuple[Path, ...] = (),
        read_allowed: tuple[Path, ...] = (),
        api_key: str = "",
        query_fn: QueryFn = query,
    ) -> None:
        self._model = model
        self._effort = effort
        self._max_budget_usd = max_budget_usd
        self._max_turns = max_turns
        self._timeout_s = timeout_s
        self._protected_globs = protected_globs
        self._network_allowlist = network_allowlist
        self._sandbox = sandbox
        self._read_denied = read_denied
        self._read_allowed = read_allowed
        self._api_key = api_key
        self._query = query_fn

    async def run(self, task: AgentTask) -> AgentResult:
        if self._sandbox:
            ensure_sandbox_available()
        budget = min(self._max_budget_usd, task.budget_usd or self._max_budget_usd)
        policy = Policy(workspace=task.workspace, protected_globs=self._protected_globs)
        options = self.build_options(task, policy, budget)
        started = time.monotonic()
        result: ResultMessage | None = None
        last_text = ""

        def interrupted(outcome: AgentOutcome, summary: str) -> AgentResult:
            # No result message means no cost report: count the whole budget, so the job's
            # spend and the retry budget err on the safe side.
            usage = Usage(cost_usd=budget, cost_capped=True, duration_s=time.monotonic() - started)
            return AgentResult(
                outcome=outcome, summary=summary, usage=usage, denied_actions=tuple(policy.denied)
            )

        try:
            async with asyncio.timeout(self._timeout_s):
                async for message in self._query(prompt=build_prompt(task), options=options):
                    if isinstance(message, AssistantMessage):
                        text = "".join(b.text for b in message.content if isinstance(b, TextBlock))
                        last_text = text or last_text
                    elif isinstance(message, ResultMessage):
                        result = message
        except TimeoutError:
            logger.warning("agent timed out after %ss on %s", self._timeout_s, task.ticket.key)
            return interrupted(AgentOutcome.TIMEOUT, last_text)
        except ClaudeSDKError as exc:
            # A failed run (budget, max turns, API error) yields its ResultMessage, then the CLI
            # exits non-zero and the SDK raises ResultError: the result already has the facts.
            if result is None:
                logger.exception("agent failed on %s", task.ticket.key)
                return interrupted(AgentOutcome.ERROR, f"Agent error: {exc}")
            logger.info("agent run on %s ended with %s", task.ticket.key, result.subtype)

        if result is None:
            return interrupted(AgentOutcome.ERROR, "The agent ended without a result message.")
        # An API failure on the last turn (overload, rate limit, prompt too long) ends with
        # subtype "success" and is_error=True: that is not a completed run.
        outcome = (
            AgentOutcome.ERROR
            if result.is_error and result.subtype == "success"
            else _OUTCOMES.get(result.subtype, AgentOutcome.ERROR)
        )
        return AgentResult(
            outcome=outcome,
            summary=(result.result or last_text).strip(),
            usage=usage_from_result(result),
            session_id=result.session_id,
            denied_actions=tuple(policy.denied),
        )

    def build_options(self, task: AgentTask, policy: Policy, budget: float) -> ClaudeAgentOptions:
        workspace = task.workspace.resolve()
        # Claude Code's sandbox otherwise gives every job the same writable /tmp/claude: a
        # private one per job (next to the workspace, deleted with it) keeps jobs apart.
        agent_tmp = workspace.parent / "agent-tmp"
        agent_tmp.mkdir(exist_ok=True)
        sandbox = cast(
            "SandboxSettings",
            {
                "enabled": self._sandbox,
                # Refuse to start rather than print a warning and run Bash unsandboxed.
                "failIfUnavailable": self._sandbox,
                "autoAllowBashIfSandboxed": True,
                "allowUnsandboxedCommands": False,
                "network": {"allowedDomains": list(self._network_allowlist)},
                # Bash reads are deny-listed: hide the service's home and data (other jobs,
                # the job database, credentials), then re-open the workspace and toolchains.
                "filesystem": {
                    "denyRead": [*(str(path) for path in self._read_denied), _SHARED_TMP],
                    "allowRead": [
                        str(workspace),
                        str(agent_tmp),
                        *(str(p) for p in self._read_allowed),
                    ],
                    "allowWrite": [str(agent_tmp)],
                    "denyWrite": [_SHARED_TMP],
                },
            },
        )
        # Not covered by SandboxSettings: the permission layer also refuses reads outside the
        # working directory, for the file tools and for Bash commands it can analyse.
        settings = json.dumps({"permissions": {"blockReadsOutsideWorkingDirectories": True}})
        return ClaudeAgentOptions(
            model=self._model,
            effort=self._effort,
            cwd=str(task.workspace),
            system_prompt={"type": "preset", "preset": "claude_code", "append": SYSTEM_PROMPT},
            # The base tool set itself, not just pre-approvals: Claude Code otherwise also
            # exposes Monitor (background tasks), cron, remote triggers, ToolSearch...
            tools=list(ALLOWED_TOOLS),
            allowed_tools=list(ALLOWED_TOOLS),
            # No MCP server at all, not even the operator's claude.ai connectors.
            mcp_servers={},
            strict_mcp_config=True,
            disallowed_tools=list(DISALLOWED_TOOLS),
            # Non-interactive: anything not pre-approved above is denied, never prompted.
            # (With CLAUDE_CODE_SUBPROCESS_ENV_SCRUB, Claude Code forces "default" mode; the
            # allowlist still applies and, with no one to ask, prompts resolve to a denial.)
            permission_mode="dontAsk",
            # Never load settings from disk: a repository's .claude/settings.json is untrusted
            # input and could register hooks that run arbitrary commands on the worker.
            setting_sources=[],
            max_turns=self._max_turns,
            max_budget_usd=budget,
            sandbox=sandbox,
            settings=settings,
            hooks={"PreToolUse": [HookMatcher(hooks=[_guard_hook(policy)])]},
            # Blank ticket2pr's own secrets, and have Claude Code strip its credentials
            # (ANTHROPIC_API_KEY, ...) from the environment of every command the agent runs.
            # PYTHONDONTWRITEBYTECODE keeps the agent's own test runs from leaving caches behind.
            env={
                **dict.fromkeys(secret_env_names(), ""),
                **({"ANTHROPIC_API_KEY": self._api_key} if self._api_key else {}),
                "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB": "1",
                "CLAUDE_CODE_TMPDIR": str(agent_tmp),
                # No background shells, not even a timed-out command moved to the background:
                # a process alive while a file tool acts could race the guard (guard.py).
                "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONNOUSERSITE": "1",  # no usercustomize.py planted in $HOME by a past job
            },
        )


def _guard_hook(
    policy: Policy,
) -> Callable[[HookInput, str | None, HookContext], Any]:
    async def hook(
        hook_input: HookInput, _tool_use_id: str | None, _context: HookContext
    ) -> HookJSONOutput:
        if hook_input["hook_event_name"] != "PreToolUse":
            return {}
        tool_name = hook_input["tool_name"]
        try:
            reason = (
                policy.check(tool_name, hook_input["tool_input"])
                if tool_name in ALLOWED_TOOLS
                else f"{tool_name} is not an allowed tool"  # deny by default
            )
        except Exception:  # the CLI treats a failing hook as no decision: deny instead
            logger.warning("guard error on %s", tool_name, exc_info=True)
            reason = "the guard could not check this call"
        if reason is None:
            return {}
        policy.denied.append(f"{tool_name}: {reason}")
        logger.info("blocked %s: %s", tool_name, reason)
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": f"Blocked by ticket2pr policy: {reason}",
            }
        }

    return hook


def usage_from_result(result: ResultMessage) -> Usage:
    usage: dict[str, Any] = result.usage or {}
    return Usage(
        input_tokens=int(usage.get("input_tokens", 0)),
        output_tokens=int(usage.get("output_tokens", 0)),
        cache_read_tokens=int(usage.get("cache_read_input_tokens", 0)),
        cache_creation_tokens=int(usage.get("cache_creation_input_tokens", 0)),
        cost_usd=float(result.total_cost_usd or 0.0),
        turns=result.num_turns,
        duration_s=result.duration_ms / 1000,
    )
