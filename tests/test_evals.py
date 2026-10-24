from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from conftest import ScriptedAgent, do_nothing
from ticket2pr import cli, evals
from ticket2pr.agent.base import AgentTask
from ticket2pr.config import Settings
from ticket2pr.errors import ConfigurationError
from ticket2pr.evals import EvalResult, EvalSummary, EvalTask, discover, run_suite

TASKS = Path(__file__).resolve().parent.parent / "evals" / "tasks"
TASK_NAMES = ("envflags", "paginate", "retry", "slugify")


async def test_oracle_resolves_every_task(settings: Settings) -> None:
    """Every shipped task is solvable, and its hidden tests accept the reference fix."""
    tasks = discover(TASKS)
    assert tuple(task.name for task in tasks) == TASK_NAMES
    summary = await run_suite(tasks, settings=settings)
    unresolved = [result for result in summary.results if not result.resolved]
    assert unresolved == []
    assert summary.resolve_rate == 1.0


def touch_readme(task: AgentTask) -> None:
    """An irrelevant change: enough for a PR to open, so the hidden tests really run."""
    (task.workspace / "README.md").write_text("Nothing to see.\n")


async def test_hidden_tests_reject_the_unfixed_repo(settings: Settings) -> None:
    """Hidden tests must fail on the unfixed code, or the eval measures nothing."""
    summary = await run_suite(
        discover(TASKS), settings=settings, agent=ScriptedAgent([touch_readme])
    )
    # A PR was opened for every task, so the hidden tests did run against the unfixed code.
    assert "failed" not in {result.status for result in summary.results}
    assert [result.resolved for result in summary.results] == [False] * len(TASK_NAMES)
    assert summary.cost_per_resolved_usd is None


def test_grader_refuses_symlinks_in_tests(tmp_path: Path) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    assert not evals._has_symlink(tests)
    (tests / "test_x.py").symlink_to(tmp_path / "elsewhere")
    assert evals._has_symlink(tests)
    linked = tmp_path / "linked"
    linked.symlink_to(tmp_path)
    assert evals._has_symlink(linked)


def test_discover_rejects_unknown_and_invalid_tasks(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="unknown eval task"):
        discover(TASKS, ["nope"])
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "ticket.yaml").write_text("key: lowercase-1\n")
    with pytest.raises(ConfigurationError, match="valid `key`"):
        EvalTask.load(broken)
    (broken / "ticket.yaml").write_text("key: OK-1\n")
    with pytest.raises(ConfigurationError, match="missing repo/"):
        EvalTask.load(broken)
    (broken / "repo").mkdir()
    (broken / "hidden_tests").mkdir()
    with pytest.raises(ConfigurationError, match="missing solution/"):
        EvalTask.load(broken)


def test_summary_metrics() -> None:
    def result(resolved: bool, cost: float) -> EvalResult:
        return EvalResult("t", "T-1", "succeeded", resolved, cost, 10, 1, 1.0)

    summary = EvalSummary((result(True, 1.0), result(False, 2.0), result(True, 3.0)))
    assert summary.resolve_rate == pytest.approx(2 / 3)
    assert summary.cost_per_resolved_usd == pytest.approx(3.0)
    assert json.loads(summary.to_json())["total_cost_usd"] == pytest.approx(6.0)
    assert EvalSummary(()).resolve_rate == 0.0


def test_cli_eval_oracle_writes_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tasks = tmp_path / "tasks"
    shutil.copytree(TASKS / "slugify", tasks / "slugify")
    monkeypatch.setenv("T2P_DATA_DIR", str(tmp_path / "data"))
    report = tmp_path / "report.json"
    result = CliRunner().invoke(
        cli.app, ["eval", "--tasks-dir", str(tasks), "--output", str(report)]
    )
    assert result.exit_code == 0, result.output
    assert "Resolved 100% of 1 task(s)" in result.output
    assert json.loads(report.read_text())["resolve_rate"] == 1.0


def test_cli_eval_rejects_bad_input(tmp_path: Path) -> None:
    runner = CliRunner()
    missing = runner.invoke(cli.app, ["eval", "--tasks-dir", str(tmp_path / "nope")])
    assert missing.exit_code == 2  # typer: "Directory ... does not exist"
    bad_agent = runner.invoke(cli.app, ["eval", "--tasks-dir", str(TASKS), "--agent", "gpt"])
    assert bad_agent.exit_code == 2
    # On CI, typer's rich error panel is coloured and wrapped: compare the bare text.
    plain = re.sub(r"\x1b\[[0-9;]*m|[│╭╮╰╯─\s]", "", bad_agent.output)
    assert "--agentmustbe" in plain
    result = runner.invoke(cli.app, ["eval", "--tasks-dir", str(TASKS), "--task", "nope"])
    assert result.exit_code == 2


def test_cli_eval_uses_claude_agent_when_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ticket2pr.runtime.build_agent", lambda _: ScriptedAgent([do_nothing]))
    result = CliRunner().invoke(
        cli.app, ["eval", "--tasks-dir", str(TASKS), "--task", "paginate", "--agent", "claude"]
    )
    assert result.exit_code == 1
    assert "Resolved 0%" in result.output


async def test_eval_ignores_fork_mode(settings: Settings) -> None:
    forked = settings.model_copy(update={"fork_owner": "ticket2pr-bot"})
    summary = await run_suite(discover(TASKS, ["slugify"]), settings=forked)
    assert summary.resolve_rate == 1.0


def test_cli_eval_creates_the_report_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("T2P_DATA_DIR", str(tmp_path / "data"))
    report = tmp_path / "results" / "nested" / "report.json"
    result = CliRunner().invoke(
        cli.app,
        ["eval", "--tasks-dir", str(TASKS), "--task", "slugify", "--output", str(report)],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(report.read_text())["resolve_rate"] == 1.0


async def test_developer_git_config_cannot_break_the_suite(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Commit signing that cannot work non-interactively must not abort `make eval`."""
    hostile = tmp_path / "hostile-gitconfig"
    hostile.write_text("[commit]\n\tgpgsign = true\n[gpg]\n\tprogram = false\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(hostile))
    summary = await run_suite(discover(TASKS, ["slugify"]), settings=settings)
    assert summary.resolve_rate == 1.0


def test_capped_costs_are_marked_as_upper_bounds() -> None:
    measured = EvalResult("a", "A-1", "succeeded", True, 0.5, 10, 1, 1.0)
    capped = EvalResult("b", "B-1", "failed", False, 2.0, 0, 0, 1.0, cost_capped=True)
    assert not EvalSummary((measured,)).any_cost_capped
    report = json.loads(EvalSummary((measured, capped)).to_json())
    assert report["any_cost_capped"] is True
    assert report["results"][1]["cost_capped"] is True
