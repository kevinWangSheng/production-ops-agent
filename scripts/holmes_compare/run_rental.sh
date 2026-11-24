#!/usr/bin/env bash
# One command for the #668 Holmes side on the rental host (#669).
# Usage: scripts/holmes_compare/run_rental.sh ROUND ENV_FILE [runner args...]
#   ENV_FILE: DEEPSEEK_API_KEY, ELASTICSEARCH_URL (http://localhost:PORT), ELASTICSEARCH_API_KEY
#             (engineer key), ZAI_API_KEY or GLM_API_KEY. Read by the runner only, never printed.
#   judge path: default opspilot (lead ruling); add --judge-path upstream for the optional comparison.
#   extra args, e.g. --questions 254,275 --trials 1 --kubeconfig ~/.kube/config
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
ROUND=$1; ENV_FILE=$2; shift 2
UP=tmp/holmes-compare/upstream
scripts/holmes_compare/install_upstream.sh "$UP"
.venv/bin/python -m scripts.holmes_compare.runner round --round "$ROUND" --upstream "$UP" \
  --holmes-python "$UP/.venv/bin/python" --env-file "$ENV_FILE" "$@"
# scores reads the round's own evidence and questions (default B1 / docs/evidence)
SCORE_ARGS=()
while (($#)); do
  case $1 in
    --evidence-root|--questions) SCORE_ARGS+=("$1" "$2"); shift 2 ;;
    --evidence-root=*|--questions=*) SCORE_ARGS+=("${1%%=*}" "${1#*=}"); shift ;;
    *) shift ;;
  esac
done
.venv/bin/python -m scripts.holmes_compare.runner scores --round "$ROUND" ${SCORE_ARGS[@]+"${SCORE_ARGS[@]}"}
