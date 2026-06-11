PYTHON ?= python3
VENV   := .venv
BIN    := $(VENV)/bin

.PHONY: help venv install run dry-run summary today completed snapshot graph test lint audit ci clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  %-20s %s\n", $$1, $$2}'

venv: ## Create a virtual environment
	$(PYTHON) -m venv $(VENV)

install: venv ## Install project dependencies
	$(BIN)/pip install -r requirements.txt

run: ## Run the grader (applies labels)
	$(BIN)/python grader.py

dry-run: ## Grade without applying labels, with summary
	$(BIN)/python grader.py --dry-run --summary

summary: ## Run the grader with summary output
	$(BIN)/python grader.py --summary

today: ## Dry-run summary for tasks due today
	$(BIN)/python grader.py --dry-run --summary --today

# Usage: make completed PROJECT="ProjectName"
completed: ## Show completed tasks for PROJECT="Name"
	$(BIN)/python grader.py --completed --project "$(PROJECT)"

snapshot: ## Take a filter snapshot and re-render the graph
	$(BIN)/python snapshot.py

graph: ## Re-render the graph without fetching
	$(BIN)/python snapshot.py --graph-only

test: ## Run tests (coverage gate lives in pyproject.toml)
	$(BIN)/python -m pytest

lint: ## Run ruff lint + format check
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .

audit: ## Scan dependencies for known vulnerabilities
	$(BIN)/pip-audit -r requirements.txt

ci: test lint audit ## Run tests, lint, and security audit

clean: ## Remove virtual env and caches
	rm -rf $(VENV) __pycache__ .pytest_cache .ruff_cache .coverage
	find . -type d -name __pycache__ -exec rm -rf {} +
