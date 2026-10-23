from __future__ import annotations

import asyncio
import json
import os
import subprocess
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    CLIConnectionError,
    HookCallback,
    HookContext,
    HookInput,
    ResultError,
    ResultMessage,
    TextBlock,
)
from pydantic import SecretStr

from ticket2pr.agent.base import AgentTask
from ticket2pr.agent.claude import ALLOWED_TOOLS, ClaudeAgent, usage_from_result
from ticket2pr.agent.guard import Policy
from ticket2pr.agent.prompts import RULES_REMINDER, build_prompt
from ticket2pr.config import Settings, secret_env_names
from ticket2pr.domain import AgentOutcome, Ticket
from ticket2pr.errors import ConfigurationError


def _result(subtype: str = "success", **overrides: Any) -> ResultMessage:
    fields: dict[str, Any] = {
        "subtype": subtype,
        "duration_ms": 90_000,
        "duration_api_ms": 80_000,
        "is_error": subtype != "success",
        "num_turns": 7,
        "session_id": "sess-1",
        "total_cost_usd": 1.25,
        "usage": {
            "input_tokens": 1200,
            "output_tokens": 340,
            "cache_read_input_tokens": 5000,
            "cache_creation_input_tokens": 800,
        },
        "result": "## Summary\nDone.",
    }
    return ResultMessage(**{**fields, **overrides})


def _agent(query_fn: Any, **overrides: Any) -> ClaudeAgent:
    params: dict[str, Any] = {
        "model": "claude-opus-5-5",
        "effort": "high",
        "max_budget_usd": 3.0,
        "max_turns": 50,
        "timeout_s": 5,
        "protected_globs": (".github/",),
        "network_allowlist": ("pypi.org",),
        "read_denied": (Path("/home/svc"), Path("/srv/t2p")),
        "read_allowed": (Path("/opt/python"),),
        "query_fn": query_fn,
    }
    return ClaudeAgent(**{**params, **overrides})


@pytest.fixture(autouse=True)
def _sandbox_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ticket2pr.sandbox.missing_sandbox_tools", list)


@pytest.fixture
def task(tmp_path: Path) -> AgentTask:
    return AgentTask(
        ticket=Ticket(key="PROJ-1", title="Fix it", description="Broken."),
        workspace=tmp_path,
        test_command="pytest -q",
        budget_usd=1.5,
    )


class Recorder:
    def __init__(self, *messages: Any) -> None:
        self.messages = messages
        self.calls: list[tuple[str, ClaudeAgentOptions]] = []

    def __call__(self, *, prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Any]:
        self.calls.append((prompt, options))
        return self._stream()

    async def _stream(self) -> AsyncIterator[Any]:
        for message in self.messages:
            yield message


async def test_success_maps_usage_and_summary(task: AgentTask) -> None:
    query = Recorder(
        AssistantMessage(content=[TextBlock(text="Working on it")], model="claude-opus-5-5"),
        _result(),
    )
    result = await _agent(query).run(task)

    assert result.outcome is AgentOutcome.COMPLETED
    assert result.ok
    assert result.summary == "## Summary\nDone."
    assert result.session_id == "sess-1"
    assert result.usage.cost_usd == 1.25
    assert result.usage.cache_read_tokens == 5000
    assert result.usage.turns == 7
    assert result.usage.duration_s == 90.0
    prompt, _ = query.calls[0]
    assert "PROJ-1" in prompt
    assert "pytest -q" in prompt


@pytest.mark.parametrize(
    ("subtype", "outcome"),
    [
        ("error_max_turns", AgentOutcome.MAX_TURNS),
        ("error_max_budget_usd", AgentOutcome.BUDGET_EXCEEDED),
        ("error_during_execution", AgentOutcome.ERROR),
    ],
)
async def test_error_subtypes(task: AgentTask, subtype: str, outcome: AgentOutcome) -> None:
    class FailingRun(Recorder):
        """Like the real SDK: yield the error result, then raise ResultError."""

        async def _stream(self) -> AsyncIterator[Any]:
            yield _result(subtype, result=None, total_cost_usd=2.5)
            raise ResultError("Claude Code returned an error result", exit_code=1)

    result = await _agent(FailingRun()).run(task)
    assert result.outcome is outcome
    assert not result.ok
    assert result.usage.cost_usd == 2.5  # the spend is kept, not lost with the exception
    assert result.usage.cost_capped is False


