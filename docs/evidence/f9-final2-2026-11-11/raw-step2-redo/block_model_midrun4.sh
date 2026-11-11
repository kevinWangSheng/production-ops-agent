#!/bin/bash
# F9 step 2, third run (run as root): once a Run created after this script started has written
# evidence rows, cut the running worker's HTTPS egress (model endpoint) at the host firewall without
# recreating the container. After the Run leaves running/queued, save the worker log and remove the rule.
# Usage: block_model_midrun4.sh <min_evidence_rows>
MIN="${1:-3}"; E=/home/opspilot/f9-final4
q() { docker exec -i opspilot-postgres-1 sh -c 'psql -U "$POSTGRES_USER" -d "${POSTGRES_DB:-$POSTGRES_USER}" -At -F " | "'; }
KNOWN=$(echo "select string_agg(quote_literal(run_id::text), ',') from opspilot_runs;" | q)
echo "known runs at start: $KNOWN"
echo "waiting for a new Run with >= $MIN evidence rows; started $(date -u +%FT%TZ)"
while :; do
  ROW=$(q <<SQL
select r.run_id, r.state, (select count(*) from opspilot_evidence e where e.run_id::text = r.run_id::text) from opspilot_runs r where r.run_id::text not in ($KNOWN) limit 1;
SQL
)
  N=$(echo "$ROW" | awk -F' [|] ' '{print $3}')
  [ -n "$N" ] && [ "$N" -ge "$MIN" ] && break
  sleep 1
done
echo "trigger at $(date -u +%FT%T.%3NZ): $ROW"
IPS=$(docker inspect opspilot-worker-1 --format '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}')
for ip in $IPS; do iptables -I DOCKER-USER -s "$ip" -p tcp --dport 443 -j REJECT --reject-with tcp-reset; echo "rule added for worker ip $ip (tcp/443 REJECT)"; done
echo "egress cut at $(date -u +%FT%T.%3NZ)"; iptables -S DOCKER-USER | grep 443
while :; do
  S=$(q <<SQL
select state from opspilot_runs where run_id::text not in ($KNOWN) limit 1;
SQL
)
  [ "$S" != "running" ] && [ "$S" != "queued" ] && break
  sleep 2
done
echo "Run left running at $(date -u +%FT%T.%3NZ): state=$S"
docker logs opspilot-worker-1 > $E/61-worker-full-log-at-handoff.log 2>&1; chown opspilot: $E/61-worker-full-log-at-handoff.log
echo "worker log saved ($(wc -l < $E/61-worker-full-log-at-handoff.log) lines)"
for ip in $IPS; do iptables -D DOCKER-USER -s "$ip" -p tcp --dport 443 -j REJECT --reject-with tcp-reset; echo "rule removed for $ip"; done
echo "egress restored at $(date -u +%FT%T.%3NZ)"; iptables -S DOCKER-USER
docker exec opspilot-worker-1 python -c "import socket; socket.create_connection(('api.deepseek.com',443),5); print('worker -> api.deepseek.com:443 tcp ok')"
