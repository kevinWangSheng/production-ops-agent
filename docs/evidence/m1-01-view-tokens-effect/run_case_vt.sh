#!/bin/zsh
# usage: run_case_vt.sh <case-name> <idempotency-key> [question-file]
set -eu
CASE=$1; KEY=$2
S=<scratchpad>
WT=<worktree>
OUT=$WT/docs/evidence/m1-01-view-tokens-effect/$CASE
mkdir -p "$OUT"
PW=$(cat $S/ui-password-limits)
Q_DEFAULT='Checkout is reported to show elevated errors in the OTel Demo. Using the authorized metrics range query and trace search for the last 5 minutes, determine whether checkout and its dependency calls (payment, product-catalog, cart, currency, shipping, email) show failures or elevated latency, cite trace ids, and return a json investigation report.'
Q="$Q_DEFAULT"; [ -n "${3:-}" ] && Q=$(cat "$3")
SUBMIT_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)
curl -sS -u "demo:$PW" -H 'Origin: http://127.0.0.1:8080' \
  -d target_id=m0-otel-20260909 -d "idempotency_key=$KEY" \
  --data-urlencode "question=$Q" \
  -o "$OUT/intake.json" -w '%{http_code}\n' http://127.0.0.1:8080/intake/ui
INC=$(python3 -c "import json;print(json.load(open('$OUT/intake.json'))['incident_id'])")
RUN=$(python3 -c "import json;print(json.load(open('$OUT/intake.json'))['run_id'])")
echo "incident=$INC run=$RUN submit_at=$SUBMIT_AT"
printf '{"case":"%s","idempotency_label":"%s","submit_at_utc":"%s","incident_id":"%s","run_id":"%s","question":%s}\n' "$CASE" "$KEY" "$SUBMIT_AT" "$INC" "$RUN" "$(python3 -c "import json,sys;print(json.dumps(sys.argv[1]))" "$Q")" > "$OUT/request.json"
for i in {1..400}; do
  if grep -q "attempt incident=$INC" $S/worker-vt.log; then break; fi
  sleep 5
done
grep "attempt incident=$INC" $S/worker-vt.log | tee "$OUT/worker-attempt.txt"
curl -sS --max-time 15 -u "demo:$PW" "http://127.0.0.1:8080/incidents/$INC/events?cursor=0" -o "$OUT/sse.txt" || true
curl -sS -u "demo:$PW" "http://127.0.0.1:8080/incidents/$INC" -o "$OUT/incident.html"
cd $WT && .venv/bin/python - "$RUN" "$OUT/window.json" <<'PY'
import json, sys, psycopg
run_id, out = sys.argv[1], sys.argv[2]
with psycopg.connect("host=127.0.0.1 port=55431 dbname=m0_budget user=m0_lab") as c:
    row = c.execute("SELECT input, state, deadline FROM opspilot_runs WHERE run_id=%s", (run_id,)).fetchone()
inp = row[0] or {}
def find(o, path=""):
    if isinstance(o, dict):
        for k, v in o.items():
            if k in ("scope_window", "window") and isinstance(v, dict):
                yield path + "/" + k, v
            yield from find(v, path + "/" + k)
    elif isinstance(o, list):
        for i, v in enumerate(o):
            yield from find(v, f"{path}[{i}]")
found = dict(find(inp))
json.dump({"run_id": run_id, "state": row[1], "deadline": row[2].isoformat(), "windows_in_input": found}, open(out, "w"), indent=1, default=str)
print(json.dumps({"state": row[1], "windows": found}, default=str))
PY
$WT/.venv/bin/python $WT/docs/evidence/m1-01-v4-acceptance/extract_ledger.py "$INC" "$OUT" | tee "$OUT/stats.json"
