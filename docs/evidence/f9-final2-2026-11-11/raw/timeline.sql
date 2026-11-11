select 'now', now();
select 'incident', to_jsonb(i) from opspilot_incidents i where incident_id::text like 'aa6f1172%';
select 'run', run_id, state, epoch, budget_spent, tool_operations_used from opspilot_runs where incident_id::text like 'aa6f1172%';
select 'control', created_at, action, actor, payload->>'channel', expected_generation, resulting_generation, payload->>'revision' from opspilot_controls where incident_id::text like 'aa6f1172%' order by created_at;
select 'delivery', received_at, status, outcome, starts_at from opspilot_alert_deliveries where incident_id::text like 'aa6f1172%' order by received_at;
select 'session', to_jsonb(o) from opspilot_observation_sessions o where incident_id::text like 'aa6f1172%';
select 'sample', s.sequence, s.outcome, s.window_end from opspilot_observation_samples s join opspilot_observation_sessions o using (session_id) where o.incident_id::text like 'aa6f1172%' order by s.sequence;
select 'evidence_count', count(*) from opspilot_evidence e where e.run_id::text in (select run_id::text from opspilot_runs where incident_id::text like 'aa6f1172%');
select 'postmortem', count(*) from opspilot_postmortem_versions;
