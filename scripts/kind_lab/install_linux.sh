#!/usr/bin/env bash
# Automated setup of the kind lab on a clean Linux host (F1 closure S4; the
# target side of F9 step 1's "documented automated setup").
#
#   sudo scripts/kind_lab/install_linux.sh            # install tools, then kind_lab.py up
#   sudo scripts/kind_lab/install_linux.sh --no-up    # install tools only
#   scripts/kind_lab/install_linux.sh --user <name>   # as root: the lab user
#
# Supported: Ubuntu 24.04 (noble) on x86_64. Installs pinned versions only:
# Docker Engine from Docker's apt repository (key checked against a pinned
# SHA-256, packages held), kind, kubectl and helm as release binaries checked
# against pinned SHA-256 sums, plus apache2-utils (htpasswd, bcrypt for the
# lab Prometheus accounts). Raises the inotify limits kind needs for a
# multi-pod cluster. It does not touch the firewall and opens no port: kind
# publishes the lab NodePorts on 127.0.0.1 only (scripts/kind_lab/kind-config.yaml).
#
# Run as root (sudo). When started through sudo by a regular user, or as root
# with --user <name> (a root-only image: no sudo-capable user is needed), that
# user joins the docker group and runs ``kind_lab.py up``; lab state goes to
# the git-ignored tmp/m1-kind-lab/ of this checkout, which that user must
# own. Re-running is safe: binaries whose checksum already matches are kept,
# and ``up`` is idempotent.
set -euo pipefail

DOCKER_VERSION="5:29.2.1-1~ubuntu.24.04~noble"
CONTAINERD_VERSION="2.2.1-1~ubuntu.24.04~noble"
COMPOSE_VERSION="5.1.0-1~ubuntu.24.04~noble"
BUILDX_VERSION="0.31.1-1~ubuntu.24.04~noble"
# https://download.docker.com/linux/ubuntu/gpg as fetched 2026-11-04
DOCKER_GPG_SHA256="1500c1f56fa9e26b9b8f42452a553675796ade0807cdce11975eb98170b3a570"
KIND_VERSION="v0.33.0"
KIND_SHA256="aee6151561422756b764a4ae28e7f44cda5af5a9eead3cc9985112b1de8d8e0d"
KUBECTL_VERSION="v1.37.1"
KUBECTL_SHA256="65691ff77eb6fa44c908b77a1082c9f092c3b9733b5cefabec0d1104890e21a8"
HELM_VERSION="v4.3.0"
HELM_SHA256="86584a54def73570558f66f5111cc53dfed56689637ae32c1201205d494f54fb"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RUN_UP=1
LAB_USER="${SUDO_USER:-root}"

usage() {
    sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
    case "$1" in
        --no-up) RUN_UP=0 ;;
        --user)
            [ $# -ge 2 ] || { echo "--user needs a name" >&2; exit 2; }
            LAB_USER="$2"
            shift
            ;;
        -h | --help) usage; exit 0 ;;
        *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

