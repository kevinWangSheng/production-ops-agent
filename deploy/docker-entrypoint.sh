#!/bin/sh
# OpsPilot image entrypoint (#392). Each variable below that the runtimes read
# only from the environment may instead be given as <NAME>_FILE, a per-service
# secret file mounted by Compose; its content is exported as <NAME> and
# <NAME>_FILE is dropped, so the secret is never part of the container
# configuration. Values are never printed. Then exec the role's command.
set -eu

for name in \
    OPSPILOT_UI_USERS \
    OPSPILOT_EVENT_TOKENS; do
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

# trim <text>: sets $trimmed to <text> without surrounding whitespace.
trim() {
    trimmed=${1#"${1%%[![:space:]]*}"}
    trimmed=${trimmed%"${trimmed##*[![:space:]]}"}
}

# The worker's private env file (OPSPILOT_ENV_FILE, the worker_env secret)
# also holds the investigation-side OTel settings, which the tool profile
# reads only from the environment (opspilot/tools/otel_demo.py), and the
# LangSmith project, endpoint and workspace id the trace export reads
# (opspilot/tracing.py, opspilot/outbox_delivery.py; #428), and the read-only
# cluster identity's OPSPILOT_K8S_* (A1, #553), and the Elasticsearch/
# OpenSearch address and es-ro credential OPSPILOT_ES_* (B1, #560), and the
# knowledge tool switch OPSPILOT_KNOWLEDGE_TOOL (C2b, #566), and the task list
# switch OPSPILOT_TODO_WRITE (C1, #565), and the SQL address and sql-ro
# credential OPSPILOT_SQL_* (C6a, #570), and the Kafka address and kafka-ro
# settings OPSPILOT_KAFKA_* (C3, #567), and the Helm release switch
# OPSPILOT_HELM_RELEASES (B4, #563), and the MongoDB address and
# mongodb-ro settings OPSPILOT_MONGODB_* (C6c, #570); export those and
# nothing else (worker_main reads the model and
# LangSmith keys from the file itself). Same reading as worker_main._read_env_file: per KEY, the first
# KEY=value line wins; line and value are stripped and one pair of matching
# quotes removed.
env_file=${OPSPILOT_ENV_FILE:-}
if [ -n "$env_file" ]; then
    if [ ! -r "$env_file" ]; then
        echo "opspilot-entrypoint: OPSPILOT_ENV_FILE is not a readable file" >&2
        exit 66
    fi
    imported=" "
    while IFS= read -r line || [ -n "$line" ]; do
        trim "$line"
        name=${trimmed%%=*}
        [ "$name" != "$trimmed" ] || continue
        case "$name" in
            OPSPILOT_OTEL_PROMETHEUS_URL | OPSPILOT_OTEL_JAEGER_URL | \
                OPSPILOT_OTEL_TOKEN | OPSPILOT_OTEL_PROMETHEUS_USERNAME | \
                OPSPILOT_OTEL_PROMETHEUS_PASSWORD | LANGSMITH_PROJECT | \
                LANGSMITH_ENDPOINT | LANGSMITH_WORKSPACE_ID) ;;
            OPSPILOT_K8S_* | OPSPILOT_ES_* | OPSPILOT_LOKI_* | OPSPILOT_VICTORIALOGS_* | \
                OPSPILOT_SQL_* | OPSPILOT_MONGODB_* | OPSPILOT_KAFKA_* | OPSPILOT_KNOWLEDGE_TOOL | \
                OPSPILOT_TODO_WRITE | OPSPILOT_HELM_RELEASES)
                # a name, never shell text: it reaches eval below
                case "$name" in *[!A-Z0-9_]*) continue ;; esac
                ;;
            *) continue ;;
        esac
        case "$imported" in *" $name "*) continue ;; esac
        eval "inline=\${${name}:-}"
        if [ -n "$inline" ]; then
            echo "opspilot-entrypoint: ${name} is set both inline and in OPSPILOT_ENV_FILE" >&2
            exit 64
        fi
        trim "${trimmed#*=}"
        case "$trimmed" in
            \"*\" | \'*\')
                trimmed=${trimmed#?}
                trimmed=${trimmed%?}
                ;;
        esac
        export "$name=$trimmed"
        imported="$imported$name "
    done < "$env_file"
fi

exec "$@"
