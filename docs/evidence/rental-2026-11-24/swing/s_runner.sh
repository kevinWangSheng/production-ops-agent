#!/usr/bin/env bash
# 2026-11-24 swing reruns (#831) at frozen F 51257e79, then p12 x3 (#830) at main 3d702d65.
# Same rules as b4/g_runner.sh: env_broken -> one --redo; abort -> one retry; ANSWER_LEAK -> stop all.
set -u
LOG=/home/opspilot/rental-logs/s1124
mkdir -p "$LOG"
HARD_STOP_EPOCH=$(date -d '2026-11-24T16:30:00Z' +%s)
export PATH=/usr/local/bin:$PATH
pass() { # dir seq trial [redo]
  local f="$LOG/p$(printf %02d "$2")-t$3.log" extra=()
  [ -n "${4:-}" ] && extra=(--redo "p$(printf %02d "$2"):t$3")
  ( cd "$1" && echo "start $(date -u +%FT%TZ) commit $(git rev-parse HEAD)" >> "$f" &&
    .venv/bin/python -m scripts.sregym_eval.driver round --round swing1124 \
      --upstream /home/opspilot/production-ops-agent/tmp/upstream/sregym-ac60c64 \
      --kubeconfig /home/opspilot/.kube/sregym.yaml --env-file /home/opspilot/.config/opspilot/rental.env \
      --cluster-path filter_proxy_readonly --trace lab \
      --evidence-root docs/evidence/rental-2026-11-24/swing/sregym \
      --problems "$2" --trials "$3" "${extra[@]}" >> "$f" 2>&1 )
  local rc=$? line; line=$(grep -E '^\{"(seq|status)"' "$f" | tail -1)
  echo "$(date -Is) p$2 t$3 rc=$rc ${4:+redo }$line" >> "$LOG/runner.log"
  if [ $rc -ne 0 ]; then echo abort; elif echo "$line" | grep -q '"env_broken"'; then echo env_broken; else echo ok; fi
}
run() { # dir seq trial
  local f="$LOG/p$(printf %02d "$2")-t$3.log" r
  [ "$(date +%s)" -ge "$HARD_STOP_EPOCH" ] && { echo "$(date -Is) HARD STOP before p$2 t$3" >> "$LOG/runner.log"; exit 4; }
  [ -e "$LOG/STOP" ] && { echo "$(date -Is) STOP file before p$2 t$3" >> "$LOG/runner.log"; exit 0; }
  r=$(pass "$1" "$2" "$3")
  grep -q ANSWER_LEAK "$f" && { echo "$(date -Is) STOP answer leak p$2 t$3" >> "$LOG/runner.log"; touch "$LOG/ANSWER_LEAK"; exit 3; }
  if [ "$r" = abort ]; then
    r=$(pass "$1" "$2" "$3"); [ "$r" = abort ] && echo "$(date -Is) SKIP second abort p$2 t$3" >> "$LOG/runner.log"
  elif [ "$r" = env_broken ]; then
    r=$(pass "$1" "$2" "$3" redo); [ "$r" = abort ] && echo "$(date -Is) SKIP abort on redo p$2 t$3" >> "$LOG/runner.log"
  fi
}
F=/home/opspilot/production-ops-agent; M=/home/opspilot/poa-main-1124
echo "$(date -Is) runner start pid=$$" >> "$LOG/runner.log"
for seq in 1 5 14; do for t in 1 2 3; do run "$F" "$seq" "$t"; done; done
for t in 1 2 3; do run "$M" 12 "$t"; done
echo "$(date -Is) runner done" >> "$LOG/runner.log"
