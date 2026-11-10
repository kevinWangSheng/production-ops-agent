# Architecture

What runs, what it owns, and how one incident moves through it. Checked against main as of 2026-11-10 (`v0.1.0` code plus later docs and lab fixes). The authoritative contract is the Chinese [C3 technical design](../design/technical-proposal-2026-09-07.md); where this page and C3 differ, C3 wins, and the gaps listed under "Where the code differs from C3" are known and tracked.

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
4. **Tools.** The model sees five read-only tools: `metrics_range_query`, `get_metric_names`, `get_label_values` (Prometheus), `traces_search` (Jaeger), and `read_evidence` (row slices of an over-limit result the Run already kept). All go through `ReadOnlyToolExecutor` (`opspilot/tools/executor.py`), which owns target scope, absolute time windows, budgets, result-size limits, pause checks and credential redaction. There is no shell, SQL or write tool.
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

- **No separate Controller and Tool Gateway (C3 §3).** The worker process holds both the model key and the target read credentials and runs the tool executor in-process. C3 requires these roles to be isolated by credentials, database rights and network reachability. Tracked in [#197](https://github.com/kevinWangSheng/production-ops-agent/issues/197).
- **No TLS proxy (C3 §9).** C3 specifies Caddy TLS in front of the Basic Auth workbench. The Compose install publishes the workbench on `127.0.0.1` only and remote access goes through an SSH tunnel; no TLS proxy ships.
- **Tools cover Prometheus and Jaeger only.** Kubernetes read-only and log tools are not implemented ([#307](https://github.com/kevinWangSheng/production-ops-agent/issues/307)).
