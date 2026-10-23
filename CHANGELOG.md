# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed

- Branches are named `ticket2pr/<key in lower case>` (no title slug), so a re-run after a title edit
  updates the existing PR.
- `registry.npmjs.org` is no longer in the default `T2P_NETWORK_ALLOWLIST` (it accepts
  `npm publish`); add it explicitly for JavaScript repositories.
- Test runs judge the shell's exit code and kill whatever the suite left running; without
  the sandbox, each run gets a fresh `HOME` and `TMPDIR`. `RUSTUP_HOME`, `CARGO_HOME` and
  `PYENV_ROOT` are passed through so version-managed toolchains keep working.
- Jobs fail on file names that are not valid UTF-8.
- An interrupted agent attempt counts its whole remaining budget; `ticket2pr run` records
  SIGTERM as an interruption; `serve` deletes leftover workspaces at startup, except those kept with `--keep-workspace`
  (by this version: older kept workspaces have no `.keep` marker and are removed);
  git aborts a clone or push stalled under 1 KB/s for 60 s.
- A gracefully interrupted job tells the ticket to re-add the label.
- A re-run refuses to force-push over a PR branch whose head commit has another author email
  than `T2P_COMMIT_AUTHOR_EMAIL` (a reviewer's fixups are never discarded).
- `T2P_SANDBOX_READONLY_PATHS` expands `~`.
- Background Bash commands are refused, and Claude Code's background tasks disabled (no
  auto-backgrounding of timed-out commands): they could race the file tools, which run outside
  the OS sandbox and are confined by the guard alone (now documented).
- The file-count limit no longer reports the agent's exact count on the ticket.
- Read-only toolchain paths that contain a hidden directory (such as `$HOME`) are not
  exposed to the sandboxes.
- npm's default "no test specified" script is not taken for a test suite.
- Auto-detected Python tests run `python3 -m pytest -q`; sandboxed tests get `TMPDIR=/tmp`.
- Sockets, FIFOs and device files left in the workspace are not copied to the publish clone.

### Security

- Literal git pathspecs, so protected paths such as `:x/.env` are never committed.
- The guard expands `~/` before confining file tools to the workspace.
- The staged diff is read with a 20 MiB bound before the in-memory secret scan.
- A job fails closed when a protected path is still staged after unstaging.
- The `PreToolUse` hook denies a call when the guard itself raises.
- Secret variables are hidden from the agent whatever the case of their names.
- Images and raw HTML in the agent's summary are escaped in PR bodies; errors in ticket
  comments are a single code span.
- Each job gets its own Claude Code temp directory instead of the shared `/tmp/claude`.
- Jobs re-enqueued at startup are failed when their ticket key is invalid.
- Ticket titles are escaped like agent text, and `[]` links (including reference
  definitions) and fenced blocks in agent text are escaped too. The agent's summary is the
  last section of the PR body.
- Ticket comments carry no agent text or git/OS error output, only outcomes ticket2pr
  words itself.
- `CLAUDE_CONFIG_DIR` (where the `claude` login lives) is hidden from the agent and tests.
- The Docker image's `/app` is root-owned.

## [0.1.0] - 2026-10-22

### Added

- Ticket → pull request pipeline: clone, Claude agent run, tests, one feedback retry,
  commit, push, PR (draft when not green) and a ticket comment with cost and tokens.
- Claude Agent SDK runner with a per-ticket dollar budget shared across retries, a turn cap,
  a timeout, a tool allowlist, a Bash OS sandbox with a network allowlist and a
  `PreToolUse` guard.
- Hardened git handling: credentials outside argv and config files, hooks disabled,
  protected paths never committed.
- Jira Cloud tracker (REST v3, ADF conversion) and a local YAML tracker; GitHub and local
  repository hosts.
- FastAPI service: HMAC-verified Jira webhook triggered when the label is added, idempotent
  job claims, worker pool, jobs API behind a bearer token.
- CLI: `run`, `serve`, `jobs`, `eval`, `doctor`.
- SWE-bench-style eval harness with hidden tests, an oracle agent and four tasks.
- bubblewrap sandbox for the repository's tests, and a fail-closed check of the sandbox
  dependencies (`bwrap`, `socat`) before each job.
- Publish clone: commits and pushes never use the agent's `.git`; git ignores global config
  and fsmonitor.
- Secret redaction in published text and refusal of changes that contain a secret.
- PR draft state kept in sync on re-runs (GraphQL), and ticket comments that state it.
- Fork mode (`T2P_FORK_OWNER`): cross-repository PRs keep the target CI's secrets away from
  unreviewed agent code.
- Agent reads confined to the workspace; the CLI refuses to run Bash unsandboxed.
- Unsandboxed (container) mode: one job at a time, leftover processes killed before
  publishing, queued jobs resumed after a restart.
