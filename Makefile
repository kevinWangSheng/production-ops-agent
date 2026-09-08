.PHONY: setup doctor check test

setup:
	UV_PROJECT_ENVIRONMENT=.venv uv sync --locked --python "$${UV_PYTHON:-3.12}"

doctor:
	python3 scripts/doctor.py

check: doctor
	uv lock --check --offline --no-python-downloads
	.venv/bin/ruff check .
	.venv/bin/ruff format --check .
	.venv/bin/python -m pytest

test: doctor
	uv lock --check --offline --no-python-downloads
	.venv/bin/python -m pytest
