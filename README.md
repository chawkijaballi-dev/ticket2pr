# ticket2pr

**Turn Jira tickets into review-ready GitHub pull requests with a budget-capped, sandboxed Claude agent.**

![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)
![mypy: strict](https://img.shields.io/badge/mypy-strict-blue)
![coverage ≥ 90%](https://img.shields.io/badge/coverage-%E2%89%A590%25-brightgreen)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Add the `ticket2pr` label to a ticket. A few minutes later the ticket has a comment linking a
pull request that contains the fix, tests that pass, and a line saying exactly what it cost.

```text
PROJ-42 → succeeded
PR: https://github.com/acme/shop/pull/318
Cost $0.84 · 412,903 tokens · 23 turns
```

ticket2pr is a small, auditable harness around the
[Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk). The model writes the code;
ticket2pr decides what it may touch, how much it may spend, whether the result is
trustworthy, and what reviewers see.

## Why it is different from "an LLM in a loop"

| Concern | What ticket2pr does |
|---|---|
| **Cost** | Hard per-ticket budget (`max_budget_usd`) enforced by the SDK and shared across retries; tokens and dollars are reported on the PR, on the ticket and in the job history. |
| **Correctness** | Runs the repository's tests after the agent; a failing run gets one retry with the failure output; still red means a **draft** PR (kept in sync on re-runs; a regular PR flagged as failing when drafts are disabled with `T2P_DRAFT_ON_TEST_FAILURE=false` or unavailable on the GitHub plan), never a green one. |
| **Isolation** | Fresh clone per job; the agent's Bash *and* the repository's tests run in a bubblewrap sandbox (reads confined to the workspace, no host IPC, network allowlist / off), which **fails closed** if it cannot start; a `PreToolUse` guard; no repository settings or hooks loaded; credentials scrubbed from every environment the agent or the tests can see. |
| **CI exposure** | Optional fork mode pushes to a bot-owned fork and opens cross-repository PRs, so the target repository's CI never runs unreviewed agent code with its secrets. |
| **Supply chain** | The agent never touches what gets pushed: its tree is copied into a fresh *publish clone* (no `.git` from the agent), committed and pushed there with global config, hooks and fsmonitor disabled. Protected paths (CI, `.env`, keys) are never committed, and a change containing a configured secret is refused. |
| **Measurability** | A SWE-bench-style eval suite with hidden tests reports resolve rate and **cost per resolved ticket**. |

## How it works

```mermaid
sequenceDiagram
    participant Jira
    participant API as ticket2pr service
    participant Agent as Claude agent (sandboxed)
    participant Repo as Workspace (fresh clone)
    participant GH as GitHub

    Jira->>API: webhook (label "ticket2pr" added), HMAC-signed
    API->>API: verify signature, dedupe, persist job, enqueue
    API->>Jira: fetch ticket (REST v3)
    API->>Repo: clone base branch, create ticket2pr/<key>-<slug>
    API->>Agent: ticket + repo guidelines + test command + budget
    Agent->>Repo: read / edit / run tests (guarded tools, sandboxed Bash)
    API->>Repo: run tests in the sandbox (no secrets, timeout)
    alt tests red and budget left
        API->>Agent: retry with failing output
    end
    API->>API: fresh publish clone ← workspace tree (never its .git)
    API->>API: drop protected paths, refuse secrets, commit
    API->>GH: push branch, open or update PR (draft if not green)
    API->>Jira: comment with PR link, test status, cost
```

Code map (`src/ticket2pr/`):

| Module | Responsibility |
|---|---|
| `pipeline.py` | The orchestration above; maps every failure to a recorded job status |
| `agent/` | `CodingAgent` protocol, Claude Agent SDK runner, prompts, tool-call guard |
| `trackers/` | `Tracker` protocol; Jira Cloud (with ADF conversion) and local YAML files |
| `scm/` | Hardened async git wrapper, GitHub REST client, `RepoHost` (GitHub or local) |
| `verify.py`, `sandbox.py` | Test-command detection; bubblewrap sandbox for tests; process hardening |
| `redaction.py` | Masks configured secrets in published text, refuses them in commits |
| `server.py` / `worker.py` | FastAPI webhook + jobs API, asyncio worker pool |
| `store.py` | SQLite job history with atomic, idempotent claims |
| `evals.py` | Offline eval harness (oracle and Claude agents) |

More detail: [architecture](docs/architecture.md) · [security model](docs/security.md) ·
[configuration](docs/configuration.md) · [evals](docs/evals.md).

## Quick start

Requirements: Linux, Python 3.12+, [uv](https://docs.astral.sh/uv/), git, an Anthropic API
key, and the sandbox tools `bubblewrap` + `socat` (`apt install bubblewrap socat`). Inside an
already isolated container you can set `T2P_SANDBOX=false` instead (see the
[security model](docs/security.md#deployment-guidance)).

```bash
cd ticket2pr
make install            # uv sync + pre-commit hooks
make eval               # offline suite with the oracle agent: free, no credentials
```

### Try it on a local repository, no Jira or GitHub needed

```bash
export ANTHROPIC_API_KEY=sk-ant-...
uv run ticket2pr eval --agent claude --task paginate      # real agent, ~$1
```

or point `run` at your own directories (`<repos>/<owner>/<name>` git repos, `<tickets>/<KEY>.yaml`):

```bash
uv run ticket2pr run SHOP-101 --repo acme/shop --tickets-dir ./tickets --repos-dir ./repos
```

### Run the service

```bash
cp .env.example .env    # Jira, GitHub, Anthropic credentials + project → repo mapping
uv run ticket2pr doctor # checks the configuration
uv run ticket2pr serve  # or: docker compose up --build
```

In Jira (*Settings → System → Webhooks*), create a webhook for *Issue created* and *Issue
updated*, pointing to `https://<host>/webhooks/jira`, with the same secret as
`T2P_JIRA_WEBHOOK_SECRET`. Then add the `ticket2pr` label to a ticket.

```bash
ticket2pr jobs                     # recent jobs with cost and PR links
curl -H "Authorization: Bearer $T2P_API_TOKEN" localhost:8080/jobs
```

## Evaluation

`ticket2pr eval` runs each task in [`evals/tasks`](evals/tasks) through the real pipeline,
then grades the pushed branch with **hidden tests** the agent never saw:

```text
Task      Ticket    Status     Resolved  Cost   Tokens   Turns
envflags  OPS-7     succeeded  yes       $…     …        …
paginate  SHOP-101  succeeded  yes       $…     …        …
retry     NET-33    succeeded  yes       $…     …        …
slugify   BLOG-12   succeeded  yes       $…     …        …
Resolved 100% of 4 task(s) · total $… · per resolved ticket $…
```

The oracle agent (reference solutions) runs in CI on every commit, so the dataset and the
grader stay correct. Run `--agent claude` to measure the model; see [docs/evals.md](docs/evals.md).

## Development

```bash
make check   # ruff format + lint, mypy --strict, pytest with a 90% coverage gate
```

The test suite runs fully offline: a scripted agent, a local git "host" and YAML tickets
exercise the real pipeline end to end, including attacks such as `.git/config` rewrites and
`pre-push` hooks planted by the agent. See [CONTRIBUTING.md](CONTRIBUTING.md).

## Limitations

- One repository per Jira project; the agent sees a single repo.
- The test command is detected from common layouts (pytest, npm, go, cargo, make) or set
  with `T2P_TEST_COMMAND`, and runs with the repository's toolchain (not ticket2pr's
  virtualenv); repos without tests get draft PRs.
- The sandbox needs Linux with bubblewrap; elsewhere, run the worker in an isolated
  container with `T2P_SANDBOX=false`.
- The in-process queue suits a single replica. Several replicas need a shared queue (the
  `JobQueue.submit` seam) and a shared database.

## License

[MIT](LICENSE) © Chawki JABALLI
