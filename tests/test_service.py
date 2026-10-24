"""HTTP service, job queue, runtime wiring and CLI."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import signal
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from typer.testing import CliRunner

from conftest import MakePipeline, ScriptedAgent, fix_add
from ticket2pr import cli
from ticket2pr.agent.base import AgentTask
from ticket2pr.config import Settings
from ticket2pr.domain import Job, JobStatus
from ticket2pr.errors import ConfigurationError
from ticket2pr.pipeline import RunOptions
from ticket2pr.runtime import Runtime, build_runtime
from ticket2pr.scm import GitHubClient
from ticket2pr.server import _Deliveries, create_app, trigger_from_event, verify_signature
from ticket2pr.store import JobStore
from ticket2pr.trackers.jira import JiraTracker
from ticket2pr.worker import JobQueue

SECRET = "whsec"
TOKEN = "api-token"


def _sign(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


def _event(key: str = "CALC-1", event: str = "jira:issue_created", **extra: Any) -> dict[str, Any]:
    return {
        "webhookEvent": event,
        "issue": {"key": key, "fields": {"labels": ["ticket2pr"]}},
        **extra,
    }


# ── pure helpers ────────────────────────────────────────────────────────


def test_verify_signature() -> None:
    body = b'{"a":1}'
    assert verify_signature(SECRET.encode(), body, _sign(body))
    assert not verify_signature(SECRET.encode(), body, _sign(b"other"))
    assert not verify_signature(SECRET.encode(), body, "md5=abc")
    assert not verify_signature(SECRET.encode(), body, "sha256=é")  # junk is a 401, not a 500
    assert not verify_signature(SECRET.encode(), body, None)


@pytest.mark.parametrize(
    ("payload", "key", "reason"),
    [
        (_event(), "CALC-1", ""),
        ([], None, "not an object"),
        (_event(event="jira:issue_deleted"), None, "ignored event"),
        (_event(key="bad key"), None, "invalid issue key"),
        ({"webhookEvent": "jira:issue_created", "issue": {"key": "CALC-1"}}, None, "lacks"),
        (_event(event="jira:issue_updated"), None, "was not added"),
        (
            _event(
                event="jira:issue_updated",
                changelog={"items": [{"field": "status", "fromString": "a", "toString": "b"}]},
            ),
            None,
            "was not added",
        ),
        (
            _event(
                event="jira:issue_updated",
                changelog={
                    "items": [
                        {"field": "labels", "fromString": "ticket2pr", "toString": "ticket2pr ui"}
                    ]
                },
            ),
            None,
            "was not added",
        ),
        (
            _event(
                event="jira:issue_updated",
                changelog={
                    "items": [{"field": "labels", "fromString": "ui", "toString": "ui ticket2pr"}]
                },
            ),
            "CALC-1",
            "",
        ),
    ],
)
def test_trigger_from_event(payload: Any, key: str | None, reason: str) -> None:
    got_key, got_reason = trigger_from_event(payload, "ticket2pr")
    assert got_key == key
    assert reason in got_reason


# ── HTTP service ────────────────────────────────────────────────────────


@pytest.fixture
def service_settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "jira_webhook_secret": SecretStr(SECRET),
            "api_token": SecretStr(TOKEN),
            "workers": 1,
        }
    )


@pytest.fixture
def client(service_settings: Settings, tickets_dir: Path, repos_dir: Path) -> Iterator[TestClient]:
    agent = ScriptedAgent([fix_add])

    def factory(settings: Settings) -> Runtime:
        return build_runtime(settings, tickets_dir=tickets_dir, repos_dir=repos_dir, agent=agent)

    with TestClient(create_app(service_settings, runtime_factory=factory)) as test_client:
        yield test_client


def _post(client: TestClient, payload: Any, signature: str | None = None) -> Any:
    body = json.dumps(payload).encode()
    return client.post(
        "/webhooks/jira",
        content=body,
        headers={"X-Hub-Signature": signature or _sign(body), "Content-Type": "application/json"},
    )


def test_create_app_requires_webhook_secret(settings: Settings) -> None:
    with pytest.raises(ConfigurationError):
        create_app(settings)


def test_healthz(client: TestClient) -> None:
    assert client.get("/healthz").json()["status"] == "ok"


def test_webhook_rejects_bad_signature_and_body(client: TestClient) -> None:
    assert _post(client, _event(), signature="sha256=00").status_code == 401
    raw = b"not json"
    response = client.post("/webhooks/jira", content=raw, headers={"X-Hub-Signature": _sign(raw)})
    assert response.status_code == 400
    huge = b"x" * 1_000_001
    response = client.post("/webhooks/jira", content=huge, headers={"X-Hub-Signature": _sign(huge)})
    assert response.status_code == 413

    def chunks() -> Iterator[bytes]:  # no Content-Length: the limit applies while streaming
        for _ in range(20):
            yield b"x" * 100_000

    response = client.post("/webhooks/jira", content=chunks(), headers={"X-Hub-Signature": "x"})
    assert response.status_code == 413
    response = client.post(
        "/webhooks/jira", content=b"{}", headers={"Content-Length": "abc", "X-Hub-Signature": "x"}
    )
    assert response.status_code in {400, 413}


def test_webhook_ignores_irrelevant_events(client: TestClient) -> None:
    response = _post(client, _event(event="jira:issue_deleted"))
    assert response.status_code == 200
    assert response.json()["accepted"] is False


def test_webhook_runs_job_end_to_end(client: TestClient) -> None:
    response = _post(client, _event())
    assert response.status_code == 202
    job_id = response.json()["job_id"]

    queue: JobQueue = client.app.state.queue  # type: ignore[attr-defined]
    client.portal.call(queue.join)  # type: ignore[union-attr]

    auth = {"Authorization": f"Bearer {TOKEN}"}
    job = client.get(f"/jobs/{job_id}", headers=auth).json()
    assert job["status"] == JobStatus.SUCCEEDED.value
    assert job["pr_url"].startswith("file://")
    listed = client.get("/jobs", headers=auth, params={"status": "succeeded"}).json()
    assert [item["id"] for item in listed] == [job_id]


def test_webhook_deduplicates_in_flight_jobs(client: TestClient) -> None:
    runtime: Runtime = client.app.state.runtime  # type: ignore[attr-defined]
    runtime.store.claim("CALC-1", None)  # simulate a job already queued
    response = _post(client, _event())
    assert response.status_code == 200
    assert "already has a job" in response.json()["reason"]
    job = runtime.store.list()[0]  # the in-flight job ends...
    job.status = JobStatus.FAILED
    runtime.store.save(job)  # ...and a replay of that delivery must still not start one
    assert _post(client, _event()).json()["reason"] == "duplicate delivery"


def test_webhook_refuses_replays(client: TestClient) -> None:
    """The signature covers the body only: a captured delivery must not start a second run."""
    event = _event(timestamp=int(time.time() * 1000))
    assert _post(client, event).status_code == 202
    queue: JobQueue = client.app.state.queue  # type: ignore[attr-defined]
    client.portal.call(queue.join)  # type: ignore[union-attr]
    replay = _post(client, event)
    assert replay.status_code == 200
    assert replay.json()["reason"] == "duplicate delivery"
    stale = _post(client, _event(timestamp=int(time.time() * 1000) - 2 * 3600 * 1000))
    assert stale.json()["reason"] == "stale event"


def test_remembered_deliveries_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ticket2pr.server._MAX_REMEMBERED_DELIVERIES", 2)
    deliveries = _Deliveries()
    for body in (b"1", b"2", b"3"):
        deliveries.remember(body)
    assert deliveries.refusal(b"1", {}) == ""  # the oldest is forgotten
    assert deliveries.refusal(b"2", {}) == deliveries.refusal(b"3", {}) == "duplicate delivery"


def test_a_delivery_that_errored_can_be_retried(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Jira retries a delivery that got a 5xx: it must not then count as a replay."""
    queue: JobQueue = client.app.state.queue  # type: ignore[attr-defined]
    submit = queue.submit
    calls = 0

    def flaky(key: str) -> Job | None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("database is locked")
        return submit(key)

    monkeypatch.setattr(queue, "submit", flaky)
    event = _event()
    with pytest.raises(RuntimeError):
        _post(client, event)
    assert _post(client, event).status_code == 202


