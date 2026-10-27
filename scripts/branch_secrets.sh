#!/bin/sh
# Scan only the commits a branch adds on top of origin/main, with the same
# pinned gitleaks as CI. CI scans every fetched branch's history, so a hit
# that reaches the remote fails every PR until the branch is rebuilt.
# Usage: scripts/branch_secrets.sh [<rev>...]   (default: HEAD)
set -eu
common=$(git rev-parse --path-format=absolute --git-common-dir)
dir="$common/opspilot-gitleaks"
if [ ! -x "$dir/gitleaks" ]; then
	python3 scripts/install_gitleaks.py --directory "$dir" >&2
fi
[ "$#" -gt 0 ] || set -- HEAD
status=0
for rev in "$@"; do
	# Same rules and reviewed exceptions as CI (scripts/check_secrets.py).
	result=$(python3 scripts/check_secrets.py --binary "$dir/gitleaks" --range "origin/main..$rev") || true
	if [ "$result" != "BRANCH_SECRET_SCAN_PASSED" ]; then
		echo "$result in origin/main..$rev; inspect: $dir/gitleaks git --redact --ignore-gitleaks-allow --log-opts=origin/main..$rev ." >&2
		status=1
	fi
done
[ "$status" -eq 0 ] && echo "BRANCH_SECRETS_CLEAN"
exit "$status"
