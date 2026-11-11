#!/bin/bash
# F9 step 4 round 2: staging first (v0.1.0 -> v0.1.1 by digest), then main, then main rollback per UPGRADE.md.
cd ~/production-ops-agent
E=~/f9-final2
V010=ghcr.io/kevinwangsheng/opspilot@sha256:228df9f72e6964e1d7d7110365d3b59330d7fc93ebd89e62b70631de6c7aaabd
V011=ghcr.io/kevinwangsheng/opspilot@sha256:0af7b9ff4b71e2c0b0b71d99020f35ed67f79eef958f612938f6adc102fff25e
STG="OPSPILOT_PROJECT=opspilot-staging OPSPILOT_SECRETS_DIR=$HOME/.config/opspilot-staging"
savelogs() { for s in web worker observer; do docker logs "$1-$s-1" > "$E/logs-$2-$1-$s.log" 2>&1; done; echo "saved container logs: $2 $1"; }
ts() { echo "### $* at $(date -u +%FT%TZ)"; }

ts "S1 save staging v0.1.0 logs"; savelogs opspilot-staging 51-before-upgrade-v010
ts "S2 staging upgrade to v0.1.1 digest"
( set -x; env $STG OPSPILOT_IMAGE=$V011 deploy/compose/install.sh upgrade ); echo "rc=$?"
ts "S3 staging re-join kind network without aliases (undocumented, see 12-staging-network.log)"
for s in web worker observer; do docker network connect kind opspilot-staging-$s-1; echo "$s rc=$?"; done
sleep 150
ts "S4 staging snapshot on v0.1.1 (150 s after upgrade, 2 Observer intervals)"
$E/snap.sh opspilot-staging after-upgrade-v011 > $E/52-snap-staging-after-upgrade.txt 2>&1; echo "snap rc=$?"
grep -E "readyz|alembic|^session|count_samples|last_sample" $E/52-snap-staging-after-upgrade.txt

ts "M1 main snapshot + logs before upgrade"
$E/snap.sh opspilot main-before-upgrade-v010 > $E/53-snap-main-before-upgrade.txt 2>&1; savelogs opspilot 53-before-upgrade-v010
ts "M2 main upgrade to v0.1.1 digest"
( set -x; OPSPILOT_IMAGE=$V011 deploy/compose/install.sh upgrade ); echo "rc=$?"
( set -x; python3 scripts/kind_lab.py connect-control --project opspilot ); echo "rc=$?"
sleep 60
ts "M3 main snapshot on v0.1.1"
$E/snap.sh opspilot main-after-upgrade-v011 > $E/54-snap-main-after-upgrade.txt 2>&1; echo "snap rc=$?"
grep -E "readyz|alembic|^incident|count_" $E/54-snap-main-after-upgrade.txt
ts "M4 save main v0.1.1 logs before rollback"; savelogs opspilot 55-on-v011

ts "R1 rollback main per UPGRADE.md: stop runtimes"
( set -x; deploy/compose/install.sh compose stop web worker observer ); echo "rc=$?"
ts "R2 schema heads: same revision, so UPGRADE.md says skip the downgrade (not run this time; run once in round 1 with rc=1)"
docker exec -i opspilot-postgres-1 sh -c 'psql -U "$POSTGRES_USER" -d "${POSTGRES_DB:-$POSTGRES_USER}" -At' <<'SQL'
select 'alembic current', version_num from alembic_version;
SQL
ts "R3 upgrade main with the v0.1.0 digest"
( set -x; OPSPILOT_IMAGE=$V010 deploy/compose/install.sh upgrade ); echo "rc=$?"
( set -x; python3 scripts/kind_lab.py connect-control --project opspilot ); echo "rc=$?"
sleep 60
ts "R4 main snapshot after rollback"
$E/snap.sh opspilot main-after-rollback-v010 > $E/56-snap-main-after-rollback.txt 2>&1; echo "snap rc=$?"
grep -E "readyz|alembic|^incident|count_" $E/56-snap-main-after-rollback.txt
ts "S5 staging snapshot again (still v0.1.1, in-flight session)"
$E/snap.sh opspilot-staging staging-later > $E/57-snap-staging-later.txt 2>&1
grep -E "readyz|^session|count_samples|last_sample" $E/57-snap-staging-later.txt
ts "done"
