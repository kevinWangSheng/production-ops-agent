#!/usr/bin/env bash
# One command for the #668 Holmes side on the rental host (#669).
# Usage: scripts/holmes_compare/run_rental.sh ROUND ENV_FILE JUDGE_PATH [runner args...]
#   ENV_FILE: DEEPSEEK_API_KEY, ELASTICSEARCH_URL (http://localhost:PORT), ELASTICSEARCH_API_KEY
#             (engineer key), ZAI_API_KEY or GLM_API_KEY. Read by the runner only, never printed.
#   JUDGE_PATH: opspilot | upstream (lead decision, task record 2026-11-19-holmes-compare-668).
#   extra args, e.g. --questions 254,275 --trials 1 --kubeconfig ~/.kube/config
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
ROUND=$1; ENV_FILE=$2; JUDGE_PATH=$3; shift 3
UP=tmp/holmes-compare/upstream
scripts/holmes_compare/install_upstream.sh "$UP"
.venv/bin/python -m scripts.holmes_compare.runner round --round "$ROUND" --upstream "$UP" \
  --holmes-python "$UP/.venv/bin/python" --env-file "$ENV_FILE" --judge-path "$JUDGE_PATH" "$@"
.venv/bin/python -m scripts.holmes_compare.runner scores --round "$ROUND"
