#!/usr/bin/env bash
# Install the pinned upstream HolmesGPT harness for #668 (Linux rental host).
# Usage: scripts/holmes_compare/install_upstream.sh [DEST]   (default tmp/holmes-compare/upstream)
# Env: HOLMES_PYTHON_BIN (default python3.12; upstream needs >=3.10,<3.14).
# Idempotent. Touches nothing outside DEST. Prints no secrets (none are read).
set -euo pipefail
COMMIT=a0009195468ebc55a7dff7d602b61feabe3cda1b
REPO_URL=https://github.com/HolmesGPT/holmesgpt.git
POETRY_VERSION=1.8.5   # upstream CLAUDE.md: the lockfile is Poetry 1.8.x
DEST=${1:-tmp/holmes-compare/upstream}
PY=${HOLMES_PYTHON_BIN:-python3.12}

if [ ! -d "$DEST/.git" ]; then
  mkdir -p "$DEST"
  git -C "$DEST" init -q
  git -C "$DEST" remote add origin "$REPO_URL"
fi
if [ "$(git -C "$DEST" rev-parse -q --verify HEAD 2>/dev/null || true)" != "$COMMIT" ]; then
  git -C "$DEST" fetch -q --depth 1 origin "$COMMIT"
  git -C "$DEST" checkout -q --detach "$COMMIT"
fi
test "$(git -C "$DEST" rev-parse HEAD)" = "$COMMIT"
if [ -n "$(git -C "$DEST" status --porcelain --untracked-files=no)" ]; then
  echo '{"status":"error","code":"UPSTREAM_MODIFIED"}'; exit 1
fi
if [ -e "$DEST/.env" ]; then
  echo '{"status":"error","code":"UPSTREAM_DOTENV_PRESENT"}'; exit 1
fi
if [ ! -x "$DEST/.tools/bin/poetry" ]; then
  "$PY" -m venv "$DEST/.tools"
  "$DEST/.tools/bin/pip" install -q "poetry==$POETRY_VERSION"
fi
(cd "$DEST" && POETRY_VIRTUALENVS_IN_PROJECT=true "$DEST/.tools/bin/poetry" env use "$PY" >/dev/null \
  && POETRY_VIRTUALENVS_IN_PROJECT=true "$DEST/.tools/bin/poetry" install --with dev --no-interaction -q)
# Collection-only check: no setup, no model call (RUN_LIVE=false, --collect-only).
(cd "$DEST" && RUN_LIVE=false "$DEST/.venv/bin/python" -m pytest tests/llm/test_ask_holmes.py \
  --collect-only -q --no-cov -n 0 -p no:cacheprovider >/dev/null)
echo "{\"status\":\"ok\",\"upstream\":\"$DEST\",\"python\":\"$DEST/.venv/bin/python\"}"