def test_jobs_api_auth(client: TestClient) -> None:
    assert client.get("/jobs").status_code == 401
    assert client.get("/jobs", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/jobs/999", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 404


def test_jobs_api_disabled_without_token(
    service_settings: Settings, tickets_dir: Path, repos_dir: Path
) -> None:
    settings = service_settings.model_copy(update={"api_token": SecretStr("")})
    app = create_app(
        settings,
        runtime_factory=lambda s: build_runtime(s, tickets_dir=tickets_dir, repos_dir=repos_dir),
    )
    with TestClient(app) as test_client:
        assert test_client.get("/jobs", headers={"Authorization": "Bearer "}).status_code == 404


def test_startup_fails_interrupted_jobs(
    service_settings: Settings, tickets_dir: Path, repos_dir: Path
) -> None:
    def factory(settings: Settings) -> Runtime:
        runtime = build_runtime(settings, tickets_dir=tickets_dir, repos_dir=repos_dir)
        job = runtime.store.claim("CALC-9", None)
        assert job is not None
        job.status = JobStatus.RUNNING
        runtime.store.save(job)
        (settings.workspaces_dir / f"job-{job.id}" / "work").mkdir(parents=True)  # left behind
        (settings.workspaces_dir / "job-999").mkdir()  # kept on purpose (--keep-workspace)
        (settings.workspaces_dir / "job-999" / ".keep").touch()
        (settings.workspaces_dir / "job-998").mkdir()  # its job failed, but cleanup was killed
        return runtime

    with TestClient(create_app(service_settings, runtime_factory=factory)) as test_client:
        runtime: Runtime = test_client.app.state.runtime  # type: ignore[attr-defined]
        assert runtime.store.list()[0].status is JobStatus.FAILED
        kept = runtime.settings.workspaces_dir.glob("job-*")
        assert [path.name for path in kept] == ["job-999"]


def test_startup_resumes_queued_jobs(
    service_settings: Settings, tickets_dir: Path, repos_dir: Path
) -> None:
    def factory(settings: Settings) -> Runtime:
        runtime = build_runtime(
            settings, tickets_dir=tickets_dir, repos_dir=repos_dir, agent=ScriptedAgent([fix_add])
        )
        runtime.store.claim("CALC-1", None)  # left queued by a previous process
        runtime.store.claim("../../secure/x?#", None)  # tampered with (unsandboxed mode)
        return runtime

    with TestClient(create_app(service_settings, runtime_factory=factory)) as test_client:
        queue: JobQueue = test_client.app.state.queue  # type: ignore[attr-defined]
        test_client.portal.call(queue.join)  # type: ignore[union-attr]
        runtime: Runtime = test_client.app.state.runtime  # type: ignore[attr-defined]
        statuses = {job.ticket_key: job.status for job in runtime.store.list()}
        assert statuses == {"CALC-1": JobStatus.SUCCEEDED, "../../secure/x?#": JobStatus.FAILED}


# ── queue ───────────────────────────────────────────────────────────────


async def test_queue_survives_pipeline_crash(make_pipeline: MakePipeline, store: JobStore) -> None:
    pipeline = make_pipeline(ScriptedAgent([fix_add]))

    async def explode(job: Job, options: RunOptions | None = None) -> Job:
        raise RuntimeError("bug")

    pipeline.run = explode  # type: ignore[method-assign]
    queue = JobQueue(pipeline=pipeline, store=store, workers=1)
    queue.start()
    with pytest.raises(RuntimeError, match="already started"):
        queue.start()
    job = queue.submit("CALC-1")
    assert job is not None
    await queue.join()
    await queue.stop()
    assert job.id is not None
    saved = store.get(job.id)
    assert saved is not None
    assert saved.status is JobStatus.FAILED
    assert saved.error == "internal error: RuntimeError"


# ── runtime ─────────────────────────────────────────────────────────────


async def test_build_runtime_production_wiring(settings: Settings) -> None:
    configured = settings.model_copy(
        update={
            "jira_base_url": "https://acme.atlassian.net",
            "jira_email": "bot@acme.test",
            "jira_api_token": SecretStr("j"),
            "scm_token": SecretStr("g"),
        }
    )
    runtime = build_runtime(configured)
    assert isinstance(runtime.tracker, JiraTracker)
    assert isinstance(runtime.host, GitHubClient)
    await runtime.aclose()


# ── CLI ─────────────────────────────────────────────────────────────────

runner = CliRunner()


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    monkeypatch.setenv("T2P_DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv("T2P_REPOS", json.dumps(settings.repos))
    monkeypatch.setenv("T2P_TEST_COMMAND", settings.test_command)
    monkeypatch.setattr("ticket2pr.runtime.build_agent", lambda _: ScriptedAgent([fix_add]))


def test_cli_version() -> None:
    result = runner.invoke(cli.app, ["--version"])
    assert result.exit_code == 0
    assert "ticket2pr" in result.output


def test_cli_run_then_list_jobs(
    cli_env: None, tickets_dir: Path, repos_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("T2P_FORK_OWNER", "bot")  # ignored: local repositories have no forks
    args = ["run", "CALC-1", "--tickets-dir", str(tickets_dir), "--repos-dir", str(repos_dir)]
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 0, result.output
    assert "succeeded" in result.output
    assert "PR: file://" in result.output

    listing = runner.invoke(cli.app, ["jobs"])
    assert listing.exit_code == 0
    assert "CALC-1" in listing.output
    assert "$0.50" in listing.output


def test_cli_run_failure_exit_code(cli_env: None, tickets_dir: Path, repos_dir: Path) -> None:
    args = ["run", "CALC-404", "--tickets-dir", str(tickets_dir), "--repos-dir", str(repos_dir)]
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 1
    assert "failed" in result.output


def test_cli_run_refuses_a_ticket_already_in_progress(
    cli_env: None, tickets_dir: Path, repos_dir: Path
) -> None:
    with JobStore(Settings().db_path) as store:
        store.claim("CALC-1", None)
    args = ["run", "CALC-1", "--tickets-dir", str(tickets_dir), "--repos-dir", str(repos_dir)]
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 1
    assert "already has a job in progress" in result.output


def test_cli_run_records_sigterm_as_interrupted(
    cli_env: None, tickets_dir: Path, repos_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def terminated(task: AgentTask) -> None:
        os.kill(os.getpid(), signal.SIGTERM)

    monkeypatch.setattr("ticket2pr.runtime.build_agent", lambda _: ScriptedAgent([terminated]))
    args = ["run", "CALC-1", "--tickets-dir", str(tickets_dir), "--repos-dir", str(repos_dir)]
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 143, result.output
    with JobStore(Settings().db_path) as store:
        assert store.list()[0].status is JobStatus.FAILED  # not left "running"


def test_cli_run_rejects_a_repo_without_owner() -> None:
    result = runner.invoke(cli.app, ["run", "CALC-1", "--repo", "shop"])
    assert result.exit_code == 2
    assert "owner/name" in result.output


def test_cli_run_rejects_bad_key() -> None:
    result = runner.invoke(cli.app, ["run", "not-a-key"])
    assert result.exit_code == 2


def test_cli_invalid_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("T2P_REPOS", '{"bad": "x"}')
    result = runner.invoke(cli.app, ["jobs"])
    assert result.exit_code == 2
    assert "Invalid configuration" in result.output


def test_cli_doctor_reports_missing_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))  # no `claude` login either
    result = runner.invoke(cli.app, ["doctor"])
    assert result.exit_code == 1
    assert "✗" in result.output
    assert "github token" in result.output


def test_doctor_accepts_a_cloud_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, settings: Settings
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    assert not cli._has_claude_credentials(settings)
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "False")
    assert not cli._has_claude_credentials(settings)
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    assert cli._has_claude_credentials(settings)


def test_cli_doctor_names_what_the_sandbox_lacks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("T2P_SANDBOX", "true")
    monkeypatch.setattr(cli, "missing_sandbox_tools", lambda: ["socat"])
    result = runner.invoke(cli.app, ["doctor"])
    assert result.exit_code == 1
    assert "missing: socat" in result.output


@pytest.mark.parametrize("credential", ["api_key", "claude_login", "claude_config_dir"])
def test_cli_doctor_all_green(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, credential: str
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    if credential == "api_key":
        monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    elif credential == "claude_config_dir":
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
        (tmp_path / "cfg").mkdir()
        (tmp_path / "cfg" / ".credentials.json").write_text("{}")
    else:
        (tmp_path / ".claude").mkdir()
        (tmp_path / ".claude" / ".credentials.json").write_text("{}")
    for name, value in {
        "T2P_JIRA_BASE_URL": "https://a.atlassian.net",
        "T2P_JIRA_EMAIL": "a@b.c",
        "T2P_JIRA_API_TOKEN": "t",
        "T2P_GITHUB_TOKEN": "g",
        "T2P_JIRA_WEBHOOK_SECRET": "s",
        "T2P_REPOS": '{"PROJ": "acme/api"}',
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(cli, "missing_sandbox_tools", list)
    result = runner.invoke(cli.app, ["doctor"])
    assert result.exit_code == 0, result.output


def test_cli_serve_without_secret_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("T2P_JIRA_WEBHOOK_SECRET", raising=False)
    result = runner.invoke(cli.app, ["serve"])
    assert result.exit_code == 2
    assert "T2P_JIRA_WEBHOOK_SECRET" in result.output


PRODUCTION_ENV = {
    "T2P_JIRA_WEBHOOK_SECRET": "s",
    "T2P_JIRA_BASE_URL": "https://a.atlassian.net",
    "T2P_JIRA_EMAIL": "a@b.c",
    "T2P_JIRA_API_TOKEN": "t",
    "T2P_GITHUB_TOKEN": "g",
}


def test_cli_serve_reports_config_errors_cleanly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("T2P_JIRA_WEBHOOK_SECRET", "s")  # but no Jira / GitHub credentials
    result = runner.invoke(cli.app, ["serve"])
    assert result.exit_code == 2
    assert "Jira needs" in result.output
    assert "Traceback" not in result.output


def test_cli_serve_starts_uvicorn(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in PRODUCTION_ENV.items():
        monkeypatch.setenv(name, value)
    calls: list[tuple[str, int]] = []

    def fake_run(app: Any, host: str, port: int, log_config: Any) -> None:
        calls.append((host, port))
        app.state.runtime.store.close()  # the lifespan that would close it never runs

    monkeypatch.setattr("uvicorn.run", fake_run)
    result = runner.invoke(cli.app, ["serve", "--port", "9999"])
    assert result.exit_code == 0, result.output
    assert calls == [("127.0.0.1", 9999)]


def test_resumed_jobs_never_trust_the_repo_stored_in_the_database(
    service_settings: Settings, tickets_dir: Path, repos_dir: Path
) -> None:
    def factory(settings: Settings) -> Runtime:
        runtime = build_runtime(
            settings, tickets_dir=tickets_dir, repos_dir=repos_dir, agent=ScriptedAgent([fix_add])
        )
        runtime.store.claim("CALC-1", "attacker/elsewhere")  # planted row
        return runtime

    with TestClient(create_app(service_settings, runtime_factory=factory)) as test_client:
        queue: JobQueue = test_client.app.state.queue  # type: ignore[attr-defined]
        test_client.portal.call(queue.join)  # type: ignore[union-attr]
        runtime: Runtime = test_client.app.state.runtime  # type: ignore[attr-defined]
        job = runtime.store.list()[0]
        assert job.repo == "acme/calc"  # from T2P_REPOS, not from the row
        assert job.status is JobStatus.SUCCEEDED
