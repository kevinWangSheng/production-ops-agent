#!/usr/bin/env bash
# Batch-4 SREGym full: 17 problems x 3, one driver invocation per pass, frozen F.
# env_broken -> one --redo; abort -> one retry; second abort on the same pass -> skip it and continue (disclosed).
# ANSWER_LEAK -> stop everything. HARD_STOP_EPOCH (08:30 PT 11-24) -> stop at completed passes.
set -u
cd /home/opspilot/production-ops-agent
LOG=/home/opspilot/rental-logs/b4/G
EV=docs/evidence/rental-2026-11-23/full/sregym
HARD_STOP_EPOCH=$(date -d '2026-11-24T16:30:00Z' +%s)
COMMON=(--round full --upstream tmp/upstream/sregym-ac60c64 --kubeconfig /home/opspilot/.kube/sregym.yaml
        --env-file /home/opspilot/.config/opspilot/rental.env --cluster-path filter_proxy_readonly --trace lab
        --evidence-root "$EV")
export PATH=/usr/local/bin:$PATH
pass() { # seq trial [redo]
  local f="$LOG/p$(printf %02d "$1")-t$2.log" extra=()
  [ -n "${3:-}" ] && extra=(--redo "p$(printf %02d "$1"):t$2")
  echo "start $(date -u +%FT%TZ) commit $(git rev-parse HEAD)" >> "$f"
  .venv/bin/python -m scripts.sregym_eval.driver round "${COMMON[@]}" --problems "$1" --trials "$2" "${extra[@]}" >> "$f" 2>&1
  local rc=$? line; line=$(grep -E '^\{"(seq|status)"' "$f" | tail -1)
  echo "$(date -Is) p$1 t$2 rc=$rc ${3:+retry }$line" >> "$LOG/runner.log"
  if [ $rc -ne 0 ]; then echo abort; elif echo "$line" | grep -q '"env_broken"'; then echo env_broken; else echo ok; fi
}
echo "$(date -Is) runner start pid=$$" >> "$LOG/runner.log"
for seq in $(seq 1 17); do
  for t in 1 2 3; do
    [ "$(date +%s)" -ge "$HARD_STOP_EPOCH" ] && { echo "$(date -Is) HARD STOP reached before p$seq t$t" >> "$LOG/runner.log"; exit 4; }
    [ -e "$LOG/STOP" ] && { echo "$(date -Is) STOP file before p$seq t$t" >> "$LOG/runner.log"; exit 0; }
    r=$(pass "$seq" "$t")
    f="$LOG/p$(printf %02d "$seq")-t$t.log"
    grep -q ANSWER_LEAK "$f" && { echo "$(date -Is) STOP answer leak p$seq t$t" >> "$LOG/runner.log"; touch "$LOG/ANSWER_LEAK"; exit 3; }
    if [ "$r" = abort ]; then
      r=$(pass "$seq" "$t")
      grep -q ANSWER_LEAK "$f" && { echo "$(date -Is) STOP answer leak p$seq t$t" >> "$LOG/runner.log"; touch "$LOG/ANSWER_LEAK"; exit 3; }
      [ "$r" = abort ] && echo "$(date -Is) SKIP second abort p$seq t$t" >> "$LOG/runner.log"
    elif [ "$r" = env_broken ]; then
      r=$(pass "$seq" "$t" redo)
      [ "$r" = abort ] && echo "$(date -Is) SKIP abort on redo p$seq t$t" >> "$LOG/runner.log"
    fi
  done
done
echo "$(date -Is) runner done" >> "$LOG/runner.log"
