PYTHON = .venv/bin/python

.PHONY: run dry-run summary install test

run:
	$(PYTHON) grader.py

dry-run:
	$(PYTHON) grader.py --dry-run --summary

summary:
	$(PYTHON) grader.py --summary

install:
	python3 -m venv .venv
	$(PYTHON) -m pip install todoist-api-python requests rich pytest pytest-mock

test:
	$(PYTHON) -m pytest test_grader.py -v
