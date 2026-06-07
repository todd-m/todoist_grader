PYTHON = .venv/bin/python

.PHONY: run dry-run summary today completed snapshot graph install test

run:
	$(PYTHON) grader.py

dry-run:
	$(PYTHON) grader.py --dry-run --summary

summary:
	$(PYTHON) grader.py --summary

today:
	$(PYTHON) grader.py --dry-run --summary --today

# Usage: make completed PROJECT="ProjectName"
completed:
	$(PYTHON) grader.py --completed --project "$(PROJECT)"

snapshot:
	$(PYTHON) snapshot.py

graph:
	$(PYTHON) snapshot.py --graph-only

install:
	python3 -m venv .venv
	$(PYTHON) -m pip install todoist-api-python requests rich pytest pytest-mock

test:
	$(PYTHON) -m pytest test_grader.py test_snapshot.py test_todoist_api.py -v