async def test_api_error_on_last_turn_is_not_a_success(task: AgentTask) -> None:
    class ApiFailure(Recorder):
        async def _stream(self) -> AsyncIterator[Any]:
            yield _result("success", is_error=True, result="API Error: 529 overloaded")
            raise ResultError("Claude Code returned an error result", exit_code=1)

    result = await _agent(ApiFailure()).run(task)
    assert result.outcome is AgentOutcome.ERROR
    assert result.usage.cost_usd == 1.25


async def test_api_key_from_settings_reaches_the_cli(task: AgentTask) -> None:
    query = Recorder(_result())
    await _agent(query, api_key="sk-ant-x").run(task)
    assert query.calls[0][1].env["ANTHROPIC_API_KEY"] == "sk-ant-x"
    plain = Recorder(_result())
    await _agent(plain).run(task)
    assert "ANTHROPIC_API_KEY" not in plain.calls[0][1].env  # SDK falls back to its own login


async def test_summary_falls_back_to_last_assistant_text(task: AgentTask) -> None:
    query = Recorder(
        AssistantMessage(content=[TextBlock(text="final words")], model="m"),
        _result(result=None),
    )
    assert (await _agent(query).run(task)).summary == "final words"


async def test_missing_result_message_is_an_error(task: AgentTask) -> None:
    result = await _agent(Recorder()).run(task)
    assert result.outcome is AgentOutcome.ERROR


async def test_sdk_errors_are_contained(task: AgentTask) -> None:
    def failing(**_: Any) -> AsyncIterator[Any]:
        async def stream() -> AsyncIterator[Any]:
            nothing: tuple[Any, ...] = ()
            for message in nothing:  # makes this an (empty) async generator
                yield message
            raise CLIConnectionError("cli died")

        return stream()

    result = await _agent(failing).run(task)
    assert result.outcome is AgentOutcome.ERROR
    assert "cli died" in result.summary
    # No result message: the exact cost is unknown, so the run counts as its whole budget.
    assert result.usage.cost_usd == 1.5
    assert result.usage.cost_capped is True


async def test_plain_exceptions_are_contained(task: AgentTask) -> None:
    def failing(**_: Any) -> AsyncIterator[Any]:
        async def stream() -> AsyncIterator[Any]:
            nothing: tuple[Any, ...] = ()
            for message in nothing:
                yield message
            raise Exception("Unknown error")  # noqa: TRY002 - what the SDK's message reader re-raises

        return stream()

    result = await _agent(failing).run(task)
    assert result.outcome is AgentOutcome.ERROR
    assert result.usage.cost_capped is True


async def test_timeout_after_the_result_keeps_the_result(task: AgentTask) -> None:
    def slow_exit(**_: Any) -> AsyncIterator[Any]:
        async def stream() -> AsyncIterator[Any]:
            yield _result()
            await asyncio.sleep(10)

        return stream()

    result = await _agent(slow_exit, timeout_s=0.05).run(task)
    assert result.outcome is AgentOutcome.COMPLETED
    assert result.usage.cost_capped is False


async def test_timeout(task: AgentTask) -> None:
    def slow(**_: Any) -> AsyncIterator[Any]:
        async def stream() -> AsyncIterator[Any]:
            await asyncio.sleep(10)
            yield _result()  # pragma: no cover

        return stream()

    result = await _agent(slow, timeout_s=0.05).run(task)
    assert result.outcome is AgentOutcome.TIMEOUT
    assert result.usage.cost_capped is True
    assert result.usage.cost_label == "≤ $1.50"


