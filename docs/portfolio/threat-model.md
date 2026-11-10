# Threat model

Scope: the Compose install in [deploy/compose/](../../deploy/compose/) (main as of 2026-11-10, release `v0.1.1`) on one control host, investigating one target through read-only telemetry. The rules the product must satisfy are in [PRODUCT-CONSTRAINTS.md](../../PRODUCT-CONSTRAINTS.md); this page lists what enforces each rule today, the evidence for it, and what is not enforced. It has not been reviewed by a security specialist and the product has never faced a real production environment ([limitations](limitations.md)).

## Assets

- The investigated system: it must not be changed by the agent.
- Credentials: the model API key, target read credentials (Prometheus, Jaeger), the Observer's Prometheus account, four database logins and the owner password, the workbench password and the intake bearer tokens.
- Business telemetry the agent reads (metrics, traces, error messages) and what it stores: evidence, reports, postmortems, knowledge.
- Human decisions: pause, cancel, close, reopen, approvals. A later agent result must never overwrite them.

## Trust boundaries

| Boundary | Who is on the other side | Trusted for |
|---|---|---|
| Alertmanager / event intake → web | Anything that holds an intake token | Opening incidents only; alert text is untrusted context |
| Browser → workbench | Operators with the UI password | Human controls and reviews |
| Worker → model provider (DeepSeek) | External SaaS | Nothing; receives authorized evidence, returns untrusted text |
| Worker / Observer → target telemetry | Prometheus, Jaeger | Data only; tool results are untrusted evidence |
| Worker → LangSmith (opt-in) | External SaaS | Receives ids, versions, token counts and fixed codes only |
| Host user / Docker → containers | Whoever administers the control host | Everything (see residual risks) |

## Threats and controls

| # | Threat | What enforces the control | Evidence | Gap |
|---|---|---|---|---|
| T1 | The agent changes the investigated system (constraint: no production mutations) | The model only sees five read tools (`opspilot/tools/`); no shell, SQL or write tool exists. In the lab, product accounts reach Prometheus through a read-only proxy that rejects writes | F7 proxy probe 136/136 (writes, admin, remote write 403): [f7-authz-probe/run.md](../evidence/f7-authz-probe/run.md); F3 step 6 probe [f3-step6-authz-probe](../evidence/f3-step6-authz-probe/probe.json) | Read-only enforcement at the data source is a deployment duty. Outside the lab, nothing stops an operator from giving the worker a credential with write rights. The lab's Jaeger query API has no authentication |
| T2 | Prompt injection through alerts, traces or logs changes target, scope, budget or a human decision | Target identity, query scope, budgets and control generation are resolved outside the model (executor, DB fences); alert text is marked untrusted context | F7 hostile-input Runs: [f7-hostile-input-live](../evidence/f7-hostile-input-live/); F1 prompt-injection scenarios in the [F1 results](../results/f1-same-condition-comparison-2026-11.md) | Report text is not protected. In the F1 dev exam one OpsPilot attempt repeated an injected canary in its report (vetoed by the deterministic `q20.canary_absent` check; see the results page) |
| T3 | Credentials leak into prompts, reports or exported traces | Data-source responses are redacted before they become evidence (`opspilot/redaction.py`); credentials are never tool parameters; trace export sends no payloads | F7 PR-B real Runs 20/20 ([task record](../tasks/2026-10-15-f7-hostile-input.md)) | Known redaction gaps: other encodings [#359](https://github.com/kevinWangSheng/production-ops-agent/issues/359), quadratic case [#365](https://github.com/kevinWangSheng/production-ops-agent/issues/365), false positives [#366](https://github.com/kevinWangSheng/production-ops-agent/issues/366) |
| T4 | Telemetry leaves to third parties beyond what was authorized (data-flow contract, C3 §12) | Evidence goes only to the configured model provider. LangSmith export is off by default and, when on, carries ids, versions, token counts and fixed codes (`opspilot/outbox_delivery.py`) | Outbox payloads read back from LangSmith: [243-trace-outbox](../evidence/243-trace-outbox/README.md) | The worker container has unrestricted outbound network access; no egress allow-list ships. Business telemetry is sent to DeepSeek by design |
| T5 | A compromised or buggy component reaches beyond its role | Separate database logins per runtime with explicit grants and role-guard triggers (migration 0011, `opspilot/deploy/roles.py`); the Observer has its own Prometheus account and never loads the model key | Runs under non-owner roles with 0 permission errors: [m1-pg-roles-live](../evidence/m1-pg-roles-live/) | The worker holds both the model key and the target credentials and runs the tool executor in-process; C3 §3 Controller / Tool Gateway separation is not built ([#197](https://github.com/kevinWangSheng/production-ops-agent/issues/197)) |
| T6 | An unauthenticated caller opens incidents or operates the workbench | Intake needs a bearer token (stored as digests); the workbench uses named Basic Auth with PBKDF2 hashes and an Origin check on state changes (`opspilot/web/auth.py`); the workbench port binds to `127.0.0.1` | F9 clean-host run: operator 200, anonymous 401 ([f9-steps-2-4](../evidence/f9-steps-2-4-2026-11-10/README.md)) | No TLS proxy ships (C3 §9 names one); remote access relies on an SSH tunnel. Single team, no SSO, no per-incident authorization |
| T7 | A late or duplicate agent result overwrites a human decision or a newer Run | Leases, execution epochs and control generations fence every write (C3 §4, §6–7; [ADR-0003](../adr/0003-business-state-recovery-authority.md)) | Worker kill on the clean host: [f9-steps-2-4](../evidence/f9-steps-2-4-2026-11-10/README.md) step 3; [f2-kill-resume-live](../evidence/f2-kill-resume-live/README.md); pause tests in F7 step 4 | A budget reservation interrupted by a kill stays `reserved` ([#527](https://github.com/kevinWangSheng/production-ops-agent/issues/527)); cost accounting, not control |
| T8 | Runaway cost or load on the target | Per-Run model-request cap, wall-clock deadline, per-call timeouts, result-size limits, backlog cap (`opspilot/investigation/limits.py`); pause stops new queries | 72h lab soak cost and stuck-work counts: [m3-soak-2026-11-07](../evidence/m3-soak-2026-11-07/README.md) | No global spend cap; cost is reconciled after the fact |
| T9 | Secrets exposed on the control host | Secrets are generated once into `0600` files outside the repository; containers run as the non-root installing user, read-only root filesystem, all capabilities dropped (`compose.yaml`); no secret in compose or image | Clean install evidence: [f9-clean-install](../evidence/f9-clean-install-2026-11-09/README.md), [f9-392-compose-install](../evidence/f9-392-compose-install/README.md) | Anyone with root or `docker` group access on the control host can read every secret, including the model key |

## Residual risks a deployer must own

- Supplying read-only credentials for the target and a read-only path to each data source.
- Putting TLS and network policy in front of the workbench and restricting the worker's outbound traffic.
- Deciding what telemetry may be sent to the model provider.
- Treating the control host's administrators as fully trusted.