log() { printf '+ %s\n' "$*"; }
die() { printf 'install_linux: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run as root (sudo $0)"
[ "$(uname -s)" = "Linux" ] || die "Linux only; macOS uses colima (docs/development.md)"
[ "$(uname -m)" = "x86_64" ] || die "x86_64 only (pinned checksums are for amd64), got $(uname -m)"
# shellcheck disable=SC1091
. /etc/os-release
[ "${ID:-}" = "ubuntu" ] && [ "${VERSION_CODENAME:-}" = "noble" ] ||
    die "Ubuntu 24.04 (noble) only, got ${PRETTY_NAME:-unknown}"
if [ "$LAB_USER" != root ]; then
    id -u "$LAB_USER" >/dev/null 2>&1 || die "no such user: $LAB_USER"
    sudo -u "$LAB_USER" test -w "$REPO_ROOT" ||
        die "$LAB_USER cannot write $REPO_ROOT; clone the repository as $LAB_USER"
fi

export DEBIAN_FRONTEND=noninteractive
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

log "apt prerequisites"
apt-get update -q
apt-get install -y -q ca-certificates curl apache2-utils python3 >/dev/null

install_docker() {
    local key=/etc/apt/keyrings/docker.asc
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o "$WORK/docker.asc"
    echo "$DOCKER_GPG_SHA256  $WORK/docker.asc" | sha256sum -c --quiet -
    install -m 0644 "$WORK/docker.asc" "$key"
    echo "deb [arch=amd64 signed-by=$key] https://download.docker.com/linux/ubuntu noble stable" \
        >/etc/apt/sources.list.d/docker.list
    apt-get update -q
    log "docker-ce $DOCKER_VERSION"
    apt-get install -y -q --allow-downgrades \
        "docker-ce=$DOCKER_VERSION" \
        "docker-ce-cli=$DOCKER_VERSION" \
        "containerd.io=$CONTAINERD_VERSION" \
        "docker-compose-plugin=$COMPOSE_VERSION" \
        "docker-buildx-plugin=$BUILDX_VERSION" >/dev/null
    apt-mark hold docker-ce docker-ce-cli containerd.io docker-compose-plugin \
        docker-buildx-plugin >/dev/null
    systemctl enable --now docker >/dev/null
}

# install_binary <name> <sha256> <url>: fetch, check, install to /usr/local/bin
install_binary() {
    local name="$1" sum="$2" url="$3" dest="/usr/local/bin/$1"
    if [ -x "$dest" ] && echo "$sum  $dest" | sha256sum -c --quiet - 2>/dev/null; then
        log "$name already at the pinned checksum"
        return
    fi
    log "$name from $url"
    curl -fsSL "$url" -o "$WORK/$name"
    echo "$sum  $WORK/$name" | sha256sum -c --quiet -
    install -m 0755 "$WORK/$name" "$dest"
}

install_helm() {
    local tarball="helm-$HELM_VERSION-linux-amd64.tar.gz"
    if command -v helm >/dev/null && helm version --short 2>/dev/null | grep -q "^$HELM_VERSION+"; then
        log "helm $HELM_VERSION already installed"
        return
    fi
    log "helm $HELM_VERSION"
    curl -fsSL "https://get.helm.sh/$tarball" -o "$WORK/$tarball"
    echo "$HELM_SHA256  $WORK/$tarball" | sha256sum -c --quiet -
    tar -xzf "$WORK/$tarball" -C "$WORK" linux-amd64/helm
    install -m 0755 "$WORK/linux-amd64/helm" /usr/local/bin/helm
}

install_docker
install_binary kind "$KIND_SHA256" \
    "https://github.com/kubernetes-sigs/kind/releases/download/$KIND_VERSION/kind-linux-amd64"
install_binary kubectl "$KUBECTL_SHA256" \
    "https://dl.k8s.io/release/$KUBECTL_VERSION/bin/linux/amd64/kubectl"
install_helm

# kind's known-issues page: a multi-pod cluster exhausts the default inotify
# limits ("too many open files" in pods).
log "inotify limits for kind"
cat >/etc/sysctl.d/99-opspilot-kind.conf <<'SYSCTL'
fs.inotify.max_user_watches = 524288
fs.inotify.max_user_instances = 512
SYSCTL
sysctl -q --system

log "installed versions"
docker version --format 'docker {{.Server.Version}}'
kind version
kubectl version --client | head -1
helm version --short

if [ "$RUN_UP" -eq 0 ]; then
    log "--no-up: tools installed; start the lab with python3 scripts/kind_lab.py up"
    exit 0
fi

cd "$REPO_ROOT"
if [ "$LAB_USER" = "root" ]; then
    log "python3 scripts/kind_lab.py up (as root)"
    exec python3 scripts/kind_lab.py up
fi
usermod -aG docker "$LAB_USER"
log "python3 scripts/kind_lab.py up (as $LAB_USER, docker group)"
exec sudo -u "$LAB_USER" -H sg docker -c "python3 scripts/kind_lab.py up"
