#!/usr/bin/env bash
# 2026-11-24b rental: #830 verification at main 179c1e30.
# Precondition (not counted): p01 x1, round pre1124. Then p12 x3, round r1124.
# Same rules as b4/g_runner.sh: env_broken -> one --redo; abort -> one retry; ANSWER_LEAK -> stop all.
set -u
cd /home/opspilot/production-ops-agent
LOG=/home/opspilot/rental-logs/r1124
EV=docs/evidence/rental-2026-11-24b/sregym
export PATH=/usr/local/bin:$PATH
pass() { # round seq trial [redo]
  local f="$LOG/p$(printf %02d "$2")-t$3.log" extra=()
  [ -n "${4:-}" ] && extra=(--redo "p$(printf %02d "$2"):t$3")
  echo "start $(date -u +%FT%TZ) commit $(git rev-parse HEAD)" >> "$f"
  .venv/bin/python -m scripts.sregym_eval.driver round --round "$1" \
    --upstream tmp/upstream/sregym-ac60c64 --kubeconfig /home/opspilot/.kube/sregym.yaml \
    --env-file /home/opspilot/.config/opspilot/rental.env --cluster-path filter_proxy_readonly --trace lab \
    --evidence-root "$EV" --problems "$2" --trials "$3" "${extra[@]}" >> "$f" 2>&1
  local rc=$? line; line=$(grep -E '^\{"(seq|status)"' "$f" | tail -1)
  echo "$(date -Is) $1 p$2 t$3 rc=$rc ${4:+redo }$line" >> "$LOG/runner.log"
  if [ $rc -ne 0 ]; then echo abort; elif echo "$line" | grep -q '"env_broken"'; then echo env_broken; else echo ok; fi
}
run() { # round seq trial
  local f="$LOG/p$(printf %02d "$2")-t$3.log" r
  [ -e "$LOG/STOP" ] && { echo "$(date -Is) STOP file before p$2 t$3" >> "$LOG/runner.log"; exit 0; }
  r=$(pass "$1" "$2" "$3")
  grep -q ANSWER_LEAK "$f" && { echo "$(date -Is) STOP answer leak p$2 t$3" >> "$LOG/runner.log"; touch "$LOG/ANSWER_LEAK"; exit 3; }
  if [ "$r" = abort ]; then
    r=$(pass "$1" "$2" "$3"); [ "$r" = abort ] && echo "$(date -Is) SKIP second abort p$2 t$3" >> "$LOG/runner.log"
  elif [ "$r" = env_broken ]; then
    r=$(pass "$1" "$2" "$3" redo); [ "$r" = abort ] && echo "$(date -Is) SKIP abort on redo p$2 t$3" >> "$LOG/runner.log"
  fi
  grep -q ANSWER_LEAK "$f" && { echo "$(date -Is) STOP answer leak p$2 t$3" >> "$LOG/runner.log"; touch "$LOG/ANSWER_LEAK"; exit 3; }
}
echo "$(date -Is) runner start pid=$$" >> "$LOG/runner.log"
run pre1124 1 1
[ -e "$LOG/HOLD_AFTER_PRE" ] && { echo "$(date -Is) hold after precondition" >> "$LOG/runner.log"; exit 0; }
for t in 1 2 3; do run r1124 12 "$t"; done
echo "$(date -Is) runner done" >> "$LOG/runner.log"
