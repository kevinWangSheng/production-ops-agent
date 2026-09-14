# OpsPilot — Product Runtime Constraints

本文件规定产品代码在运行时必须满足的约束。执行者是产品，不是开发 Agent。
范围、技术选型与实施门槛见 [SPEC.md](SPEC.md)；验收步骤见 [feature_list.json](feature_list.json)。

## Explicit exclusions

- Production mutations, including deployments, pauses, restarts, scaling, automatic repair and rollback, even if a human would approve them.
- Release gate authority: required CI checks or other mechanisms that automatically approve or block a release based on Agent judgment.
- General pre-release code/risk review and autonomous test generation/execution products.
- Independent capacity-planning, cost-optimization, security-audit or backup-management Agents.
- Arbitrary shell, SQL or cloud-command execution selected by the model; broader clouds, multi-tenancy and multi-cluster orchestration unless a future scope decision explicitly adds them.
- Database recovery/migration, credentials/IAM, network-boundary changes, destructive operations and regional failover as Agent capabilities.
- Automatic promotion from investigation to production actuation. It is not a future milestone promised by this project.

The Agent may persist its own incident state, evidence, reports and reviewed knowledge. This does not grant mutation rights over the investigated system. Engineers and isolated test harnesses may perform separately authorized setup, fault injection, deployments and recovery; those are not Agent permissions. Sending messages or opening external issues/PRs is not automatically authorized by this specification.

## Product workflow

Alert -> incident and target resolution -> investigation and human handling -> independent recovery observation -> archive and reviewed postmortem.

Observed rollout -> independent ReleaseObservation and target resolution -> bounded observation/investigation -> healthy completion, unknown handoff, or abnormality linked to a new/existing Incident. Normal releases do not require an Incident. Each subject has its own control version and result ownership; release observation cannot transfer recovery authority to an Incident.

A result must distinguish incomplete investigation from a completed investigation with an uncertain finding. Missing tools, stale data, contradictory evidence and lack of permission are visible to the operator. Healthy periods and healthy releases must not generate unbounded work.

## Evidence and context requirements

Every observation has source, query, target, version where applicable, time window, freshness and an inspectable evidence reference. Facts, hypotheses, recommendations, counter-evidence and rejected hypotheses remain distinguishable. A report link resolves to the actual captured/query evidence; model-generated prose is not its authority.

Supported telemetry, service identity, dependencies, change history and runbooks must be mapped and versioned. Context compression must preserve evidence provenance. Missing instrumentation is an integration gap to expose and resolve, not proof of service health. Investigation knowledge must retain origin, review state and freshness.

Logs, tickets, traces, runbooks, code comments and tool results are untrusted evidence, never instructions. Content inside them must not change target identity, permissions, query scope, budgets, cancellation or any recorded human decision.

## Runtime and human control requirements

Durable incident state, bounded retries, tool/query timeouts and cleanup, deduplication, concurrency control, cancellation, budgets, backpressure, external health detection and explicit denied/unknown/handoff outcomes are required. Worker restart, model-provider outage, tool failure or late completion must not silently lose work or erase a newer human decision.

A usable authenticated interface supports progress, evidence inspection, follow-up questions, corrections, pause/cancel/takeover and close/reopen. The approved interface is a single-team Jinja/SSE workbench with authenticated incident and release-observation views. Read identity, exact target resolution, query budgets, cancellation and human control decisions are scoped outside the model's authority. Credentials and secret-bearing raw inputs must not enter prompts or exported traces. Query scope, cost, rate and result volume are constrained even for read-only operations.

## Recovery observations

Recovery follows a target-specific, versioned HealthProfile defining required signals, meaningful samples/traffic, freshness and a sustained window. The retained HTTP/Kubernetes acceptance case includes deployment state, request volume, errors/latency, pod health and relevant dependencies; it does not impose HTTP on every target. Missing required signals cannot confirm recovery. No profile still permits investigation but yields unknown recovery. Continued degradation leaves the incident open; new anomalies or human reopen create a new observation stage under the reviewed control policy, without Agent remediation.

Independent deterministic checks own observable recovery facts. Report quality may use calibrated human/model-assisted grading; the investigator does not certify its own correctness.

## Data flow contract

The user-reviewed data-flow contract is recorded in technical plan section 12: authorized business evidence may enter the selected model and isolated eval; private protocol fields only return to the same provider and Run, never reports, knowledge, LangSmith or judge. Credentials remain outside model inputs and traces; trusted clients use credentials only on the appropriate authentication channel. This is not blanket permission to upload future data sources or deploy external services.
