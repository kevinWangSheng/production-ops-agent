.PHONY: setup doctor check test acceptance red-proof

setup:
	UV_PROJECT_ENVIRONMENT=.venv uv sync --locked --python "$${UV_PYTHON:-3.12}"

doctor:
	python3 scripts/doctor.py

check: doctor
	uv lock --check --offline --no-python-downloads
	.venv/bin/ruff check .
	.venv/bin/ruff format --check .
	.venv/bin/mypy
	.venv/bin/python -m pytest

test: doctor
	uv lock --check --offline --no-python-downloads
	.venv/bin/python -m pytest

acceptance:
	.venv/bin/python scripts/m1_acceptance.py
	test -x tmp/gitleaks/gitleaks || python3 scripts/install_gitleaks.py --directory tmp/gitleaks
	python3 scripts/check_secrets.py --binary tmp/gitleaks/gitleaks

red-proof:
	.venv/bin/python scripts/check_red_proof.py --base origin/main
