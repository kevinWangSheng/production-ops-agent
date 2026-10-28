#!/bin/sh
# OpsPilot image entrypoint (#392). Each variable below that the runtimes read
# only from the environment may instead be given as <NAME>_FILE, a per-service
# secret file mounted by Compose; its content is exported as <NAME> and
# <NAME>_FILE is dropped, so the secret is never part of the container
# configuration. Values are never printed. Then exec the role's command.
set -eu

for name in \
    OPSPILOT_UI_USERS \
    OPSPILOT_EVENT_TOKENS \
    OPSPILOT_OTEL_TOKEN \
    OPSPILOT_OTEL_PROMETHEUS_USERNAME \
    OPSPILOT_OTEL_PROMETHEUS_PASSWORD; do
    eval "file=\${${name}_FILE:-}"
    [ -n "$file" ] || continue
    eval "inline=\${${name}:-}"
    if [ -n "$inline" ]; then
        echo "opspilot-entrypoint: ${name} and ${name}_FILE are both set" >&2
        exit 64
    fi
    if [ ! -r "$file" ]; then
        echo "opspilot-entrypoint: ${name}_FILE is not a readable file" >&2
        exit 66
    fi
    value=$(cat "$file")
    export "$name=$value"
    unset "${name}_FILE"
done

exec "$@"
