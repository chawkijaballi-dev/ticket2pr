.DEFAULT_GOAL := help
.PHONY: help install dev-tools fmt lint typecheck test cov eval check docker

help: ## Show this help
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-10s %s\n", $$1, $$2}'

install: ## Install dependencies and git hooks
	uv sync
	uv run pre-commit install

dev-tools: ## Optional: token-saving tools for your own Claude Code (claude-mem, rtk, caveman, ponytail, pxpipe, graphify)
	bash scripts/dev-tools.sh

fmt: ## Format the code
	uv run ruff format src tests scripts
	uv run ruff check --fix src tests scripts

lint: ## Lint without modifying files
	uv run ruff format --check src tests scripts
	uv run ruff check src tests scripts

typecheck: ## Static type checking (strict)
	uv run mypy

test: ## Run the test suite
	uv run pytest

cov: ## Run tests with coverage (fails under 90%)
	uv run pytest --cov --cov-report=term-missing

eval: ## Run the offline eval suite with the oracle agent (free)
	uv run ticket2pr eval --agent oracle

check: lint typecheck cov ## Lint, types and tests (CI also runs the eval and the image build)

docker: ## Build the worker image
	docker build -t ticket2pr:dev .
