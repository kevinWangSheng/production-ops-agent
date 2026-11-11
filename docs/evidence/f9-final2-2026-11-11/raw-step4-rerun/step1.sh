#!/bin/bash
# F9 final3 step 1: roll staging back from the v0.1.1 digest to the v0.1.0 digest per deploy/compose/UPGRADE.md "Rolling back".
cd ~/production-ops-agent
E=~/f9-final3
V010=ghcr.io/kevinwangsheng/opspilot@sha256:228df9f72e6964e1d7d7110365d3b59330d7fc93ebd89e62b70631de6c7aaabd
STG="OPSPILOT_PROJECT=opspilot-staging OPSPILOT_SECRETS_DIR=$HOME/.config/opspilot-staging"
ts() { echo "### $* at $(date -u +%FT%TZ)"; }
ts "R0 save staging v0.1.1 container logs before rollback"
for s in web worker observer; do docker logs "opspilot-staging-$s-1" > "$E/logs-10-before-rollback-v011-opspilot-staging-$s.log" 2>&1; done; echo saved
ts "R1 stop every staging runtime (UPGRADE.md Rolling back step 1)"
( set -x; env $STG deploy/compose/install.sh compose stop web worker observer ); echo "rc=$?"
ts "R2 schema head: v0.1.1 upgrade reported 'schema unchanged: 0024_trace_outbox', v0.1.0 head is 0024_trace_outbox; same revision, so step 2 (downgrade) is skipped as UPGRADE.md says"
~/f9-final3/q.sh opspilot-staging <<< "select 'alembic current', version_num from alembic_version;"
ts "R3 upgrade staging with the v0.1.0 digest (UPGRADE.md Rolling back step 3)"
( set -x; env $STG OPSPILOT_IMAGE=$V010 deploy/compose/install.sh upgrade ); echo "rc=$?"
ts "R4 staging re-join kind network without aliases (undocumented, same as f9-final2 12-staging-network.log / step4b.sh S3)"
for s in web worker observer; do docker network connect kind opspilot-staging-$s-1; echo "$s rc=$?"; done
sleep 60
ts "R5 staging snapshot on v0.1.0"
$E/snap.sh opspilot-staging staging-after-rollback-v010 > $E/11-snap-staging-after-rollback.txt 2>&1; echo "snap rc=$?"
grep -E "config_image|readyz|alembic|^incident|^session|count_" $E/11-snap-staging-after-rollback.txt
ts done
