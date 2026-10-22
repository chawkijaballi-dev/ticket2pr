# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

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
