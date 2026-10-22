"""Domain models, configuration, job store, verification and rendering."""

from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path

import pytest
from pydantic import ValidationError

from ticket2pr import render
from ticket2pr.config import Settings
from ticket2pr.domain import (
    AgentOutcome,
    AgentResult,
    ChangeSet,
    Job,
    JobStatus,
    TestReport,
    Ticket,
    Usage,
    is_ticket_key,
)
from ticket2pr.store import JobStore
from ticket2pr.verify import detect_test_command, run_tests

# ── domain ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("key", "valid"),
    [("PROJ-1", True), ("A1_B-42", True), ("proj-1", False), ("PROJ-0", False), ("PROJ", False)],
)
def test_ticket_keys(key: str, valid: bool) -> None:
    assert is_ticket_key(key) is valid


def test_branch_slug() -> None:
    ticket = Ticket(key="PROJ-42", title="Fix   the Login bug!! (urgent) and much more text here")
    assert ticket.branch_slug() == "proj-42-fix-the-login-bug-urgent-and"
    assert ticket.branch_slug(max_len=12) == "proj-42-fix"
    assert Ticket(key="PROJ-42", title="Short").branch_slug() == "proj-42-short"
    long_word = Ticket(key="PROJ-42", title="x" * 60)
    assert long_word.branch_slug() == ("proj-42-" + "x" * 60)[:40]


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


def test_status_terminality() -> None:
    assert not JobStatus.RUNNING.terminal
    assert JobStatus.NEEDS_REVIEW.terminal


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
    assert settings.db_path == Path(".ticket2pr/jobs.sqlite3")
    assert settings.workspaces_dir == Path(".ticket2pr/workspaces")


@pytest.mark.parametrize("repos", [{"proj": "acme/api"}, {"PROJ": "not-a-repo"}, {"PROJ": "a/b/c"}])
def test_settings_reject_bad_repo_mapping(repos: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        Settings(repos=repos, _env_file=None)


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


def test_store_claim_under_concurrency() -> None:
    store = JobStore(":memory:")
    results: list[Job | None] = []

    def claim() -> None:
        results.append(store.claim("PROJ-1", None))

    threads = [threading.Thread(target=claim) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(job is not None for job in results) == 1


def test_store_round_trip_and_listing() -> None:
    store = JobStore(":memory:")
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


def test_store_save_requires_id() -> None:
    with pytest.raises(ValueError, match="persisted"):
        JobStore(":memory:").save(Job(ticket_key="PROJ-1"))


def test_store_fail_interrupted() -> None:
    store = JobStore(":memory:")
    running = store.claim("PROJ-1", None)
    assert running is not None
    running.status = JobStatus.RUNNING
    store.save(running)
    store.claim("PROJ-2", None)
    assert store.fail_interrupted() == 2
    assert all(job.status is JobStatus.FAILED for job in store.list())


# ── verify ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        ({"package.json": '{"scripts": {"test": "jest"}}'}, "npm test --silent"),
        ({"package.json": '{"scripts": {}}'}, None),
        ({"package.json": "not json"}, None),
        ({"pyproject.toml": "", "tests/test_a.py": ""}, "python -m pytest -q"),
        ({"setup.cfg": "", "test_x.py": ""}, "python -m pytest -q"),
        ({"setup.py": "", "x_test.py": ""}, "python -m pytest -q"),
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
    passed = await run_tests("echo ok", tmp_path, timeout_s=10)
    assert passed.passed
    assert passed.exit_code == 0
    assert "ok" in passed.output_tail

    failed = await run_tests("echo boom >&2; exit 3", tmp_path, timeout_s=10)
    assert not failed.passed
    assert failed.exit_code == 3
    assert "boom" in failed.output_tail

    missing = await run_tests(None, tmp_path, timeout_s=10)
    assert not missing.ran
    assert not missing.passed


async def test_run_tests_hides_secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("T2P_GITHUB_TOKEN", "leak-me")
    report = await run_tests("env", tmp_path, timeout_s=10)
    assert "leak-me" not in report.output_tail
    assert "CI=true" in report.output_tail


async def test_run_tests_timeout_kills_process_group(tmp_path: Path) -> None:
    report = await run_tests("sleep 30 & sleep 30", tmp_path, timeout_s=0.3)
    assert not report.passed
    assert report.exit_code is None
    assert "Timed out" in report.output_tail


# ── render ──────────────────────────────────────────────────────────────

TICKET = Ticket(key="PROJ-1", title="Fix", url="https://acme.atlassian.net/browse/PROJ-1")
USAGE = Usage(input_tokens=1000, output_tokens=200, cache_read_tokens=50, cost_usd=1.234, turns=3)


def test_commit_message_and_title() -> None:
    assert render.commit_message(TICKET).splitlines()[0] == "PROJ-1: Fix"
    assert "Ticket: https://acme" in render.commit_message(TICKET)
    assert "Ticket:" not in render.commit_message(Ticket(key="PROJ-1", title="x"))
    assert render.pr_title(TICKET) == "[PROJ-1] Fix"


def test_pr_body_contents() -> None:
    body = render.pr_body(
        ticket=TICKET,
        agent=AgentResult(outcome=AgentOutcome.COMPLETED, summary="Did it", usage=USAGE),
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
    assert "Not committed (protected path): `.env`" in body


def test_pr_body_without_tests_or_summary() -> None:
    body = render.pr_body(
        ticket=Ticket(key="PROJ-1", title="Fix"),
        agent=AgentResult(outcome=AgentOutcome.COMPLETED),
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
        (None, "Tests were not run"),
        (TestReport(command=None, passed=False), "Tests were not run"),
        (TestReport(command="pytest", passed=True), "Tests pass"),
        (TestReport(command="pytest", passed=False), "Tests fail"),
    ],
)
def test_tracker_comment_with_pr(tests: TestReport | None, expected: str) -> None:
    text = render.tracker_comment(pr_url="https://x.test/pr/1", tests=tests, usage=USAGE)
    assert expected in text
    assert "$1.23" in text


def test_tracker_comment_on_failure() -> None:
    text = render.tracker_comment(pr_url=None, tests=None, usage=USAGE, error="boom")
    assert "could not open a pull request: boom" in text
    assert "unknown error" in render.tracker_comment(pr_url=None, tests=None, usage=USAGE)


def test_configuration_docs_are_up_to_date() -> None:
    script = Path(__file__).resolve().parent.parent / "scripts" / "gen_config_docs.py"
    result = subprocess.run([sys.executable, str(script), "--check"], check=False)
    assert result.returncode == 0, "run: uv run python scripts/gen_config_docs.py"
