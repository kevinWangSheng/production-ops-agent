#!/bin/bash
# F9 step 4 on the clean host: pinned v0.1.0 -> v0.1.1 upgrade, rollback to v0.1.0 (UPGRADE.md).
set -x
cd ~/production-ops-agent
V010=ghcr.io/kevinwangsheng/opspilot@sha256:228df9f72e6964e1d7d7110365d3b59330d7fc93ebd89e62b70631de6c7aaabd
V011=ghcr.io/kevinwangsheng/opspilot@sha256:0af7b9ff4b71e2c0b0b71d99020f35ed67f79eef958f612938f6adc102fff25e
snap() {
  echo "=== snapshot: $1 $(date -u +%FT%TZ)"
  docker ps --format "{{.Names}} {{.Image}} {{.Status}}" | grep opspilot-
  for c in web worker observer; do docker inspect opspilot-$c-1 --format "$c image={{.Config.Image}} id={{.Image}}"; docker exec opspilot-$c-1 python -c "import opspilot,importlib.metadata as m; print(\"$c version\", m.version(\"opspilot\"))" 2>&1 | tail -1; done
  ~/f9-final/q.sh <<SQL
select alembic, version_num from alembic_version;
select incident, incident_id, state, lifecycle, control_generation from opspilot_incidents order by created_at;
select run, run_id, state, epoch from opspilot_runs;
select session, session_id, state, ended_reason, adopted_count from opspilot_observation_sessions order by created_at;
select controls, count(*) from opspilot_controls;
select evidence, count(*) from opspilot_evidence;
select samples, count(*) from opspilot_observation_samples;
SQL
  curl -s -o /dev/null -w "workbench http %{http_code}\n" http://127.0.0.1:18090/healthz
}
snap "before (opspilot:local from checkout 9416041)"
echo "### A: move to pinned v0.1.0"; date -u +%FT%TZ
OPSPILOT_IMAGE=$V010 deploy/compose/install.sh upgrade; echo "rc=$?"
python3 scripts/kind_lab.py connect-control --project opspilot; echo "rc=$?"
sleep 20; snap "on v0.1.0"
echo "### B: upgrade to pinned v0.1.1"; date -u +%FT%TZ
OPSPILOT_IMAGE=$V011 deploy/compose/install.sh upgrade; echo "rc=$?"
python3 scripts/kind_lab.py connect-control --project opspilot; echo "rc=$?"
sleep 90; snap "on v0.1.1 (after 90s, Observer sampling should continue)"
echo "### C: rollback to v0.1.0 (UPGRADE.md Rolling back)"; date -u +%FT%TZ
deploy/compose/install.sh compose stop web worker observer; echo "rc=$?"
echo "step 2 check: run the downgrade to the same head; documented to exit 1 and change nothing"
HEAD_REV=$(~/f9-final/q.sh <<< "select version_num from alembic_version")
deploy/compose/install.sh compose run --rm --no-deps migrate python -m opspilot.schema downgrade --to "$HEAD_REV"; echo "downgrade rc=$? (expected 1, same head)"
OPSPILOT_IMAGE=$V010 deploy/compose/install.sh upgrade; echo "rc=$?"
python3 scripts/kind_lab.py connect-control --project opspilot; echo "rc=$?"
sleep 90; snap "rolled back to v0.1.0 (after 90s)"
echo "### done"; date -u +%FT%TZ