async def test_unknown_environment_variables_are_blanked(
    task: AgentTask, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgres://u:p@db/x")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/srv/claude")
    monkeypatch.setenv("DISABLE_TELEMETRY", "1")  # Claude Code's, unprefixed
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    query = Recorder(_result())
    await _agent(query).run(task)
    env = query.calls[0][1].env
    assert env["DATABASE_URL"] == ""
    assert env["AWS_REGION"] == ""  # Bedrock is not in use
    for kept in ("PATH", "CLAUDE_CONFIG_DIR", "DISABLE_TELEMETRY"):
        assert kept not in env

    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    await _agent(query).run(task)
    assert "AWS_REGION" not in query.calls[1][1].env


async def test_options_lock_down_the_agent(task: AgentTask) -> None:
    query = Recorder(_result())
    await _agent(query).run(task)
    _, options = query.calls[0]

    assert options.model == "claude-opus-5-5"
    assert options.effort == "high"
    assert options.cwd == str(task.workspace)
    assert options.permission_mode == "dontAsk"
    assert options.setting_sources == []
    assert options.allowed_tools == ALLOWED_TOOLS
    assert options.tools == ALLOWED_TOOLS  # the whole tool set, not just pre-approvals
    assert options.mcp_servers == {}
    assert options.strict_mcp_config is True
    assert "WebFetch" in options.disallowed_tools
    assert options.max_turns == 50
    assert options.max_budget_usd == 1.5  # the task's remaining budget wins when lower
    assert options.sandbox is not None
    assert options.sandbox["enabled"] is True
    assert options.sandbox["allowUnsandboxedCommands"] is False
    assert options.sandbox["network"] == {"allowedDomains": ["pypi.org"]}
    assert all(options.env[name] == "" for name in secret_env_names())
    assert options.env["CLAUDE_CODE_SUBPROCESS_ENV_SCRUB"] == "1"
    assert options.env["PYTHONDONTWRITEBYTECODE"] == "1"
    sandbox: dict[str, Any] = dict(options.sandbox)
    assert sandbox["failIfUnavailable"] is True
    agent_tmp = task.workspace.resolve().parent / "agent-tmp"  # per job, not /tmp/claude
    assert sandbox["filesystem"] == {
        "denyRead": [
            "/home/svc",
            "/srv/t2p",
            "/tmp/claude",
            f"/tmp/claude-{os.getuid()}",
            "/run/user",
            "/run/dbus",
        ],
        "allowRead": [str(task.workspace.resolve()), str(agent_tmp), "/opt/python"],
        "allowWrite": [str(agent_tmp)],
        "denyWrite": ["/tmp/claude", f"/tmp/claude-{os.getuid()}"],
    }
    assert agent_tmp.is_dir()
    assert options.env["CLAUDE_CODE_TMPDIR"] == str(agent_tmp)
    assert options.env["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] == "1"
    assert options.env["GIT_CONFIG_VALUE_0"] == "explicit"
    assert options.env["GIT_CEILING_DIRECTORIES"] == str(task.workspace.resolve().parent)
    assert isinstance(options.settings, str)
    assert json.loads(options.settings) == {
        "permissions": {"blockReadsOutsideWorkingDirectories": True}
    }


def test_every_secret_setting_is_hidden_from_the_agent() -> None:
    names = secret_env_names()
    secret_fields = [
        name for name, field in Settings.model_fields.items() if field.annotation is SecretStr
    ]
    assert secret_fields  # the check below must not pass vacuously
    for field in secret_fields:
        assert f"T2P_{field.upper()}" in names
    assert "T2P_ANTHROPIC_API_KEY" in names
    assert "ANTHROPIC_API_KEY" not in names  # the CLI needs it; Claude Code scrubs it itself


async def test_bash_sandbox_can_be_disabled(task: AgentTask) -> None:
    query = Recorder(_result())
    await _agent(query, sandbox=False).run(task)
    options = query.calls[0][1]
    assert options.sandbox is not None
    assert options.sandbox["enabled"] is False
    # The scrub would make Claude Code demand its sandbox and refuse every Bash command.
    assert "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB" not in options.env


async def test_agent_budget_caps_task_budget(task: AgentTask) -> None:
    query = Recorder(_result())
    big = AgentTask(ticket=task.ticket, workspace=task.workspace, budget_usd=99.0)
    await _agent(query).run(big)
    assert query.calls[0][1].max_budget_usd == 3.0
    spent = AgentTask(ticket=task.ticket, workspace=task.workspace, budget_usd=0.0)
    await _agent(query).run(spent)
    assert query.calls[1][1].max_budget_usd == 0.0  # not the full budget again


def _bash_call(command: str) -> HookInput:
    return cast(
        "HookInput",
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": command},
            "tool_use_id": "t1",
            "session_id": "s",
            "transcript_path": "",
            "cwd": "",
        },
    )


def _guard(options: ClaudeAgentOptions) -> HookCallback:
    assert options.hooks is not None
    return options.hooks["PreToolUse"][0].hooks[0]


CTX: HookContext = {"signal": None}


async def test_guard_hook_denies_and_records(task: AgentTask) -> None:
    query = Recorder(_result())
    await _agent(query).run(task)
    hook = _guard(query.calls[0][1])

    denied: dict[str, Any] = dict(await hook(_bash_call("git push"), "t1", CTX))
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert await hook(_bash_call("pytest"), "t2", CTX) == {}
    stop = cast("HookInput", {"hook_event_name": "Stop"})
    assert await hook(stop, None, CTX) == {}


async def test_guard_hook_denies_tools_outside_the_allowlist(task: AgentTask) -> None:
    query = Recorder(_result())
    await _agent(query).run(task)
    hook = _guard(query.calls[0][1])
    call = cast(
        "HookInput",
        {"hook_event_name": "PreToolUse", "tool_name": "Monitor", "tool_input": {"command": "x"}},
    )
    denied: dict[str, Any] = dict(await hook(call, "t1", CTX))
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"


async def test_guard_hook_denies_when_the_check_fails(task: AgentTask) -> None:
    query = Recorder(_result())
    await _agent(query).run(task)
    hook = _guard(query.calls[0][1])
    call = cast(
        "HookInput",
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Read",
            "tool_input": {"file_path": "x\0y"},  # resolve() raises ValueError
        },
    )
    denied: dict[str, Any] = dict(await hook(call, "t1", CTX))
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"


