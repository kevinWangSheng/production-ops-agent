#!/usr/bin/env bash
# Rental-host setup for the 72h soak (S-R1, S-R2), run once as root after
# install_linux.sh brought the kind lab up:
#
#   sudo scripts/soak/rental_setup.sh --lab-user <user> \
#     --tunnel-key <tunnel.pub> --fault-key <fault.pub>
#
# Creates two non-root accounts. ``soaktunnel`` may only forward: local
# forwards to the lab Prometheus proxy and Jaeger on 127.0.0.1, remote
# listens on the kind network gateway for the workbench webhook (28080) and
# the three health listeners (29464-29466). ``soakfault`` may only run
# /opt/opspilot-soak/fault.sh as the lab user (forced command + sudoers).
# sshd gets a drop-in (GatewayPorts clientspecified for soaktunnel only) and,
# when ufw is active, the kind bridge interface alone admits the kind subnet
# to those four gateway ports. No public port is opened. Re-running is safe.
# The private keys stay on da-server; this script only reads public keys.
set -euo pipefail

LAB_USER=""
TUNNEL_KEY=""
FAULT_KEY=""
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SOAK_DIR=/opt/opspilot-soak
TUNNEL_USER=soaktunnel
FAULT_USER=soakfault
WEBHOOK_PORT=28080
HEALTH_PORTS=(29464 29465 29466)

die() { echo "rental_setup.sh: $*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --lab-user) LAB_USER="$2"; shift 2 ;;
        --tunnel-key) TUNNEL_KEY="$2"; shift 2 ;;
        --fault-key) FAULT_KEY="$2"; shift 2 ;;
        -h|--help) sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "unknown argument $1" ;;
    esac
done
[[ $EUID -eq 0 ]] || die "run as root"
[[ -n "$LAB_USER" ]] && id "$LAB_USER" >/dev/null || die "--lab-user must name an existing user"
for key in "$TUNNEL_KEY" "$FAULT_KEY"; do
    [[ -f "$key" ]] || die "public key file missing: $key"
    grep -qE '^(ssh-ed25519|ecdsa-sha2-nistp256) [A-Za-z0-9+/=]+( [^"]*)?$' "$key" \
        || die "$key is not a single ed25519/ecdsa public key line"
done
[[ "$REPO" =~ ^/[A-Za-z0-9_./-]+$ ]] || die "repository path has unusual characters: $REPO"

GATEWAY="$(docker network inspect kind -f '{{range .IPAM.Config}}{{if .Gateway}}{{.Gateway}} {{end}}{{end}}' | tr ' ' '\n' | grep -E '^[0-9.]+$' | head -n1)"
SUBNET="$(docker network inspect kind -f '{{range .IPAM.Config}}{{.Subnet}} {{end}}' | tr ' ' '\n' | grep -E '^[0-9.]+/[0-9]+$' | head -n1)"
NETWORK_ID="$(docker network inspect kind -f '{{.Id}}')"
BRIDGE="br-${NETWORK_ID:0:12}"
[[ -n "$GATEWAY" && -n "$SUBNET" ]] || die "kind network has no IPv4 gateway/subnet (is the lab up?)"
ip link show "$BRIDGE" >/dev/null || die "bridge $BRIDGE not found"

ensure_user() {
    local name="$1" shell="$2"
    id "$name" >/dev/null 2>&1 || useradd --system --create-home --shell "$shell" "$name"
    usermod --shell "$shell" "$name"
    install -d -m 700 -o "$name" -g "$name" "/home/$name/.ssh"
}

ensure_user "$TUNNEL_USER" /usr/sbin/nologin
ensure_user "$FAULT_USER" /bin/sh

listens=""
for port in "$WEBHOOK_PORT" "${HEALTH_PORTS[@]}"; do
    listens+=",permitlisten=\"$GATEWAY:$port\""
done
printf 'restrict,port-forwarding,permitopen="127.0.0.1:19090",permitopen="127.0.0.1:16686"%s %s\n' \
    "$listens" "$(cat "$TUNNEL_KEY")" >"/home/$TUNNEL_USER/.ssh/authorized_keys"
printf 'restrict,command="sudo -n -u %s %s/fault.sh" %s\n' \
    "$LAB_USER" "$SOAK_DIR" "$(cat "$FAULT_KEY")" >"/home/$FAULT_USER/.ssh/authorized_keys"
for name in "$TUNNEL_USER" "$FAULT_USER"; do
    chown "$name:$name" "/home/$name/.ssh/authorized_keys"
    chmod 600 "/home/$name/.ssh/authorized_keys"
done

install -d -m 755 -o root -g root "$SOAK_DIR"
install -m 755 -o root -g root "$REPO/scripts/soak/fault.sh" "$SOAK_DIR/fault.sh"
printf 'REPO=%s\n' "$REPO" >"$SOAK_DIR/fault.env"
chown root:root "$SOAK_DIR/fault.env"
chmod 644 "$SOAK_DIR/fault.env"

sudoers=/etc/sudoers.d/opspilot-soak
cat >"$sudoers.tmp" <<EOF
Defaults!$SOAK_DIR/fault.sh env_keep += "SSH_ORIGINAL_COMMAND"
$FAULT_USER ALL=($LAB_USER) NOPASSWD: $SOAK_DIR/fault.sh ""
EOF
chmod 440 "$sudoers.tmp"
visudo -cf "$sudoers.tmp" >/dev/null || { rm -f "$sudoers.tmp"; die "sudoers check failed"; }
mv "$sudoers.tmp" "$sudoers"

dropin=/etc/ssh/sshd_config.d/60-opspilot-soak.conf
cat >"$dropin" <<EOF
# OpsPilot 72h soak (scripts/soak/rental_setup.sh). Remove with the host.
Match User $TUNNEL_USER
    AllowTcpForwarding yes
    GatewayPorts clientspecified
    PermitTTY no
    X11Forwarding no
    AllowAgentForwarding no
Match User $FAULT_USER
    AllowTcpForwarding no
    PermitTTY no
    X11Forwarding no
    AllowAgentForwarding no
EOF
sshd -t || die "sshd config check failed (see $dropin)"
systemctl reload ssh 2>/dev/null || systemctl reload sshd

if command -v ufw >/dev/null && ufw status | grep -q '^Status: active'; then
    ufw allow in on "$BRIDGE" from "$SUBNET" to "$GATEWAY" port "$WEBHOOK_PORT" proto tcp
    ufw allow in on "$BRIDGE" from "$SUBNET" to "$GATEWAY" port "${HEALTH_PORTS[0]}:${HEALTH_PORTS[-1]}" proto tcp
fi

cat <<EOF
soak rental setup done.
  kind gateway   $GATEWAY  (subnet $SUBNET, bridge $BRIDGE)
  lab values     OPSPILOT_LAB_HOST_ADDRESS=$GATEWAY
                 OPSPILOT_LAB_HOST_PORTS=webhook=$WEBHOOK_PORT,web=${HEALTH_PORTS[0]},worker=${HEALTH_PORTS[1]},observer=${HEALTH_PORTS[2]}
                 then: sudo -u $LAB_USER python3 scripts/kind_lab.py up
  tunnel account $TUNNEL_USER  (forwards only)
  fault account  $FAULT_USER   (forced command $SOAK_DIR/fault.sh as $LAB_USER)
EOF
