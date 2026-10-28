#!/usr/bin/env bash
# OpsPilot control-side install (#392).
#
#   install.sh [install]                 first install, or re-run (idempotent)
#   install.sh upgrade                   new image, same secrets and data
#   install.sh uninstall                 remove containers; keep data and secrets
#   install.sh uninstall --purge-data    also delete the database volume
#   install.sh compose <args...>         docker compose on this install
#                                        (e.g. `compose ps`, `compose logs web`)
#
# Order on install and upgrade (C3 §11): stop every runtime -> start postgres
# -> migrate (one-shot, owner) -> init-roles (one-shot, owner) -> start the
# runtimes, each of which verifies the schema head on start. Nothing relies on
# Compose depends_on for this order.
#
# Settings (environment; the values used are kept in <secrets dir>/install.env
# and reused by later runs unless given again):
#   OPSPILOT_SECRETS_DIR      secrets and settings, outside the repository
#                             (default ${XDG_CONFIG_HOME:-~/.config}/opspilot)
#   OPSPILOT_PROJECT          Compose project name (default $COMPOSE_PROJECT_NAME,
#                             else opspilot)
#   OPSPILOT_WEB_HOST_PORT    workbench port on 127.0.0.1 (default 18080)
#                             (allowed origins follow it: 127.0.0.1 and localhost)
#   OPSPILOT_IMAGE            product image; opspilot:local (default) is built
#                             from this checkout, anything else is pulled
#   OPSPILOT_TOOL_PROFILE     fixture (default) or otel-demo
#   OPSPILOT_START_AGENT      1 also starts worker and observer (fill
#                             worker_env / observer_env first)
#
# Generated once and never regenerated (re-running keeps every password):
# database passwords as pgpass files, the workbench password (ui_password, for
# the operator; the web only gets its PBKDF2 hash) and the Alertmanager bearer
# token (alertmanager_token; the web only gets its sha256). Secret values are
# never printed.
set -euo pipefail

readonly COMPOSE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly REPO_ROOT="$(cd "$COMPOSE_DIR/../.." && pwd)"
readonly COMPOSE_FILE="$COMPOSE_DIR/compose.yaml"
readonly LOCAL_IMAGE="opspilot:local"
readonly UI_USER="operator"
readonly EVENT_ACTOR="alertmanager"
readonly RUNTIMES=(web worker observer)
readonly SETTINGS=(OPSPILOT_PROJECT OPSPILOT_WEB_HOST_PORT
    OPSPILOT_IMAGE OPSPILOT_TOOL_PROFILE OPSPILOT_START_AGENT)

log() { printf '[opspilot-install %s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
die() { printf 'install.sh: %s\n' "$*" >&2; exit 1; }

usage() {
    sed -n '3,10p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2
    exit 2
}

# -- settings ------------------------------------------------------------------

inside_repo() {
    case "$1/" in "$REPO_ROOT/"* | "$(cd "$REPO_ROOT" && pwd -P)/"*) return 0 ;; esac
    return 1
}

