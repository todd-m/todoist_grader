.PHONY: run dry-run summary install

run:
	python grader.py

dry-run:
	python grader.py --dry-run --summary

summary:
	python grader.py --summary

install:
	pip install todoist-api-python requests rich
