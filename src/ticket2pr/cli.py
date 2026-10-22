"""Command-line interface: ``ticket2pr run | serve | jobs | eval | doctor``."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from ticket2pr import __version__
from ticket2pr.config import Settings
from ticket2pr.domain import Job, JobStatus, is_ticket_key
from ticket2pr.errors import Ticket2PRError
from ticket2pr.pipeline import RunOptions
from ticket2pr.runtime import open_runtime
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
        return Settings()
    except ValidationError as exc:
        console.print(f"[red]Invalid configuration:[/red]\n{exc}")
        raise typer.Exit(2) from exc


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
    settings = _settings()
    options = RunOptions(repo=repo, require_label=require_label, keep_workspace=keep_workspace)

    async def _run() -> Job:
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
            f"${job.usage.cost_usd:.2f}",
            f"{job.usage.total_tokens:,}",
            job.pr_url or (job.error or "")[:80],
        )
    console.print(table)


@app.command()
def doctor() -> None:
    """Check that the environment can run jobs."""
    settings = _settings()
    checks = {
        "git on PATH": shutil.which("git") is not None,
        "Claude credentials (ANTHROPIC_API_KEY)": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "Jira configured": bool(
            settings.jira_base_url
            and settings.jira_email
            and settings.jira_api_token.get_secret_value()
        ),
        "GitHub token": bool(settings.github_token.get_secret_value()),
        "webhook secret": bool(settings.jira_webhook_secret.get_secret_value()),
        "at least one project → repo mapping": bool(settings.repos),
        "graphify (optional)": shutil.which("graphify") is not None,
    }
    for name, ok in checks.items():
        console.print(f"{'[green]✓' if ok else '[red]✗'}[/] {name}")
    required = [ok for name, ok in checks.items() if "optional" not in name]
    raise typer.Exit(0 if all(required) else 1)


@app.command("eval")
def eval_(
    tasks_dir: Annotated[Path, typer.Option(help="Directory of eval tasks.")] = Path("evals/tasks"),
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
    per_resolved = summary.cost_per_resolved_usd
    console.print(
        f"Resolved {summary.resolve_rate:.0%} of {len(summary.results)} task(s) · "
        f"total ${summary.total_cost_usd:.2f} · "
        f"per resolved ticket {'-' if per_resolved is None else f'${per_resolved:.2f}'}"
    )
    if output is not None:
        output.write_text(summary.to_json(), encoding="utf-8")
    raise typer.Exit(0 if summary.resolve_rate == 1 else 1)


def _print_job(job: Job) -> None:
    style = _STATUS_STYLE.get(job.status, "")
    console.print(f"[bold]{job.ticket_key}[/bold] → [{style}]{job.status.value}[/{style}]")
    if job.pr_url:
        console.print(f"PR: {job.pr_url}", soft_wrap=True)
    if job.error:
        console.print(f"Reason: {job.error}", soft_wrap=True)
    console.print(
        f"Cost ${job.usage.cost_usd:.2f} · {job.usage.total_tokens:,} tokens · "
        f"{job.usage.turns} turns"
    )
