"""Command-line interface: ``ticket2pr run | serve | jobs | eval | doctor``."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from ticket2pr import __version__
from ticket2pr.config import CLAUDE_CREDENTIAL_ENV_VARS, REPO_RE, Settings
from ticket2pr.domain import Job, JobStatus, is_ticket_key
from ticket2pr.errors import Ticket2PRError
from ticket2pr.pipeline import RunOptions
from ticket2pr.runtime import open_runtime
from ticket2pr.sandbox import harden_service_process, missing_sandbox_tools
from ticket2pr.store import JobStore

app = typer.Typer(
    help="Turn tickets into review-ready pull requests with a budget-capped Claude agent.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()

_STATUS_STYLE = {
    JobStatus.SUCCEEDED: "green",
    JobStatus.NEEDS_REVIEW: "yellow",
    JobStatus.FAILED: "red",
    JobStatus.SKIPPED: "dim",
}


def _version(value: bool) -> None:
    if value:
        console.print(f"ticket2pr {__version__}")
        raise typer.Exit


@app.callback()
def main(
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging.")] = False,
    _version_flag: Annotated[
        bool,
        typer.Option("--version", callback=_version, is_eager=True, help="Show the version."),
    ] = False,
) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        handlers=[RichHandler(console=console, show_path=False)],
    )


def _settings() -> Settings:
    try:
        settings = Settings()
    except ValidationError as exc:
        console.print(f"[red]Invalid configuration:[/red]\n{exc}")
        raise typer.Exit(2) from exc
    # This process holds the API tokens: keep same-uid processes (the agent, the tests) from
    # reading them out of /proc, and, without the sandbox, adopt what they daemonise.
    harden_service_process(settings)
    return settings


@app.command()
def run(
    ticket: Annotated[str, typer.Argument(help="Ticket key, e.g. PROJ-42.")],
    repo: Annotated[
        str | None, typer.Option(help="owner/name; overrides the T2P_REPOS mapping.")
    ] = None,
    tickets_dir: Annotated[
        Path | None, typer.Option(help="Read tickets from YAML files here instead of Jira.")
    ] = None,
    repos_dir: Annotated[
        Path | None, typer.Option(help="Serve repos from <dir>/<owner>/<name> instead of GitHub.")
    ] = None,
    require_label: Annotated[
        bool, typer.Option(help="Skip tickets that lack the trigger label.")
    ] = False,
    keep_workspace: Annotated[bool, typer.Option(help="Keep the clone for inspection.")] = False,
) -> None:
    """Process one ticket now, in the foreground."""
    if not is_ticket_key(ticket):
        raise typer.BadParameter(f"{ticket!r} is not a ticket key like PROJ-42")
    if repo is not None and not REPO_RE.fullmatch(repo):
        raise typer.BadParameter(f"--repo {repo!r} is not owner/name")
    settings = _settings()
    options = RunOptions(require_label=require_label, keep_workspace=keep_workspace)

    async def _run() -> Job:
        # SIGTERM (timeout, CI cancel, docker stop) cancels like Ctrl-C, so the job is recorded
        # as interrupted instead of being left "running" and blocking the ticket.
        main = asyncio.current_task()
        assert main is not None  # noqa: S101 - called by asyncio.run
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, main.cancel)
        async with open_runtime(settings, tickets_dir=tickets_dir, repos_dir=repos_dir) as rt:
            job = rt.store.claim(ticket, repo)
            if job is None:
                raise Ticket2PRError(f"{ticket} already has a job in progress")
            return await rt.pipeline.run(job, options)

    try:
        job = asyncio.run(_run())
    except Ticket2PRError as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(1) from exc
    except asyncio.CancelledError:
        console.print("[red]interrupted[/red]")
        raise typer.Exit(143) from None
    _print_job(job)
    raise typer.Exit(0 if job.status in {JobStatus.SUCCEEDED, JobStatus.NEEDS_REVIEW} else 1)


@app.command()
def serve(
    host: Annotated[str, typer.Option(help="Bind address.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Bind port.")] = 8080,
) -> None:
    """Run the webhook service."""
    import uvicorn  # noqa: PLC0415 - heavy import, only needed for this command

    from ticket2pr.server import create_app  # noqa: PLC0415

    try:
        application = create_app(_settings())
    except Ticket2PRError as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(2) from exc
    uvicorn.run(application, host=host, port=port, log_config=None)


@app.command()
def jobs(
    limit: Annotated[int, typer.Option(min=1, max=500)] = 20,
    status: Annotated[JobStatus | None, typer.Option(help="Filter by status.")] = None,
) -> None:
    """List recent jobs."""
    with JobStore(_settings().db_path) as store:
        rows = store.list(limit=limit, status=status)
    table = Table("ID", "Ticket", "Status", "Cost", "Tokens", "PR / error", box=None)
    for job in rows:
        style = _STATUS_STYLE.get(job.status, "")
        table.add_row(
            str(job.id),
            job.ticket_key,
            f"[{style}]{job.status.value}[/{style}]" if style else job.status.value,
            job.usage.cost_label,
            f"{job.usage.total_tokens:,}",
            job.pr_url or (job.error or "")[:80],
        )
    console.print(table)


@app.command()
def doctor() -> None:
    """Check that the environment can run jobs."""
    settings = _settings()
    missing = missing_sandbox_tools() if settings.sandbox else []
    sandbox_check = "sandbox: bwrap + socat usable (or T2P_SANDBOX=false)" + (
        f" — missing: {', '.join(missing)}" if missing else ""
    )
    checks = {
        "git on PATH": shutil.which("git") is not None,
        "Claude credentials (API key, auth token or `claude` login)": _has_claude_credentials(
            settings
        ),
        "Jira configured": bool(
            settings.jira_base_url
            and settings.jira_email
            and settings.jira_api_token.get_secret_value()
        ),
        "GitHub token": bool(settings.github_token.get_secret_value()),
        "webhook secret": bool(settings.jira_webhook_secret.get_secret_value()),
        "at least one project → repo mapping": bool(settings.repos),
        sandbox_check: not missing,
    }
    recommended = {
        "pushes to a bot-owned fork (T2P_FORK_OWNER), so CI secrets stay out of reach": bool(
            settings.fork_owner
        ),
    }
    for name, ok in checks.items():
        console.print(f"{'[green]✓[/green]' if ok else '[red]✗[/red]'} {name}")
    for name, ok in recommended.items():
        console.print(f"{'[green]✓[/green]' if ok else '[yellow]![/yellow]'} {name}")
    raise typer.Exit(0 if all(checks.values()) else 1)


@app.command("eval")
def eval_(
    tasks_dir: Annotated[
        Path,
        typer.Option(help="Directory of eval tasks.", exists=True, file_okay=False),
    ] = Path("evals/tasks"),
    task: Annotated[list[str] | None, typer.Option(help="Run only these tasks.")] = None,
    agent: Annotated[
        str, typer.Option(help="'claude' (real, costs money) or 'oracle'.")
    ] = "oracle",
    output: Annotated[Path | None, typer.Option(help="Write the JSON report here.")] = None,
) -> None:
    """Measure resolve rate and cost on the offline eval suite."""
    from ticket2pr.evals import discover, run_suite  # noqa: PLC0415 - only for this command
    from ticket2pr.runtime import build_agent  # noqa: PLC0415

    if agent not in {"claude", "oracle"}:
        raise typer.BadParameter("--agent must be 'claude' or 'oracle'")
    settings = _settings()
    try:
        tasks = discover(tasks_dir, task)
    except Ticket2PRError as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(2) from exc
    coding_agent = build_agent(settings) if agent == "claude" else None
    summary = asyncio.run(run_suite(tasks, settings=settings, agent=coding_agent))

    table = Table("Task", "Ticket", "Status", "Resolved", "Cost", "Tokens", "Turns", box=None)
    for result in summary.results:
        table.add_row(
            result.task,
            result.ticket,
            result.status,
            "[green]yes[/green]" if result.resolved else "[red]no[/red]",
            f"${result.cost_usd:.2f}",
            f"{result.total_tokens:,}",
            str(result.turns),
        )
    console.print(table)
    for result in summary.results:
        if result.error:
            console.print(f"[red]{result.task}:[/red] {result.error}", soft_wrap=True)
    per_resolved = summary.cost_per_resolved_usd
    console.print(
        f"Resolved {summary.resolve_rate:.0%} of {len(summary.results)} task(s) · "
        f"total ${summary.total_cost_usd:.2f} · "
        f"per resolved ticket {'-' if per_resolved is None else f'${per_resolved:.2f}'}"
    )
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(summary.to_json(), encoding="utf-8")
    raise typer.Exit(0 if summary.resolve_rate == 1 else 1)


def _has_claude_credentials(settings: Settings) -> bool:
    """Any credential the Claude Agent SDK can use: a key, a token, or a `claude` login."""
    if settings.anthropic_api_key.get_secret_value():
        return True
    if any(os.environ.get(name) for name in CLAUDE_CREDENTIAL_ENV_VARS):
        return True
    config_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    return (config_dir.expanduser() / ".credentials.json").is_file()


def _print_job(job: Job) -> None:
    style = _STATUS_STYLE.get(job.status, "")
    console.print(f"[bold]{job.ticket_key}[/bold] → [{style}]{job.status.value}[/{style}]")
    if job.pr_url:
        console.print(f"PR: {job.pr_url}", soft_wrap=True)
    if job.error:
        console.print(f"Reason: {job.error}", soft_wrap=True)
    console.print(
        f"Cost {job.usage.cost_label} · {job.usage.total_tokens:,} tokens · {job.usage.turns} turns"
    )
