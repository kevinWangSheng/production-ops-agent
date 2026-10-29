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
#                             (default: the one this project was installed
#                             with, else ${XDG_CONFIG_HOME:-~/.config}/opspilot);
#                             a different one for a project whose database
#                             volume exists is refused
#   OPSPILOT_PROJECT          Compose project name (default $COMPOSE_PROJECT_NAME,
#                             else opspilot)
#   OPSPILOT_WEB_HOST_PORT    workbench port on 127.0.0.1 (default 18080)
#                             (allowed origins follow it: 127.0.0.1 and localhost)
#   OPSPILOT_IMAGE            product image; opspilot:local (default) is built
#                             from this checkout; anything else is pulled and
#                             must be pinned by digest (<name>@sha256:<64 hex>)
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

# Which secrets directory each Compose project was installed with: one file
# per project holding the directory's path (no secret). It lives outside the
# secrets directory, since it is how that directory is found.
#
# Lookups whose failure matters set a variable and die themselves rather than
# return a status: errexit is suspended inside a function called from
# `if`/`||`/`&&`/`!`, so a failure there would read as "no".
install_record() {
    printf '%s/opspilot/installs/%s' "${XDG_STATE_HOME:-$HOME/.local/state}" "$1"
}

# read_record <project>: sets RECORDED to its recorded secrets dir, or empty.
read_record() {
    local record
    record="$(install_record "$1")"
    RECORDED=""
    [ -e "$record" ] || return 0
    RECORDED="$(cat "$record")" || die "cannot read the install record $record"
}

# Docker reads 0 as "any free port" and normalises 018080 to 18080, so only
# the canonical form is accepted: the allowed origins are built from this
# exact string.
valid_port() {
    case "$1" in
        '' | 0* | *[!0-9]*) die "OPSPILOT_WEB_HOST_PORT must be a port number 1-65535 without leading zeros" ;;
    esac
    [ ${#1} -le 5 ] && [ "$1" -le 65535 ] ||
        die "OPSPILOT_WEB_HOST_PORT must be a port number 1-65535 without leading zeros"
}

# A pulled product image is pinned by digest (B9); a tag can move under an
# install. Only the local build is referenced by name. Checked only where the
# image is pulled and started (install, upgrade): uninstall and compose keep
# working with whatever image an install recorded.
valid_image() {
    [ "$IMAGE_IN_USE" = 1 ] || return 0
    [ "$1" = "$LOCAL_IMAGE" ] && return 0
    [[ "$1" =~ ^[^@[:space:]]+@sha256:[0-9a-f]{64}$ ]] ||
        die "OPSPILOT_IMAGE '$1' must be pinned by digest (<name>@sha256:<64 hex>), or be $LOCAL_IMAGE; nothing changed"
}

valid_project() {
    case "$1" in
        '' | [!a-z0-9]* | *[!a-z0-9_-]*)
            die "project name '$1' must be lowercase letters, digits, '-' or '_'" ;;
    esac
}

# lookup_volume: sets VOLUME to present or absent for this project's database
# volume; a failed lookup stops the run (it is never taken as "absent").
lookup_volume() {
    local volumes
    volumes="$(docker volume ls --quiet --filter "label=com.docker.compose.project=$OPSPILOT_PROJECT")" ||
        die "could not list Docker volumes to check for project $OPSPILOT_PROJECT's database; nothing changed"
    case $'\n'"$volumes"$'\n' in
        *$'\n'"${OPSPILOT_PROJECT}_pgdata"$'\n'*) VOLUME=present ;;
        *) VOLUME=absent ;;
    esac
}

# A database volume belongs to the secrets it was initialised with; a second
# set would lock every login out of it.
check_install_binding() {
    read_record "$OPSPILOT_PROJECT"
    [ -n "$RECORDED" ] && [ "$RECORDED" != "$SECRETS_DIR" ] || return 0
    lookup_volume
    [ "$VOLUME" = present ] || return 0
    die "project $OPSPILOT_PROJECT's database was installed with secrets in $RECORDED; set OPSPILOT_SECRETS_DIR=$RECORDED (or remove its data first: uninstall --purge-data with that directory)"
}

