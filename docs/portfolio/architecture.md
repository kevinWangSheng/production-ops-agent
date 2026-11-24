# Architecture

What runs, what it owns, and how one incident moves through it. Checked against main as of 2026-11-10 (release `v0.1.1`; the evidence linked here was recorded on `v0.1.0`). The authoritative contract is the Chinese [C3 technical design](../design/technical-proposal-2026-09-07.md); where this page and C3 differ, C3 wins, and the gaps listed under "Where the code differs from C3" are known and tracked.

## Processes

One product image (`Dockerfile`, Python 3.12) runs as separate containers under Docker Compose on a control host ([deploy/compose/compose.yaml](../../deploy/compose/compose.yaml)). The product is not deployed inside the investigated cluster; there is no Helm chart (C3 §12).

| Container | Entry point | What it does | Credentials it holds |
|---|---|---|---|
| `postgres` | official `postgres:17.9`, pinned by digest | Every business record: incidents, Runs, steps, evidence, controls, observations, postmortems, knowledge, trace outbox. No host port. | owner password |
| `migrate`, `init-roles` | `python -m opspilot.schema migrate`, `python -m opspilot.deploy.roles` | One-shot: Alembic migrations with every runtime stopped, then the four runtime logins | owner login only |
| `web` | `python -m opspilot.web` | Workbench (FastAPI + Jinja + SSE), alert/event/version-change intake, human controls, postmortem review. A background loop authorizes recovery observation after a qualified conclusion and runs the release authorize/anomaly passes. Published on `127.0.0.1` only. | `opspilot_web_app` DB login, UI password hashes, intake token digests |
| `worker` | `python -m opspilot.worker_main` | Claims queued Runs and runs the investigation loop (model + read-only tools), the deadline sweep and backlog pass, a postmortem-drafting thread and the trace-outbox thread | `opspilot_worker_app` DB login, model key, target read credentials (`OPSPILOT_OTEL_*`) |
| `observer` | `python -m opspilot.observer` | Deterministic recovery and release sampling against a versioned HealthProfile; never calls a model | `opspilot_observer_app` DB login, its own read-only Prometheus account |
| `retention` | `python -m opspilot.retention --days 30` (profile `maintenance`, run by hand) | Dry-run by default; `--apply` deletes raw payloads 30 days after an incident is closed (#244) | `opspilot_retention_app` DB login |

Each runtime serves `/healthz`, `/readyz` and `/metrics` on container port 9464 (`opspilot/health.py`).

## Investigation path

1. **Intake.** Alertmanager posts to `POST /intake/alertmanager` with a bearer token. The web dedupes the delivery, opens or attaches to an incident, and resolves the target from `target_identities.json` by exact label match. No match: the incident is handed to a human and no Run starts. Alert text enters the Run as untrusted context, not instructions (M1-04).
2. **Claim.** A worker slot claims the Run with a lease and an execution epoch. Every write is fenced on that lease and on the current human control generation, so a second worker, a stale attempt or an overridden decision cannot update the incident (`opspilot/worker_main.py` docstring; C3 §6–7).
3. **Loop.** A hand-written loop (LangGraph was compared and deferred, [ADR-0004](../adr/0004-langgraph-orchestration.md)) calls DeepSeek `deepseek-flash` through an OpenAI-compatible client. Each model round and each tool result is committed before the next begins, so a killed worker resumes from the rows ([ADR-0003](../adr/0003-business-state-recovery-authority.md)). Limits live in `opspilot/investigation/limits.py` (for example 100 model requests per Run, a 2-hour wall clock).
4. **Tools.** The tool set depends on `OPSPILOT_TOOL_PROFILE` (`opspilot/tools/profiles.py`). The default `fixture` profile registers one read-only tool, `metrics_range_query`, over a fixed sample (`opspilot/tools/fixture.py`). The `otel-demo` profile registers five: `metrics_range_query`, `get_metric_names`, `get_label_values` (Prometheus), `traces_search` (Jaeger), and `read_evidence` (row slices of an over-limit result the Run already kept) (`opspilot/tools/otel_demo.py`). The `otel-demo-ext` profile (`opspilot/tools/otel_demo_ext.py`) always registers 19 and adds up to 47 more, one group per configured backend, for at most 66:
   - Always (19): the five `otel-demo` tools; Prometheus `execute_prometheus_instant_query`, `get_series`, `get_all_labels`, `list_prometheus_rules`, `get_prometheus_target`, `get_metric_metadata`; Jaeger `traces_search_by_tags`, `traces_query_trace_by_id`, `traces_list_services`, `traces_list_operations`; Kubernetes `kubernetes_jq_query`, `kubernetes_count`, `kubernetes_tabular_query`, `fetch_pod_logs`.
   - Elasticsearch/OpenSearch, `OPSPILOT_ES_URL` (8): `elasticsearch_search`, `elasticsearch_mappings`, `elasticsearch_list_indices`, `elasticsearch_cat`, `elasticsearch_cluster_health`, `elasticsearch_index_stats`, `elasticsearch_allocation_explain`, `elasticsearch_nodes_stats`.
   - Loki, `OPSPILOT_LOKI_URL` (1): `grafana_loki_query`.
   - VictoriaLogs, `OPSPILOT_VICTORIALOGS_URL` (5): `victorialogs_query`, `victorialogs_streams`, `victorialogs_field_names`, `victorialogs_field_values`, `victorialogs_hits`.
   - Kubernetes live usage, `OPSPILOT_K8S_LIVE_METRICS=1` (2): `kubectl_top_pods`, `kubectl_top_nodes`.
   - Helm, `OPSPILOT_HELM_RELEASES=1` (3): `helm_list`, `helm_history`, `helm_status`.
   - Argo CD, `OPSPILOT_ARGOCD=1` (4): `argocd_app_list`, `argocd_app_get`, `argocd_app_history`, `argocd_app_resources`.
   - Approved runbooks, `OPSPILOT_KNOWLEDGE_TOOL=1` (1): `fetch_skill`.
   - SQL (PostgreSQL, MySQL/MariaDB, ClickHouse, SQL Server), `OPSPILOT_SQL_URL` (3): `database_sql_query`, `database_sql_list_tables`, `database_sql_describe_table`.
   - Kafka, `OPSPILOT_KAFKA_BOOTSTRAP_SERVERS` (8): `list_kafka_consumers`, `describe_consumer_group`, `list_topics`, `describe_topic`, `find_consumer_groups_by_topic`, `list_brokers`, `describe_configs`, `cluster_overview`.
   - MongoDB, `OPSPILOT_MONGODB_URL` (6): `mongodb_find`, `mongodb_aggregate`, `mongodb_list_collections`, `mongodb_collection_schema`, `mongodb_server_status`, `mongodb_current_op`.
   - Redis/Valkey, `OPSPILOT_REDIS_URL` (6): `redis_info`, `redis_config_get`, `redis_scan_keys`, `redis_key_info`, `redis_get_value`, `redis_slowlog`.

   `OPSPILOT_TODO_WRITE` also shows the model a `TodoWrite` task list, which the loop answers itself; it is not a data-source tool. All registered tools go through `ReadOnlyToolExecutor` (`opspilot/tools/executor.py`), which owns target scope, absolute time windows, budgets, result-size limits, pause checks and credential redaction. Each backend uses its own read-only credential; SQL, MongoDB and Redis refuse an address that is one of OpsPilot's own databases. There is no shell or write tool.
5. **Report.** The final report separates facts, hypotheses and gaps, cites evidence ids, and is validated before publishing (a report that fails validation gets one forced retry with itemized feedback, `opspilot/investigation/reports.py`). Outcome is `supported`, `partial` or `inconclusive`; a Run that cannot finish is handed to a human with a reason code ([ADR-0005](../adr/0005-handoff-and-deadline-terminal.md)).
6. **Recovery observation.** After a qualified conclusion (or after a human registers their remediation), observation is authorized. The Observer samples the target's HealthProfile (`opspilot/observer/profiles/otel-demo-checkout.json`) and records `recovery_confirmed` only after a sustained healthy window; missing signals cannot confirm recovery.
7. **Postmortem and knowledge.** The worker drafts a postmortem whose every claim must cite valid evidence. It becomes knowledge for later Runs only after a human approves it in the workbench; approved entries can be revoked.

Release watching follows the same pattern with its own subject: a version-change event opens a `ReleaseObservation`; a regression becomes a new or linked incident. OpsPilot never approves or blocks a release.

Human controls (pause globally or per target, cancel, take over, close, reopen, follow-up questions) are conditional updates that bump a control generation; every claim, budget reservation, tool dispatch and result commit checks it (C3 §4).

## Telemetry about OpsPilot itself

- Prometheus metrics on each runtime (`opspilot_queue_depth`, `opspilot_model_calls_total`, `opspilot_model_consecutive_failures`, build info and others) and structured JSON logs (`opspilot/logs.py`).
- Optional trace export (`OPSPILOT_TRACE=outbox`): the worker writes ids, versions, token counts and fixed codes to an outbox table and a thread delivers them to LangSmith ([ADR-0006](../adr/0006-trace-evidence-and-backlog.md)). Off by default.

## The lab it was built against

Everything was developed and measured against one target: the OpenTelemetry Demo (Helm chart `0.37.8`) on a kind cluster, with its Prometheus, Jaeger, kube-state-metrics, an Alertmanager and a read-only proxy in front of Prometheus, created by [scripts/kind_lab.py](../../scripts/kind_lab.py). Traffic comes from the demo's load generator; faults are flagd feature flags (for example `paymentFailure`) switched by `kind_lab.py fault inject`. See [limitations](limitations.md).

## Where the code differs from C3

- **No separate Controller and Tool Gateway (C3 §3).** The worker process holds both the model key and the target read credentials and runs the tool executor in-process. C3 requires these roles to be isolated by credentials, database rights and network reachability; only the database rights are separated per runtime. Closed as a known limitation ([#197](https://github.com/kevinWangSheng/production-ops-agent/issues/197), C3 §3).
- **No TLS proxy (C3 §9).** C3 specifies Caddy TLS in front of the Basic Auth workbench. The Compose install publishes the workbench on `127.0.0.1` only and remote access goes through an SSH tunnel; no TLS proxy ships.
