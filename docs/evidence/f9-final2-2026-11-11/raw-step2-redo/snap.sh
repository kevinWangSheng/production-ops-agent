#!/bin/bash
# snap.sh <project> <label>: read-only state snapshot of one Compose project (SQL fed on stdin).
P="$1"; LABEL="$2"
echo "=== snapshot project=$P label=$LABEL at $(date -u +%FT%TZ)"
docker ps --format '{{.Names}} {{.Image}} {{.Status}}' | grep "^$P-" | sort
for s in web worker observer; do
  c="$P-$s-1"
  docker inspect "$c" --format "$s config_image={{.Config.Image}} image_id={{.Image}} started={{.State.StartedAt}}"
  docker exec "$c" python -c "
import urllib.request
for p in ('healthz','readyz'):
    try:
        r=urllib.request.urlopen('http://127.0.0.1:9464/'+p, timeout=5); print('$s', p, r.status, r.read()[:300].decode(errors='replace'))
    except Exception as e:
        print('$s', p, 'ERROR', getattr(e,'code',''), e)
"
  docker logs "$c" 2>&1 | grep -o '"version": "[^"]*", "revision": "[^"]*"' | sort | uniq -c | sed "s/^/$s log /"
done
docker exec -i "$P-postgres-1" sh -c 'psql -U "$POSTGRES_USER" -d "${POSTGRES_DB:-$POSTGRES_USER}" -At -F " | "' <<'SQL'
select 'db_now', now();
select 'alembic', version_num from alembic_version;
select 'incident', incident_id, state, lifecycle, mode, control_generation, created_at, closed_at from opspilot_incidents order by created_at;
select 'run', run_id, incident_id, state, epoch, budget_spent, tool_operations_used from opspilot_runs order by deadline;
select 'session', session_id, incident_id, state, ended_reason, healthy_since, adopted_count, created_at, updated_at from opspilot_observation_sessions order by created_at;
select 'count_controls', count(*) from opspilot_controls;
select 'count_evidence', count(*) from opspilot_evidence;
select 'count_samples', count(*) from opspilot_observation_samples;
select 'count_steps', count(*) from opspilot_steps;
select 'count_alert_deliveries', count(*) from opspilot_alert_deliveries;
select 'count_postmortem_versions', count(*) from opspilot_postmortem_versions;
select 'last_sample', sequence, outcome, window_end from opspilot_observation_samples order by window_end desc limit 3;
SQL
