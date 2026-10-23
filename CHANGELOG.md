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
- A re-run refuses, before the agent runs, a PR branch whose head commit has another author
  or committer email than `T2P_COMMIT_AUTHOR_EMAIL` (a reviewer's fixups or amends are never
  discarded); the push itself uses `--force-with-lease`, so a concurrent push is not lost.
- `T2P_SANDBOX_READONLY_PATHS` expands `~`.
- Empty mount-point files Claude Code's sandbox leaves (`package.json`, `.npmrc`,
  `.gitmodules`...) are removed before publishing.
- The sandbox check also starts bwrap (a success is remembered for the process, a failure
  retried at the next job, so fixing the host needs no restart): installed but unable to create
  namespaces (an unprivileged container) now fails the job before any spend instead of
  failing every test run, and `doctor` says what is missing.
- With `T2P_TEST_NETWORK=true`, tests can resolve names on systemd-resolved hosts
  (`/etc/resolv.conf` points into the emptied `/run`); the bwrap probe uses the tests' own profile.
- Fork mode publishes from a full clone, so a fork far behind the target can still be pushed to.
- The agent's tool set is exactly the allowlist (no `Monitor`, cron, remote triggers or
  ToolSearch), with no MCP server; the guard denies any other tool. Ticket comments show
  the cost but no longer tokens or turns.
- Background Bash commands are refused, and Claude Code's background tasks disabled (no
  auto-backgrounding of timed-out commands): they could race the file tools, which run outside
  the OS sandbox and are confined by the guard alone (now documented).
- The file-count limit no longer reports the agent's exact count on the ticket.
- Read-only toolchain paths that contain a hidden directory (such as `$HOME`) are not
  exposed to the sandboxes.
- npm's default "no test specified" script is not taken for a test suite.
- Auto-detected Python tests run `python3 -m pytest -q`; sandboxed tests get `TMPDIR=/tmp`.
- Sockets, FIFOs and device files left in the workspace are not copied to the publish clone.
- An unexpected (non-SDK) agent error, or a timeout after the run's result arrived, no longer
  loses the attempt's spend or an earlier attempt's draft PR.
- A change that only touches protected paths fails as such, not as "no change"; `run
  --repos-dir` ignores `T2P_FORK_OWNER` (local repositories have no forks); the eval report's
  `duration_s` is renamed `agent_duration_s` (it never counted cloning, tests or grading).
- Local ticket files with a non-list `labels` are refused (a bare string was split into
  letters); eval tasks without `solution/` are refused at discovery.
- Job workspaces are deleted even when a run left read-only directories (such as Go's module
  cache) in them.
- The PR's usage table shows cache-write tokens, so its counts add up to the job history's.
- The PR body's guardrail events are capped (20 lines), so a long list cannot exceed
  GitHub's body limit after the push.

### Security

- The mask for secrets is `REDACTED` (no brackets, which could rebuild an image link from
  the agent's escaped text), and agent text is masked before escaping too, which could split
  a secret containing `#`, `@` or `[`. The commit message and PR title neutralise `#123` and
  `@user` references like the body.
- A configured secret in the ticket title is masked in the commit message and PR title too.
- The agent's Bash sandbox also denies `/run/user` and `/run/dbus` (host IPC sockets), as the
  test sandbox already did.
- Without the sandbox, leftover processes (including a "zombie" whose other threads still run)
  are frozen then killed until none is left, never blocking past the deadline; a job
  refuses to publish if any survives. The eval harness ignores the developer's git config.
- ticket2pr's git ignores the per-user excludes file (`~/.config/git/ignore`).
- Literal git pathspecs, so protected paths such as `:x/.env` are never committed.
- The guard expands `~/` before confining file tools to the workspace.
- The staged diff is read with a 20 MiB bound before the in-memory secret scan.
- A job fails closed when a protected path is still staged after unstaging.
- The `PreToolUse` hook denies a call when the guard itself raises.
- Secret variables are hidden from the agent whatever the case of their names.
- Images and raw HTML in the agent's summary are escaped in PR bodies; errors in ticket
  comments are a single code span.
- Each job gets its own Claude Code temp directory; the shared `/tmp/claude` and
  `/tmp/claude-<uid>` are denied.
- The image no longer ships an exec-based `HEALTHCHECK` (it exposed every secret to
  unsandboxed tests); probe `/healthz` over HTTP.
- Without the sandbox, the agent's Bash works again (Claude Code's env scrub, which demands
  its sandbox, is only set with it).
- Jobs re-enqueued at startup are failed when their ticket key is invalid.
- Eval reports mark capped costs (a timed-out task counts its budget) as upper bounds.
- Webhook replays are refused: a triggering body already received, or an event older than an
  hour.
- The agent's workspace is no longer a git repository, and sandboxed tests cannot create one:
  Claude Code runs git there itself, outside every sandbox, and would execute planted config.
- Claude Code's git never treats the workspace root as a bare repository nor climbs into an
  enclosing checkout.
- @mentions, #references and closing keywords in agent text no longer notify or close anything.
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
