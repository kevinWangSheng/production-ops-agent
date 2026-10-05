.PHONY: setup doctor check test migrate acceptance red-proof

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

# 迁移由拥有 DDL 权限的 owner 连接在启动前执行；运行时 install() 只校验版本。
# 需要 OPSPILOT_DSN；旧库接管还需与服务端同大版本的 pg_dump（OPSPILOT_PG_DUMP）。
migrate:
	.venv/bin/python -m opspilot.schema migrate

acceptance:
	.venv/bin/python scripts/m1_acceptance.py
	test -x tmp/gitleaks/gitleaks || python3 scripts/install_gitleaks.py --directory tmp/gitleaks
	python3 scripts/check_secrets.py --binary tmp/gitleaks/gitleaks

red-proof:
	.venv/bin/python scripts/check_red_proof.py --base origin/main