async def test_denied_actions_are_reported(task: AgentTask) -> None:
    class DenyingQuery(Recorder):
        async def _stream(self) -> AsyncIterator[Any]:
            await _guard(self.calls[-1][1])(_bash_call("env"), None, CTX)
            yield _result()

    result = await _agent(DenyingQuery()).run(task)
    assert result.denied_actions == ("Bash: dumping the environment is not allowed",)


def test_usage_from_result_tolerates_missing_fields() -> None:
    usage = usage_from_result(_result(usage=None, total_cost_usd=None))
    assert usage.cost_usd == 0.0
    assert usage.total_tokens == 0


async def test_fails_closed_when_sandbox_cannot_start(
    task: AgentTask, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("ticket2pr.sandbox.missing_sandbox_tools", lambda: ["socat"])
    query = Recorder(_result())
    with pytest.raises(ConfigurationError, match="missing: socat"):
        await _agent(query).run(task)
    assert query.calls == []  # the agent never started
    await _agent(query, sandbox=False).run(task)  # explicit opt-out still works
    assert len(query.calls) == 1


def test_prompt_fences_untrusted_text_and_ends_with_the_rules(task: AgentTask) -> None:
    feedback = "HEAD" + "x" * 6_000 + "TAIL"
    prompt = build_prompt(replace(task, repo_guidelines="Use tabs.", feedback=feedback))
    assert "<repository_guidelines>\nUse tabs.\n</repository_guidelines>" in prompt
    assert "<test_output>" in prompt
    assert "TAIL" in prompt
    assert "HEAD" not in prompt  # cut to its tail
    assert prompt.endswith(RULES_REMINDER)


def test_git_never_treats_the_workspace_root_as_a_repository(
    task: AgentTask, tmp_path: Path
) -> None:
    """HEAD, objects/, refs/ and a config at the root would make it one: Claude Code's own
    (unsandboxed) git must not discover it, nor run its fsmonitor."""
    ws = task.workspace
    marker = tmp_path / "pwned"
    (ws / "HEAD").write_text("ref: refs/heads/main\n")
    (ws / "objects").mkdir()
    (ws / "refs").mkdir()
    (ws / "config").write_text(
        f"[core]\n\trepositoryformatversion = 0\n\tbare = false\n\tworktree = .\n"
        f'\tfsmonitor = "touch {marker}; false"\n'
    )
    options = _agent(Recorder(_result())).build_options(task, Policy(workspace=ws), 1.0)
    env = {**os.environ, **{k: v for k, v in options.env.items() if k.startswith("GIT_")}}
    subprocess.run(["git", "status"], cwd=ws, env=env, capture_output=True, check=False)
    assert not marker.exists()