resolve_settings() {
    [ "$(id -u)" != 0 ] || die "run as a dedicated non-root user, not root"
    SECRETS_DIR="${OPSPILOT_SECRETS_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/opspilot}"
    case "$SECRETS_DIR" in /*) ;; *) SECRETS_DIR="$PWD/$SECRETS_DIR" ;; esac
    inside_repo "$SECRETS_DIR" && die "OPSPILOT_SECRETS_DIR must be outside the repository"
    mkdir -p -m 700 "$SECRETS_DIR"
    SECRETS_DIR="$(cd "$SECRETS_DIR" && pwd -P)"
    inside_repo "$SECRETS_DIR" && die "OPSPILOT_SECRETS_DIR must be outside the repository"
    chmod 700 "$SECRETS_DIR"
    # Persisted values fill in only what the caller did not set.
    local key value
    if [ -f "$SECRETS_DIR/install.env" ]; then
        while IFS='=' read -r key value; do
            case " ${SETTINGS[*]} " in *" $key "*) ;; *) continue ;; esac
            [ -n "${!key:-}" ] || export "$key=$value"
        done < "$SECRETS_DIR/install.env"
    fi
    export OPSPILOT_SECRETS_DIR="$SECRETS_DIR"
    export OPSPILOT_PROJECT="${OPSPILOT_PROJECT:-${COMPOSE_PROJECT_NAME:-opspilot}}"
    export OPSPILOT_WEB_HOST_PORT="${OPSPILOT_WEB_HOST_PORT:-18080}"
    case "$OPSPILOT_WEB_HOST_PORT" in
        '' | *[!0-9]*) die "OPSPILOT_WEB_HOST_PORT must be a port number" ;;
    esac
    export OPSPILOT_IMAGE="${OPSPILOT_IMAGE:-$LOCAL_IMAGE}"
    export OPSPILOT_TOOL_PROFILE="${OPSPILOT_TOOL_PROFILE:-fixture}"
    export OPSPILOT_START_AGENT="${OPSPILOT_START_AGENT:-0}"
    export OPSPILOT_RUNTIME_UID="$(id -u)"
    export OPSPILOT_RUNTIME_GID="$(id -g)"
}

save_settings() {
    local key tmp="$SECRETS_DIR/.install.env.tmp"
    : > "$tmp"
    for key in "${SETTINGS[@]}"; do
        printf '%s=%s\n' "$key" "${!key}" >> "$tmp"
    done
    mv "$tmp" "$SECRETS_DIR/install.env"
}

compose() {
    docker compose -p "$OPSPILOT_PROJECT" -f "$COMPOSE_FILE" "$@"
}

# -- secrets -------------------------------------------------------------------

random_hex() { od -An -tx1 -N32 /dev/urandom | tr -d ' \n'; }

# write_once <name> <command...>: create <name> from the command's stdout
# unless it already exists; mode 0600 either way.
write_once() {
    local name="$1" path="$SECRETS_DIR/$1"
    shift
    if [ ! -s "$path" ]; then
        "$@" > "$path.tmp"
        [ -s "$path.tmp" ] || { rm -f "$path.tmp"; die "could not generate $name"; }
        mv "$path.tmp" "$path"
        log "generated $name"
    fi
    chmod 600 "$path"
}

pgpass_line() { printf '*:*:*:%s:%s\n' "$1" "$2"; }
owner_pgpass() { pgpass_line opspilot_owner "$(cat "$SECRETS_DIR/postgres_password")"; }
login_pgpass() { pgpass_line "$1" "$(random_hex)"; }
# Hashes on the host (standard-library script), so no image is needed yet.
credential_entry() {
    command -v python3 > /dev/null || die "python3 is required to hash the generated credentials"
    python3 -I "$REPO_ROOT/opspilot/deploy/credentials.py" "$1" "$2" < "$SECRETS_DIR/$3"
}
placeholder_env() {
    printf '# %s: KEY=value lines read by the %s (see deploy/compose/compose.yaml)\n' "$1" "$2"
}

ensure_secrets() {
    umask 077
    write_once postgres_password random_hex
    write_once owner_pgpass owner_pgpass
    write_once web_pgpass login_pgpass opspilot_web_app
    write_once worker_pgpass login_pgpass opspilot_worker_app
    write_once observer_pgpass login_pgpass opspilot_observer_app
    write_once retention_pgpass login_pgpass opspilot_retention_app
    write_once ui_password random_hex
    write_once alertmanager_token random_hex
    write_once web_ui_users credential_entry ui-user "$UI_USER" ui_password
    write_once web_event_tokens credential_entry event-token "$EVENT_ACTOR" alertmanager_token
    write_once worker_env placeholder_env worker_env "worker (DEEPSEEK_API_KEY)"
    write_once observer_env placeholder_env observer_env "observer (Prometheus credentials)"
    export_auth_revision
}

# Identifies the credential set on every principal; a hash of hashes.
export_auth_revision() {
    export OPSPILOT_AUTH_REVISION="auth-$(cat "$SECRETS_DIR/web_ui_users" "$SECRETS_DIR/web_event_tokens" | sha256 | cut -c1-12)"
}

sha256() {
    if command -v sha256sum > /dev/null; then sha256sum; else shasum -a 256; fi
}

# -- steps ---------------------------------------------------------------------

prepare_image() {
    if [ "$OPSPILOT_IMAGE" = "$LOCAL_IMAGE" ]; then
        log "building $LOCAL_IMAGE from $REPO_ROOT"
        docker build --tag "$LOCAL_IMAGE" --file "$REPO_ROOT/Dockerfile" "$REPO_ROOT"
    else
        log "pulling $OPSPILOT_IMAGE"
        docker pull "$OPSPILOT_IMAGE"
    fi
}

runtimes_to_start() {
    if [ "$OPSPILOT_START_AGENT" = 1 ]; then echo web worker observer; else echo web; fi
}

deploy() {
    log "stopping runtimes (${RUNTIMES[*]}) before migrating"
    COMPOSE_PROFILES=agent compose stop "${RUNTIMES[@]}"
    log "starting postgres"
    compose up --detach --wait postgres
    log "migrate: start"
    compose up --no-deps --exit-code-from migrate migrate
    log "migrate: done"
    log "init-roles: start"
    compose up --no-deps --exit-code-from init-roles init-roles
    log "init-roles: done"
    # shellcheck disable=SC2046
    set -- $(runtimes_to_start)
    log "starting runtimes: $*"
    COMPOSE_PROFILES=agent compose up --detach --no-deps --wait "$@"
    log "workbench: http://127.0.0.1:$OPSPILOT_WEB_HOST_PORT (loopback only)"
    log "login: user '$UI_USER', password in $SECRETS_DIR/ui_password"
    log "Alertmanager bearer token: $SECRETS_DIR/alertmanager_token"
}

cmd_install() {
    resolve_settings
    prepare_image
    ensure_secrets
    save_settings
    deploy
}

cmd_upgrade() {
    resolve_settings
    [ -s "$SECRETS_DIR/postgres_password" ] || die "no install found in $SECRETS_DIR; run install first"
    prepare_image
    ensure_secrets
    save_settings
    deploy
}

cmd_uninstall() {
    local purge=0
    case "${1:-}" in
        '') ;;
        --purge-data) purge=1 ;;
        *) usage ;;
    esac
    resolve_settings
    if [ "$purge" = 0 ]; then
        COMPOSE_PROFILES=agent,maintenance compose down --remove-orphans
        log "containers removed; database volume and $SECRETS_DIR kept"
        return
    fi
    local answer="${OPSPILOT_PURGE_CONFIRM:-}"
    if [ -z "$answer" ] && [ -t 0 ]; then
        printf 'This deletes the OpsPilot database of project %s. Type the project name to confirm: ' "$OPSPILOT_PROJECT" >&2
        read -r answer
    fi
    [ "$answer" = "$OPSPILOT_PROJECT" ] || die "--purge-data not confirmed (type the project name, or set OPSPILOT_PURGE_CONFIRM=$OPSPILOT_PROJECT); nothing removed"
    COMPOSE_PROFILES=agent,maintenance compose down --remove-orphans --volumes
    log "containers and database volume removed; secrets in $SECRETS_DIR kept"
}

main() {
    local command="${1:-install}"
    [ $# -eq 0 ] || shift
    case "$command" in
        install) [ $# -eq 0 ] || usage; cmd_install ;;
        upgrade) [ $# -eq 0 ] || usage; cmd_upgrade ;;
        uninstall) [ $# -le 1 ] || usage; cmd_uninstall "$@" ;;
        compose)
            resolve_settings
            [ -s "$SECRETS_DIR/web_ui_users" ] || die "no install found in $SECRETS_DIR; run install first"
            export_auth_revision
            compose "$@"
            ;;
        -h | --help) usage ;;
        *) usage ;;
    esac
}

main "$@"
