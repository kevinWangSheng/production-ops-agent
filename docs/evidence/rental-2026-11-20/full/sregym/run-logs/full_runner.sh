#!/usr/bin/env bash
# Full SREGym run: one driver invocation per pass; env_broken or abort -> one retry.
set -u
cd /home/opspilot/production-ops-agent
LOG=/home/opspilot/rental-logs/sregym/full
EV=docs/evidence/rental-2026-11-20/full/sregym
COMMON=(--round full --upstream tmp/upstream/sregym-ac60c64 --kubeconfig /home/opspilot/.kube/sregym.yaml
        --env-file /home/opspilot/.config/opspilot/rental.env --cluster-path filter_proxy_readonly --trace lab
        --evidence-root "$EV")
pass() { # seq trial [redo]
  local extra=()
  [ -n "${3:-}" ] && extra=(--redo "p$(printf %02d "$1"):t$2")
  .venv/bin/python -m scripts.sregym_eval.driver round "${COMMON[@]}" --problems "$1" --trials "$2" "${extra[@]}" \
    >> "$LOG/p$(printf %02d "$1")-t$2.log" 2>&1
  local rc=$?
  local line; line=$(grep -E '^\{"(seq|status)"' "$LOG/p$(printf %02d "$1")-t$2.log" | tail -1)
  echo "$(date -Is) p$1 t$2 rc=$rc ${3:+retry }$line" >> "$LOG/runner.log"
  if [ $rc -ne 0 ]; then echo abort; elif echo "$line" | grep -q '"env_broken"'; then echo env_broken;
  else echo ok; fi
}
echo "$(date -Is) runner start pid=$$" >> "$LOG/runner.log"
for seq in $(seq 1 17); do
  for t in 1 2 3; do
    r=$(pass "$seq" "$t")
    if [ "$r" = abort ]; then
      grep -q ANSWER_LEAK "$LOG/p$(printf %02d "$seq")-t$t.log" && { echo "$(date -Is) STOP answer leak" >> "$LOG/runner.log"; exit 3; }
      r=$(pass "$seq" "$t")
      [ "$r" = abort ] && { echo "$(date -Is) STOP second abort p$seq t$t" >> "$LOG/runner.log"; exit 2; }
    elif [ "$r" = env_broken ]; then
      r=$(pass "$seq" "$t" redo)
      [ "$r" = abort ] && { echo "$(date -Is) STOP abort on retry p$seq t$t" >> "$LOG/runner.log"; exit 2; }
    fi
  done
done
echo "$(date -Is) runner done" >> "$LOG/runner.log"
