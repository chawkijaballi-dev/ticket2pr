"""Runtime configuration, loaded from environment variables (prefix ``T2P_``) or a ``.env`` file."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_MODEL = "claude-opus-5-5"

# owner/name; GitLab nests groups (group/sub/name); Azure DevOps is org/project/repo (the
# collection in the organization's place on a Server), and its project and repository names
# may hold spaces.
REPO_RE = re.compile(r"^[A-Za-z0-9_.~-]+(?:/[A-Za-z0-9_.-]+)+$")
_AZURE_SEGMENT = r"[A-Za-z0-9_.-]+(?: [A-Za-z0-9_.-]+)*"
_AZURE_REPO_RE = re.compile(rf"^[A-Za-z0-9_.-]+/{_AZURE_SEGMENT}/{_AZURE_SEGMENT}$")
# A fork's namespace; Bitbucket Data Center's personal projects start with "~".
_OWNER_RE = re.compile(r"^~?[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
_PROJECT_RE = re.compile(r"^[A-Z][A-Z0-9_]+$")

Effort = Literal["low", "medium", "high", "xhigh", "max"]
ScmProvider = Literal["github", "gitlab", "bitbucket", "bitbucket-server", "azure", "gitea"]
DEFAULT_SCM_API = {  # each forge's cloud; self-hosted-only forges have none
    "github": "https://api.github.com",
    "gitlab": "https://gitlab.com/api/v4",
    "bitbucket": "https://api.bitbucket.org/2.0",
    "azure": "https://dev.azure.com",
}


# Azure DevOps: no fork pull requests over its API. Bitbucket Cloud: access tokens are scoped to
# one repository or workspace, so none can push to a fork and open the pull request.
FORKLESS_PROVIDERS = ("azure", "bitbucket")


def repo_error(provider: ScmProvider, repo: str) -> str | None:
    """Why ``repo`` is not a repository path for ``provider``, or ``None``."""
    segments = repo.split("/")
    if any(segment in (".", "..") for segment in segments):
        return "no . or .. segments"
    if provider == "azure":
        return None if _AZURE_REPO_RE.fullmatch(repo) else "want organization/project/repository"
    if not REPO_RE.fullmatch(repo):
        return "want owner/name"
    if provider != "gitlab" and repo.count("/") != 1:  # only GitLab nests groups
        return "want owner/name"
    return None


class Settings(BaseSettings):
    """All knobs of a ticket2pr deployment. Every field maps to ``T2P_<FIELD_NAME>``."""

    model_config = SettingsConfigDict(
        env_prefix="T2P_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,  # Settings(scm_token=...) as well as its environment aliases
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
        default="ticket2pr",
        description="Webhook jobs need this label (`run` checks it only with --require-label).",
    )

    # ── Source control ────────────────────────────────────────────────
    scm_provider: ScmProvider = Field(
        default="github",
        description="Where repositories live: github, gitlab, bitbucket (Cloud), "
        "bitbucket-server (Data Center), azure (Azure DevOps) or gitea (Gitea, Forgejo).",
    )
    scm_token: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=AliasChoices("T2P_SCM_TOKEN", "T2P_GITHUB_TOKEN"),
        description="Token that reads and pushes the repositories and opens pull requests "
        "(docs/security.md lists the scopes per forge). Also read from T2P_GITHUB_TOKEN.",
    )
    scm_api_url: str = Field(
        default="",
        validation_alias=AliasChoices("T2P_SCM_API_URL", "T2P_GITHUB_API_URL"),
        description="API base URL, for self-hosted forges: https://<host>/api/v3 (GitHub "
        "Enterprise), https://<host>/api/v4 (GitLab), https://<host>/rest/api/1.0 (Bitbucket "
        "Data Center; required), https://<host>[/tfs] (Azure DevOps Server; the collection goes in "
        "T2P_REPOS), https://<host>/api/v1 (Gitea, Forgejo; required). Empty: the provider's "
        "cloud.",
    )
    repos: dict[str, str] = Field(
        default_factory=dict,
        description='Jira project key → repository, e.g. {"PROJ": "acme/api"} '
        "(group/subgroup/name on GitLab, organization/project/repository on Azure DevOps).",
    )
    fork_owner: str = Field(
        default="",
        description="Push branches to <fork_owner>/<name> (a fork owned by the bot) and open "
        "cross-repository pull requests. Recommended: a branch pushed to the target repository "
        "runs its CI with the repository's secrets before anyone reviews the agent's code. "
        "Not supported with Azure DevOps or Bitbucket Cloud.",
    )
    base_branch: str = Field(default="main", description="Branch to clone and target with PRs.")
    branch_prefix: str = Field(default="ticket2pr/", description="Prefix of the branches pushed.")
    commit_author_name: str = Field(default="ticket2pr", description="Commit author name.")
    commit_author_email: str = Field(
        default="ticket2pr@users.noreply.github.com",
        description="Commit author email. Also how re-runs recognise ticket2pr's own commits: "
        "a branch whose head has another author or committer email is never overwritten.",
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
        "the agent's Bash and the test sandbox must still see (read-only).",
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
        default_factory=lambda: [
            *(".git/", "**/.env", "**/*.pem", "**/*.key"),
            # Each forge's CI configuration: it runs with the repository's secrets.
            *(".github/", ".gitlab-ci.yml", ".gitlab/", "bitbucket-pipelines.yml"),
            *("azure-pipelines.yml", "azure-pipelines.yaml", ".azure-pipelines/"),
            *(".gitea/", ".forgejo/", ".woodpecker.yml", ".woodpecker.yaml", ".woodpecker/"),
        ],
        description="Glob patterns never committed; the agent's file tools may not write them "
        "(its Bash commands are not checked).",
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
        for project in value:
            if not _PROJECT_RE.fullmatch(project):
                raise ValueError(f"invalid Jira project key: {project!r}")
        return value

    @model_validator(mode="after")
    def _check_scm(self) -> Settings:
        for project, repo in self.repos.items():
            if error := repo_error(self.scm_provider, repo):
                raise ValueError(
                    f"invalid {self.scm_provider} repository for {project}: {repo!r} ({error})"
                )
        if self.fork_owner and self.scm_provider in FORKLESS_PROVIDERS:
            raise ValueError(f"T2P_FORK_OWNER is not supported with {self.scm_provider}")
        return self

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
            raise ValueError(f"invalid fork owner: {value!r}")
        return value

    @field_validator("data_dir")
    @classmethod
    def _absolute_data_dir(cls, value: Path) -> Path:
        # Workspaces are handed to git and to subprocesses with other working directories.
        return value.expanduser().resolve()

    @field_validator("jira_base_url", "scm_api_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @property
    def scm_api(self) -> str:
        """The forge's API base URL: the configured one, or the provider's cloud."""
        return self.scm_api_url or DEFAULT_SCM_API.get(self.scm_provider, "")

    @property
    def workspaces_dir(self) -> Path:
        return self.data_dir / "workspaces"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "jobs.sqlite3"

    def repo_for(self, ticket_key: str) -> str | None:
        """Return the repository configured for the ticket's Jira project, if any."""
        project = ticket_key.split("-", 1)[0]
        return self.repos.get(project)


