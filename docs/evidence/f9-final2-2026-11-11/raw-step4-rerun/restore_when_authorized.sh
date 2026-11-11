#!/bin/bash
# Wait (max 25 min) until staging has an authorized observation session on incident $1, then restore the fault once.
cd ~/production-ops-agent
INC="$1"; end=$(( $(date +%s) + 1500 ))
while [ "$(date +%s)" -lt "$end" ]; do
  row=$(~/f9-final3/q.sh opspilot-staging <<< "select session_id, state, authorization_source, authorized_by, created_at from opspilot_observation_sessions where incident_id='$INC' and state='authorized'")
  if [ -n "$row" ]; then
    echo "$(date -u +%FT%TZ) staging session authorized: $row"
    ~/f9-final3/q.sh opspilot-staging <<< "select 'run', run_id, state, budget_spent, tool_operations_used from opspilot_runs where incident_id='$INC'; select 'decision', * from opspilot_self_heal_authorizations where incident_id='$INC';"
    echo "### fault restore at $(date -u +%FT%TZ)"
    python3 scripts/kind_lab.py fault restore --experiment-id f9-final3-step4; echo "rc=$?"
    echo "### done at $(date -u +%FT%TZ) -- no further fault injection until the drill ends"
    ~/f9-final3/flag.sh
    exit 0
  fi
  sleep 5
done
echo "$(date -u +%FT%TZ) timeout: no authorized staging session"; exit 1
