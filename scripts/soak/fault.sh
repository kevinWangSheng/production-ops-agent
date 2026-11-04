#!/usr/bin/env bash
# Forced command of the soak fault key on the rental host (S-R2). sshd runs
#
#   command="sudo -n -u <lab user> /opt/opspilot-soak/fault.sh",restrict ssh-ed25519 ...
#
# for the ``soakfault`` account (rental_setup.sh writes that line and the
# sudoers rule); the client's words arrive in SSH_ORIGINAL_COMMAND (sudo
# keeps it through env_keep, also written by rental_setup.sh), or as the
# arguments when called by hand. Only these are accepted, nothing else runs:
#
#   flag <paymentFailure|adManualGc|loadGeneratorFloodHomepage> <on|off> <item>
#   collector-restart
#   alerts            (read-only: the lab Alertmanager's active alerts, JSON)
#   clock
#   ping
#
# The lab checkout is read from /opt/opspilot-soak/fault.env (REPO=...,
# root-owned); the flag work is scripts/soak/lab_fault.py in that checkout.
set -euo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

ENV_FILE="${OPSPILOT_SOAK_FAULT_ENV:-/opt/opspilot-soak/fault.env}"

if [[ $# -gt 0 ]]; then
    words=("$@")
else
    # Word-split only; no globbing, no expansion of the client's text.
    set -f
    read -r -a words <<<"${SSH_ORIGINAL_COMMAND:-}"
    set +f
fi

refuse() {
    echo "fault.sh: refused: $*" >&2
    exit 64
}

repo() {
    local line
    line="$(grep -E '^REPO=/[A-Za-z0-9_./-]+$' "$ENV_FILE" | tail -n1)" || refuse "no REPO in $ENV_FILE"
    printf '%s' "${line#REPO=}"
}

lab_fault() {
    local root
    root="$(repo)"
    cd "$root"
    exec python3 -E -s -m scripts.soak.lab_fault "$@"
}

case "${#words[@]}:${words[0]:-}" in
    4:flag)
        case "${words[1]}" in
            paymentFailure|adManualGc|loadGeneratorFloodHomepage) ;;
            *) refuse "flag ${words[1]}" ;;
        esac
        case "${words[2]}" in
            on|off) ;;
            *) refuse "state ${words[2]}" ;;
        esac
        [[ "${words[3]}" =~ ^[a-z0-9]{1,8}$ ]] || refuse "item ${words[3]}"
        lab_fault flag "${words[1]}" "${words[2]}" "${words[3]}"
        ;;
    1:collector-restart)
        lab_fault collector-restart
        ;;
    1:alerts)
        lab_fault alerts
        ;;
    1:clock)
        echo "epoch=$(date +%s.%N)"
        timedatectl show -p NTPSynchronized -p TimeUSec 2>/dev/null || true
        chronyc -n tracking 2>/dev/null || true
        ;;
    1:ping)
        echo pong
        ;;
    *)
        refuse "${words[*]:-<empty>}"
        ;;
esac
