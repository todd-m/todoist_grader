PYTHON = .venv/bin/python

.PHONY: run dry-run summary today completed snapshot install test

run:
	$(PYTHON) grader.py

dry-run:
	$(PYTHON) grader.py --dry-run --summary

summary:
	$(PYTHON) grader.py --summary

today:
	$(PYTHON) grader.py --dry-run --summary --today

completed:
	$(PYTHON) grader.py --completed --project "$(PROJECT)"

snapshot:
	$(PYTHON) snapshot.py

install:
	python3 -m venv .venv
	$(PYTHON) -m pip install todoist-api-python requests rich pytest pytest-mock

test:
	$(PYTHON) -m pytest test_grader.py -v
