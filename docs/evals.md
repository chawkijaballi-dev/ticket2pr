# Evals

A coding agent is only as good as the evidence that it works. `ticket2pr eval` measures two
numbers that matter to a team deciding whether to adopt it:

- **Resolve rate**: share of tickets whose PR passes hidden tests the agent never saw.
- **Cost per resolved ticket**: total spend divided by resolved tickets, which includes the
  cost of the failures.

## Task format

```text
evals/tasks/<name>/
├── ticket.yaml       key, title, description, type, labels (what the agent receives)
├── repo/             the repository before the change, committed as `main`
├── hidden_tests/     copied into tests/ only at grading time
└── solution/         reference files, overlaid by the oracle agent
```

Each task runs in a scratch directory through the **real pipeline**: local tracker, local
git host, the configured guardrails and the agent under test. Grading clones the pushed
branch, adds the hidden tests and runs pytest. A task is resolved only if all of them pass.

## Agents

| `--agent` | What it measures | Cost |
|---|---|---|
| `oracle` (default) | That every task is solvable and graded correctly, and that the harness works. Runs in CI. | Free |
| `claude` | The Claude agent with your `T2P_MODEL` / `T2P_EFFORT` / budget settings. | API usage |

A test also checks that a do-nothing agent resolves nothing, which guards against tasks
whose hidden tests already pass.

## Usage

```bash
uv run ticket2pr eval                                   # oracle, all tasks
uv run ticket2pr eval --agent claude --output evals/results/opus-high.json
T2P_EFFORT=medium uv run ticket2pr eval --agent claude --output evals/results/opus-medium.json
uv run ticket2pr eval --agent claude --task retry --task slugify
```

Compare effort levels or models by running the suite with different settings and diffing
the JSON reports. The exit code is non-zero unless every task is resolved, so the command can
gate a release.

## Adding a task

Prefer small, realistic tickets with an unambiguous acceptance criterion that the hidden
tests check, and make sure `repo/` contains visible tests in the same style (the agent
should be able to infer how to test). Run `make eval` and `pytest tests/test_evals.py`: the
oracle must resolve it and the do-nothing agent must not.
