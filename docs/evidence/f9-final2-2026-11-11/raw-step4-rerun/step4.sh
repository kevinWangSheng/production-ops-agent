#!/bin/bash
# F9 final3 step 4: with staging observation $1 in flight (authorized, >=1 sample), upgrade staging v0.1.0 -> v0.1.1 by digest per UPGRADE.md.
cd ~/production-ops-agent
E=~/f9-final3; SID="$1"
V011=ghcr.io/kevinwangsheng/opspilot@sha256:0af7b9ff4b71e2c0b0b71d99020f35ed67f79eef958f612938f6adc102fff25e
STG="OPSPILOT_PROJECT=opspilot-staging OPSPILOT_SECRETS_DIR=$HOME/.config/opspilot-staging"
ts() { echo "### $* at $(date -u +%FT%TZ)"; }
sess() { ~/f9-final3/q.sh opspilot-staging <<< "select 'session', session_id, state, ended_reason, healthy_since, adopted_count, updated_at from opspilot_observation_sessions where session_id='$SID'; select 'sample', sequence, outcome, window_end from opspilot_observation_samples where session_id='$SID' order by sequence;"; }
ts "U0 in-flight check before upgrade"; sess
ts "U1 staging snapshot before upgrade (v0.1.0)"
$E/snap.sh opspilot-staging staging-before-upgrade-v010 > $E/40-snap-staging-before-upgrade.txt 2>&1; echo "snap rc=$?"
ts "U2 save staging v0.1.0 container logs"
for s in web worker observer; do docker logs "opspilot-staging-$s-1" > "$E/logs-41-before-upgrade-v010-opspilot-staging-$s.log" 2>&1; done; echo saved
ts "U3 staging upgrade to v0.1.1 digest (UPGRADE.md)"
( set -x; env $STG OPSPILOT_IMAGE=$V011 deploy/compose/install.sh upgrade ); echo "rc=$?"
ts "U4 staging re-join kind network without aliases (undocumented, same as f9-final2)"
for s in web worker observer; do docker network connect kind opspilot-staging-$s-1; echo "$s rc=$?"; done
ts "U5 in-flight check right after the switch"; sess
sleep 90
ts "U6 staging snapshot on v0.1.1 (90 s after upgrade)"
$E/snap.sh opspilot-staging staging-after-upgrade-v011 > $E/43-snap-staging-after-upgrade.txt 2>&1; echo "snap rc=$?"
grep -E "config_image|readyz|alembic|^session|count_|last_sample" $E/43-snap-staging-after-upgrade.txt
ts "U7 in-flight check on v0.1.1"; sess
ts done
