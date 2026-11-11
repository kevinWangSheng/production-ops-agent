#!/bin/bash
# F9 final3 step 5/6: read-only export after the in-flight session ended.
E=~/f9-final3; SID=27c61712-da7f-5afb-a127-56e64eab168c; INC=c030a3a3-0369-5ba3-a7fd-d44066a31286
for P in opspilot-staging opspilot; do
  tag=$([ $P = opspilot ] && echo main || echo staging)
  { echo "=== $P session $SID and samples, exported at $(date -u +%FT%TZ)"
    ~/f9-final3/q.sh $P <<SQL
\pset fieldsep ' | '
select 'columns', string_agg(column_name, ',' order by ordinal_position) from information_schema.columns where table_name='opspilot_observation_sessions';
select 'session', o.* from opspilot_observation_sessions o where session_id='$SID';
select 'sample_columns', string_agg(column_name, ',' order by ordinal_position) from information_schema.columns where table_name='opspilot_observation_samples';
select 'sample', sequence, outcome, window_start, window_end from opspilot_observation_samples where session_id='$SID' order by sequence;
select 'sample_full', s.* from opspilot_observation_samples s where session_id='$SID' order by sequence;
select 'incident', incident_id, state, lifecycle, mode, control_generation, created_at, closed_at, related_incident_id from opspilot_incidents where incident_id='$INC';
select 'run', run_id, state, budget_spent, tool_operations_used from opspilot_runs where incident_id='$INC';
select 'auto_decision', a.* from opspilot_self_heal_authorizations a where incident_id='$INC';
select 'control', c.* from opspilot_controls c where incident_id='$INC';
select 'delivery', received_at, status, outcome, incident_id from opspilot_alert_deliveries where received_at > '2026-11-11T20:00:00Z' order by received_at;
select 'postmortem_version', postmortem_id, version, created_at from opspilot_postmortem_versions where created_at > '2026-11-11T20:00:00Z' order by created_at;
SQL
  } > $E/50-$tag-session-samples.txt 2>&1
  $E/snap.sh $P $tag-final > $E/51-snap-$tag-final.txt 2>&1
done
for s in web worker observer; do docker logs opspilot-staging-$s-1 > $E/logs-52-on-v011-opspilot-staging-$s.log 2>&1; done
for s in web worker observer; do docker logs --since 2026-11-11T20:00:00Z opspilot-$s-1 > $E/logs-53-main-since-2000Z-opspilot-$s.log 2>&1; done
~/f9-final3/flag.sh > $E/54-final-flag-and-state.txt 2>&1; ~/f9-final3/state.sh >> $E/54-final-flag-and-state.txt 2>&1
echo done $(date -u +%FT%TZ)
