.PHONY: install test lint format typecheck check

install:        ## Set up the environment (one command for a new dev)
	uv sync

test:           ## Run the test suite
	uv run pytest

lint:           ## Lint with ruff
	uv run ruff check .

format:         ## Auto-format with ruff
	uv run ruff format .

typecheck:      ## Static type-check with mypy
	uv run mypy

check: lint typecheck test  ## Run the full quality gate
