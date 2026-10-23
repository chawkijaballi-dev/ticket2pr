"""Runtime configuration, loaded from environment variables (prefix ``T2P_``) or a ``.env`` file."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_MODEL = "claude-opus-5-5"

_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_PROJECT_RE = re.compile(r"^[A-Z][A-Z0-9_]+$")

Effort = Literal["low", "medium", "high", "xhigh", "max"]


class Settings(BaseSettings):
    """All knobs of a ticket2pr deployment. Every field maps to ``T2P_<FIELD_NAME>``."""

    model_config = SettingsConfigDict(
        env_prefix="T2P_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Jira ──────────────────────────────────────────────────────────
    jira_base_url: str = Field(
        default="", description="Jira Cloud site, e.g. https://acme.atlassian.net."
    )
    jira_email: str = Field(default="", description="Account that owns the API token.")
    jira_api_token: SecretStr = Field(default=SecretStr(""), description="Jira API token.")
    jira_webhook_secret: SecretStr = Field(
        default=SecretStr(""),
        description="Shared secret Jira signs webhooks with (required by serve).",
    )
    trigger_label: str = Field(
        default="ticket2pr", description="Only tickets carrying this label are processed."
    )

    # ── GitHub ────────────────────────────────────────────────────────
    github_token: SecretStr = Field(
        default=SecretStr(""),
        description="Fine-grained token: Contents + Pull requests (read/write).",
    )
    github_api_url: str = Field(
        default="https://api.github.com", description="Change for GitHub Enterprise Server."
    )
    repos: dict[str, str] = Field(
        default_factory=dict,
        description='Jira project key → GitHub repo, e.g. {"PROJ": "acme/api"}.',
    )
    fork_owner: str = Field(
        default="",
        description="Push branches to <fork_owner>/<repo> (a fork owned by the bot) and open "
        "cross-repository PRs. Recommended: a branch pushed to the target repository runs its "
        "CI with the repository's secrets before anyone reviews the agent's code.",
    )
    base_branch: str = Field(default="main", description="Branch to clone and target with PRs.")
    branch_prefix: str = Field(default="ticket2pr/", description="Prefix of the branches pushed.")
    commit_author_name: str = Field(default="ticket2pr", description="Commit author name.")
    commit_author_email: str = Field(
        default="ticket2pr@users.noreply.github.com", description="Commit author email."
    )

    # ── Agent ─────────────────────────────────────────────────────────
    anthropic_api_key: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=AliasChoices("ANTHROPIC_API_KEY", "T2P_ANTHROPIC_API_KEY"),
        description="Claude API key (also read without the T2P_ prefix). Empty: the Claude "
        "Agent SDK uses its own credentials (environment or `claude` login).",
    )
    model: str = Field(default=DEFAULT_MODEL, description="Claude model the agent runs on.")
    effort: Effort = Field(default="high", description="low, medium, high, xhigh or max.")
    max_budget_usd: float = Field(default=5.0, gt=0, description="Hard spend cap per ticket.")
    max_turns: int = Field(default=80, gt=0, description="Agent turn cap per attempt.")
    agent_timeout_s: int = Field(
        default=1800, gt=0, description="Wall-clock limit per agent attempt, in seconds."
    )
    sandbox: bool = Field(
        default=True,
        description="Run the agent's Bash and the repository's tests in a bubblewrap sandbox "
        "(Linux; needs bwrap and socat). Jobs fail when it cannot start. Disable only when the "
        "worker itself runs in an isolated, egress-restricted container.",
    )
    test_network: bool = Field(
        default=False,
        description="Give the sandboxed test run network access (e.g. when the test command "
        "installs dependencies, like `uv run pytest`).",
    )
    sandbox_readonly_paths: list[Path] = Field(
        default_factory=list,
        description="Extra toolchain directories under $HOME or the working directory that "
        "the test sandbox must still see (read-only).",
    )
    network_allowlist: list[str] = Field(
        default_factory=lambda: ["pypi.org", "files.pythonhosted.org"],
        description="Domains the agent's sandboxed Bash may reach (package registries). Each is "
        "also a way out for repository data: avoid registries that accept uploads on the same "
        "host, such as registry.npmjs.org (npm publish).",
    )

    # ── Guardrails & verification ─────────────────────────────────────
    test_command: str = Field(default="", description="Overrides auto-detection when set.")
    test_timeout_s: int = Field(default=900, gt=0, description="Test run limit, in seconds.")
    max_changed_files: int = Field(
        default=25, gt=0, description="Larger changes fail the job instead of opening a PR."
    )
    protected_paths: list[str] = Field(
        default_factory=lambda: [".github/", ".git/", "**/.env", "**/*.pem", "**/*.key"],
        description="Glob patterns the agent may neither edit nor commit.",
    )
    draft_on_test_failure: bool = Field(
        default=True, description="Open PRs as drafts when the tests fail or are missing."
    )

    # ── Service ───────────────────────────────────────────────────────
    data_dir: Path = Field(
        default=Path(".ticket2pr"), description="Job database and workspaces live here."
    )
    api_token: SecretStr = Field(
        default=SecretStr(""), description="Bearer token for the /jobs API; empty disables it."
    )
    workers: int = Field(
        default=2,
        gt=0,
        le=32,
        description="Concurrent jobs in serve mode. Forced to 1 when the sandbox is disabled.",
    )

    @field_validator("repos")
    @classmethod
    def _check_repos(cls, value: dict[str, str]) -> dict[str, str]:
        for project, repo in value.items():
            if not _PROJECT_RE.fullmatch(project):
                raise ValueError(f"invalid Jira project key: {project!r}")
            if not _REPO_RE.fullmatch(repo):
                raise ValueError(f"invalid GitHub repo for {project}: {repo!r} (want owner/name)")
        return value

    @model_validator(mode="after")
    def _one_worker_without_sandbox(self) -> Settings:
        # Without the sandbox, jobs share a user and a PID namespace: one job's processes could
        # tamper with another job's publish clone, and reaping them would kill other jobs.
        if not self.sandbox and self.workers != 1:
            self.workers = 1
        return self

    @field_validator("sandbox_readonly_paths")
    @classmethod
    def _expand_readonly_paths(cls, value: list[Path]) -> list[Path]:
        return [path.expanduser() for path in value]  # "~/.rustup" from a .env

    @field_validator("fork_owner")
    @classmethod
    def _check_fork_owner(cls, value: str) -> str:
        if value and not _OWNER_RE.fullmatch(value):
            raise ValueError(f"invalid GitHub account name: {value!r}")
        return value

    @field_validator("data_dir")
    @classmethod
    def _absolute_data_dir(cls, value: Path) -> Path:
        # Workspaces are handed to git and to subprocesses with other working directories.
        return value.expanduser().resolve()

    @field_validator("jira_base_url", "github_api_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @property
    def workspaces_dir(self) -> Path:
        return self.data_dir / "workspaces"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "jobs.sqlite3"

    def repo_for(self, ticket_key: str) -> str | None:
        """Return the GitHub repo configured for the ticket's Jira project, if any."""
        project = ticket_key.split("-", 1)[0]
        return self.repos.get(project)


