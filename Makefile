.PHONY: setup doctor check test migrate retention retention-apply acceptance red-proof eval-structural secrets-branch hooks

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
# 旧库只差表内列顺序时：make migrate MIGRATE_FLAGS=--accept-column-order（接受的 diff 要记进任务记录）。
MIGRATE_FLAGS ?=
migrate:
	.venv/bin/python -m opspilot.schema migrate $(MIGRATE_FLAGS)

# 原始资料保留期清理（#244）：以属于 opspilot_retention 的 login 连接，
# 需要 OPSPILOT_RETENTION_DSN；先 dry-run 复核，再显式 apply。DAYS 默认 30，不得更小。
DAYS ?= 30
retention:
	.venv/bin/python -m opspilot.retention --days $(DAYS)

retention-apply:
	.venv/bin/python -m opspilot.retention --apply --days $(DAYS)

acceptance:
	.venv/bin/python scripts/m1_acceptance.py
	test -x tmp/gitleaks/gitleaks || python3 scripts/install_gitleaks.py --directory tmp/gitleaks
	python3 scripts/check_secrets.py --binary tmp/gitleaks/gitleaks

# 只扫本分支相对 origin/main 新增的提交（CI 扫全部分支历史，误报一旦推送只能重建分支）。
secrets-branch:
	sh scripts/branch_secrets.sh

# 把 pre-push 设为推送前执行 secrets-branch（对所有 worktree 生效）。
hooks:
	git config core.hooksPath scripts/git-hooks

red-proof:
	.venv/bin/python scripts/check_red_proof.py --base origin/main

# 零模型、零网络：重放入库的合成 CANARY fixture（PR-D1）。
eval-structural:
	.venv/bin/python scripts/eval_structural_ci.py