# Credentials the Claude CLI may need. They must reach it; with the sandbox, Claude Code strips
# them from the agent's commands (CLAUDE_CODE_SUBPROCESS_ENV_SCRUB), without it they are
# reachable (docs/security.md).
CLAUDE_CREDENTIAL_ENV_VARS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
)
# Claude Code on a cloud provider: it authenticates with that provider's credentials.
CLOUD_PROVIDER_FLAGS = ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX")


def enabled_cloud_providers() -> tuple[str, ...]:
    return tuple(
        flag
        for flag in CLOUD_PROVIDER_FLAGS
        if os.environ.get(flag, "").strip().lower() in ("1", "true", "yes", "on")
    )


# Egress proxy and CA settings, for the agent and for tests that have network access. A proxy
# URL can hold a password: the redactor masks it.
PROXY_ENV_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy")
NETWORK_ENV_VARS = (
    *PROXY_ENV_VARS,
    *("SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS", "REQUESTS_CA_BUNDLE"),
)
# The worker's environment reaches Claude Code and the agent's commands: everything else is
# blanked, so a credential ticket2pr does not know about (DATABASE_URL...) cannot end up in
# the diff. Kept: the basics, and Claude Code's own settings (it reads unprefixed ones too).
_AGENT_ENV_KEPT = frozenset(
    {
        *("PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LANGUAGE", "TERM", "TZ", "TMPDIR"),
        *NETWORK_ENV_VARS,
        *("UV_CACHE_DIR", "RUSTUP_HOME", "CARGO_HOME", "PYENV_ROOT"),
        *("MAX_THINKING_TOKENS", "MAX_MCP_OUTPUT_TOKENS"),
    }
)
_AGENT_ENV_KEPT_PREFIXES = ("LC_", "CLAUDE_", "ANTHROPIC_", "DISABLE_", "BASH_", "OTEL_")
# A cloud provider's credentials only when Claude Code is set to use that provider.
_PROVIDER_ENV_PREFIXES = {
    "CLAUDE_CODE_USE_BEDROCK": ("AWS_",),
    "CLAUDE_CODE_USE_VERTEX": ("CLOUD_ML_", "VERTEX_", "GOOGLE_", "GCLOUD_"),
}


def agent_env_kept(name: str) -> bool:
    """Whether the agent (Claude Code and its commands) sees this worker variable."""
    providers = enabled_cloud_providers()
    prefixes = _AGENT_ENV_KEPT_PREFIXES + tuple(
        prefix for flag in providers for prefix in _PROVIDER_ENV_PREFIXES[flag]
    )
    return name in _AGENT_ENV_KEPT or name.startswith(prefixes)


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
