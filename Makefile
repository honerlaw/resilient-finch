.PHONY: check lint typecheck fmt

check: lint typecheck

lint:
	uv run ruff check .
	uv run ruff format --check .

typecheck:
	uv run ty check .

fmt:
	uv run ruff format .
	uv run ruff check --fix .