# resolve_settings [image]: "image" when this run pulls and starts the
# product image.
resolve_settings() {
    IMAGE_IN_USE=0
    [ "${1:-}" != image ] || IMAGE_IN_USE=1
    [ "$(id -u)" != 0 ] || die "run as a dedicated non-root user, not root"
    local project_hint="${OPSPILOT_PROJECT:-${COMPOSE_PROJECT_NAME:-opspilot}}"
    valid_project "$project_hint"
    # Checked before anything is created; again below for a remembered value.
    [ -z "${OPSPILOT_WEB_HOST_PORT:-}" ] || valid_port "$OPSPILOT_WEB_HOST_PORT"
    [ -z "${OPSPILOT_IMAGE:-}" ] || valid_image "$OPSPILOT_IMAGE"
    SECRETS_DIR="${OPSPILOT_SECRETS_DIR:-}"
    if [ -z "$SECRETS_DIR" ]; then
        read_record "$project_hint"
        SECRETS_DIR="${RECORDED:-${XDG_CONFIG_HOME:-$HOME/.config}/opspilot}"
    fi
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
    valid_project "$OPSPILOT_PROJECT"
    check_install_binding
    export OPSPILOT_WEB_HOST_PORT="${OPSPILOT_WEB_HOST_PORT:-18080}"
    valid_port "$OPSPILOT_WEB_HOST_PORT"
    export OPSPILOT_IMAGE="${OPSPILOT_IMAGE:-$LOCAL_IMAGE}"
    valid_image "$OPSPILOT_IMAGE"
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
    local record
    record="$(install_record "$OPSPILOT_PROJECT")"
    mkdir -p "$(dirname "$record")"
    printf '%s\n' "$SECRETS_DIR" > "$record.tmp"
    mv "$record.tmp" "$record"
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
# A substitution inside an argument would hide its failure; read it first.
owner_pgpass() {
    local password
    password="$(cat "$SECRETS_DIR/postgres_password")" || die "cannot read postgres_password"
    [ -n "$password" ] || die "postgres_password is empty"
    pgpass_line opspilot_owner "$password"
}
login_pgpass() {
    local password
    password="$(random_hex)" || die "could not generate a password for $1"
    [ ${#password} -eq 64 ] || die "could not generate a password for $1"
    pgpass_line "$1" "$password"
}
# Hashes on the host (standard-library script), so no image is needed yet.
credential_entry() {
    command -v python3 > /dev/null || die "python3 is required to hash the generated credentials"
    python3 -I "$REPO_ROOT/opspilot/deploy/credentials.py" "$1" "$2" < "$SECRETS_DIR/$3"
}
placeholder_env() {
    printf '# %s: KEY=value lines read by the %s (see deploy/compose/compose.yaml)\n' "$1" "$2"
}

# Every file ensure_secrets writes.
readonly SECRET_FILES=(postgres_password owner_pgpass web_pgpass worker_pgpass
    observer_pgpass retention_pgpass ui_password alertmanager_token
    web_ui_users web_event_tokens worker_env observer_env)

# Secrets are generated only for a database that does not exist yet: against
# a live volume a regenerated password no longer matches its role (or
# silently changes the operator's), so a missing or empty file is refused
# before anything is built, written or started.
require_secrets_for_live_volume() {
    lookup_volume
    [ "$VOLUME" = present ] || return 0
    local name missing=()
    for name in "${SECRET_FILES[@]}"; do
        [ -s "$SECRETS_DIR/$name" ] || missing+=("$name")
    done
    [ ${#missing[@]} -eq 0 ] ||
        die "project $OPSPILOT_PROJECT's database volume exists but ${missing[*]} in $SECRETS_DIR is missing or empty; restore it from a backup (it is never regenerated for an existing database), or remove the data first with uninstall --purge-data"
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
    write_once worker_env placeholder_env worker_env "worker (DEEPSEEK_API_KEY; OPSPILOT_OTEL_* for otel-demo)"
    write_once observer_env placeholder_env observer_env "observer (Prometheus credentials)"
    export_auth_revision
}

# Identifies the credential set on every principal; a hash of hashes.
export_auth_revision() {
    local digest
    digest="$(cat "$SECRETS_DIR/web_ui_users" "$SECRETS_DIR/web_event_tokens" | sha256)" ||
        die "cannot read web_ui_users / web_event_tokens"
    export OPSPILOT_AUTH_REVISION="auth-${digest:0:12}"
}

sha256() {
    if command -v sha256sum > /dev/null; then sha256sum; else shasum -a 256; fi
}

# -- steps ---------------------------------------------------------------------

# The [project] version of pyproject.toml, its single source (#394); read
# without tomllib so an older host python3 is enough.
product_version() {
    local version
    version="$(awk '/^\[/ { section = $0 } section == "[project]" && /^version *=/ {
        gsub(/^version *= *"|" *$/, ""); print; exit }' "$REPO_ROOT/pyproject.toml")" ||
        die "cannot read pyproject.toml"
    [ -n "$version" ] || die "pyproject.toml has no [project] version"
    printf '%s' "$version"
}

prepare_image() {
    if [ "$OPSPILOT_IMAGE" = "$LOCAL_IMAGE" ]; then
        local version revision
        version="$(product_version)" || exit 1
        # A checkout without git metadata (an unpacked archive) still builds.
        revision="$(git -C "$REPO_ROOT" rev-parse HEAD 2> /dev/null)" || revision=unknown
        log "building $LOCAL_IMAGE $version ($revision) from $REPO_ROOT"
        docker build --tag "$LOCAL_IMAGE" \
            --build-arg "OPSPILOT_VERSION=$version" \
            --build-arg "OPSPILOT_REVISION=$revision" \
            --file "$REPO_ROOT/Dockerfile" "$REPO_ROOT"
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
    resolve_settings image
    require_secrets_for_live_volume
    prepare_image
    ensure_secrets
    save_settings
    deploy
}

cmd_upgrade() {
    resolve_settings image
    [ -s "$SECRETS_DIR/postgres_password" ] || die "no install found in $SECRETS_DIR; run install first"
    require_secrets_for_live_volume
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
