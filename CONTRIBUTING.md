# Contributing

Thanks for helping. This project values small, well-tested changes.

## Setup

```bash
make install   # uv sync + pre-commit hooks
make check     # ruff format/lint, mypy --strict, pytest with a 90% coverage gate
make eval      # offline eval suite (oracle agent)
```

## Ground rules

- **Types:** `mypy --strict` must pass, tests included. Prefer frozen Pydantic models or
  frozen dataclasses for data, `Protocol`s for seams.
- **Tests:** every behaviour change comes with a test. The suite is offline: use the
  fixtures in `tests/conftest.py` (`settings`, `repos_dir`, `tickets_dir`, `store`,
  `make_pipeline`) and its `ScriptedAgent` rather than mocks of internals. Network calls are mocked with `respx`.
- **Errors:** raise a `Ticket2PRError` subclass for anything a user can cause or fix; the
  pipeline turns those into job statuses.
- **Security:** changes to `agent/guard.py`, `scm/git.py`, `verify.py` or the agent options
  need a test showing the attack they prevent, and an update to `docs/security.md`.
- **Configuration:** new settings get a `description`; regenerate the docs with
  `uv run python scripts/gen_config_docs.py`.
- **Commits:** [Conventional Commits](https://www.conventionalcommits.org/)
  (`feat:`, `fix:`, `docs:`, `test:`, `refactor:`, `chore:`).

## Adding an eval task

See [docs/evals.md](docs/evals.md#adding-a-task).
