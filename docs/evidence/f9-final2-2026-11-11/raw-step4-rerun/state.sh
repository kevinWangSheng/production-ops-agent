#!/bin/bash
# state.sh: one-line-per-row read-only status of incidents, runs, sessions in both projects, plus newest samples and deliveries
echo "--- state at $(date -u +%FT%TZ)"
for P in opspilot opspilot-staging; do
~/f9-final3/q.sh $P <<SQL
select '$P incident', left(incident_id::text,8), state, lifecycle, mode, control_generation, created_at, closed_at from opspilot_incidents where closed_at is null or closed_at > now() - interval '3 hours' order by created_at;
select '$P run', left(run_id::text,8), left(incident_id::text,8), state, budget_spent, tool_operations_used from opspilot_runs where incident_id in (select incident_id from opspilot_incidents where closed_at is null or closed_at > now() - interval '3 hours');
select '$P session', session_id, left(incident_id::text,8), state, ended_reason, healthy_since, (select count(*) from opspilot_observation_samples s where s.session_id=o.session_id) n, updated_at from opspilot_observation_sessions o where o.created_at > now() - interval '3 hours' or o.state not in ('completed','expired','cancelled') order by created_at;
select '$P last_sample', left(session_id::text,8), sequence, outcome, window_end from opspilot_observation_samples order by window_end desc limit 2;
select '$P delivery', received_at, status, outcome, left(incident_id::text,8) from opspilot_alert_deliveries order by received_at desc limit 2;
SQL
done