# Credentials the Claude CLI may need. They must reach it, and Claude Code strips them from
# the agent's commands (CLAUDE_CODE_SUBPROCESS_ENV_SCRUB).
CLAUDE_CREDENTIAL_ENV_VARS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
)
# Credentials of other tools that may sit in the worker's environment: blanked for the agent.
OTHER_CREDENTIAL_ENV_VARS = ("GITHUB_TOKEN", "GH_TOKEN")


def secret_env_names() -> tuple[str, ...]:
    """Every environment variable that can carry a ticket2pr secret.

    Derived from the ``SecretStr`` settings (with their aliases), so a new secret setting is
    hidden from the agent without anyone having to remember a second list.
    """
    names: set[str] = set(OTHER_CREDENTIAL_ENV_VARS)
    for name, field in Settings.model_fields.items():
        if field.annotation is not SecretStr:
            continue
        names.add(f"T2P_{name.upper()}")
        alias = field.validation_alias
        if isinstance(alias, AliasChoices):
            names.update(choice for choice in alias.choices if isinstance(choice, str))
    # Settings match variable names case-insensitively: hide every spelling that is set,
    # except the exact Claude credential names, which Claude Code itself needs.
    known = names | set(CLAUDE_CREDENTIAL_ENV_VARS)
    names.update(key for key in os.environ if key.upper() in known)
    names.difference_update(CLAUDE_CREDENTIAL_ENV_VARS)
    return tuple(sorted(names))
