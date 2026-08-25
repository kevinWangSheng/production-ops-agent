# Production Ops Agent — Initial Specification

Status: **Draft for future discussion**
Working title: **OpsPilot**
Initial triage: **ready-for-agent** for specification refinement only
Implementation gate: **Human review required**

## Problem Statement

An engineer who wants to build a production-oriented operations Agent as a high-quality job-search portfolio project needs a scope that demonstrates more than a chat interface or unrestricted tool calling. Existing Agent demos can gather telemetry and call Kubernetes tools, but they often do not prove durable 24×7 operation, evidence-backed diagnosis, least-privilege authorization, idempotent execution, independent recovery verification, replayable evaluation, or safe degradation.

The project needs to be small enough for one engineer to complete, while still showing credible SRE, distributed-systems, Agent-runtime, security, evaluation, and product judgment. It must not claim general autonomous production operation without evidence.

## Solution

Build an always-on, evidence-first Kubernetes release incident Agent for a single service family and a narrow class of release regressions. It automatically joins alerts and deployment events into a durable incident, gathers read-only evidence from metrics, logs, traces, Kubernetes state, deployment history, and source changes, and renders a structured Evidence Packet containing facts, hypotheses, counter-evidence, uncertainty, and a typed ActionProposal.

The model process never owns production credentials. An external Action Broker resolves targets, applies policy, obtains incident-scoped approval, acquires short-lived identity, and delegates only versioned actions to an existing rollout controller. An Independent Verifier determines success from final state, user-facing SLI, traffic integrity, dependencies, and a sustained observation window. Every input, decision, approval, action, and result is durable, auditable, replayable, and evaluable.

The first credible product level is L2 always-on investigation plus L3 approval-scoped execution. A later L4 milestone may automate only the rollback of a stateless service when a release-regression scenario has passed explicit replay, adversarial, fault-injection, rollback, and soak gates.

## User Stories

1. As an on-call engineer, I want related alerts and deployment events joined into one incident, so that I am not forced to investigate duplicate pages independently.
2. As an on-call engineer, I want investigation to start automatically from Alertmanager or rollout events, so that useful evidence is available before I open the incident.
3. As an on-call engineer, I want the Agent to show user impact, affected service, region, tenant, severity, and data freshness, so that I can quickly judge urgency and scope.
4. As an on-call engineer, I want a unified incident timeline containing alerts, deployments, configuration changes, traffic changes, dependency failures, scaling events, and human actions, so that event ordering is explicit.
5. As an on-call engineer, I want every claimed fact linked to its source query, time window, summary, and snapshot, so that I can verify the claim instead of trusting generated prose.
6. As an on-call engineer, I want supporting and contradicting evidence shown separately, so that correlation is not presented as causation.
7. As an on-call engineer, I want rejected hypotheses retained, so that the Agent and later responders do not repeat the same investigation.
8. As an on-call engineer, I want the Agent to state missing telemetry, permission gaps, stale context, and uncertainty, so that I know when human investigation is still required.
9. As an on-call engineer, I want the Agent to abstain when evidence is insufficient or conflicting, so that confidence is not fabricated under incident pressure.
10. As an on-call engineer, I want a compact handoff containing current state, strongest evidence, in-flight work, and the next safe decision, so that I can take over immediately.
11. As an incident commander, I want facts, inferences, and recommendations rendered as distinct sections, so that stakeholders do not confuse an unverified hypothesis with an observed fact.
12. As an incident commander, I want every proposed mitigation to name an exact immutable resource identity and revision, so that approval cannot accidentally target the wrong environment.
13. As an incident commander, I want to see action preconditions, expected benefit, risk, blast radius, stop condition, verification window, and rollback behavior, so that approval is informed and bounded.
14. As an approver, I want approval bound to one incident, action type, target, parameter set, and expiry time, so that approval cannot be reused for a different operation.
15. As an approver, I want a mandatory dry-run or deterministic plan preview before mutation, so that the concrete production impact is visible.
16. As an approver, I want high-risk or out-of-scope action classes denied regardless of model confidence, so that a prompt cannot expand authority.
17. As a platform security engineer, I want read and write identities separated, so that an investigator compromise does not grant mutation privileges.
18. As a platform security engineer, I want execution credentials injected only by an external broker and never placed in model context, so that secrets are not exfiltrated through prompts or traces.
19. As a platform security engineer, I want logs, tickets, code, runbooks, and tool output treated as hostile input, so that passive prompt injection cannot directly authorize production changes.
20. As a platform security engineer, I want global, environment, service, and action-class freeze controls, so that autonomous or approved activity can be stopped immediately.
21. As a platform engineer, I want each action to carry an idempotency key and resource lease, so that retries, duplicate approvals, or concurrent Agents cannot execute the same rollback twice.
22. As a platform engineer, I want the deterministic executor to use Argo Rollouts or another existing rollout state machine, so that the Agent does not reimplement traffic shifting, locking, timeout, history, or rollback semantics.
23. As a platform engineer, I want the workflow to resume from a checkpoint after Agent, model-provider, or worker failure, so that 24×7 incidents do not restart from scratch.
24. As a platform engineer, I want bounded retries, circuit breakers, dead-letter handling, and investigation budgets, so that outages do not produce infinite loops or unlimited cost.
25. As a platform engineer, I want alert storms prioritized and backpressured by severity, service, and error-budget impact, so that the Agent does not overload observability systems during the worst incident.
26. As an SRE, I want the verifier to check rollout state, request volume, error rate, latency, pod health, dependencies, and a sustained window, so that command success is not confused with service recovery.
27. As an SRE, I want ambiguous or missing verification data to escalate rather than pass, so that traffic disappearance or telemetry failure cannot produce a false recovery claim.
28. As an SRE, I want the Agent to degrade to deterministic runbooks and human handoff when the model provider is unavailable, so that the operational control plane remains useful.
29. As an Agent engineer, I want model, prompt, tool schema, policy, runbook, and dataset versions attached to every run, so that regressions can be reproduced.
30. As an Agent engineer, I want historical incidents and live fault scenarios replayed against new versions, so that quality changes are measured before deployment.
31. As an evaluator, I want diagnosis, evidence quality, unsupported claims, action safety, execution, and final-state verification scored separately, so that a good narrative cannot hide an unsafe action.
32. As an evaluator, I want adversarial logs, telemetry manipulation, missing data, stale ownership, wrong targets, duplicate events, and concurrent incidents in the corpus, so that common production failure modes are exercised.
33. As a maintainer, I want the Agent itself instrumented with traces, metrics, structured logs, costs, queue depth, stuck-run detection, and tool errors, so that its own operational health is visible.
34. As a maintainer, I want a reproducible local Kubernetes demo that injects a bad release and completes investigation, approval, rollback, and verification, so that the full system can be reviewed without private infrastructure.
35. As a maintainer, I want Helm-based deployment, versioned configuration, migration rollback, health checks, and upgrade documentation, so that deployment quality is part of the product rather than an afterthought.
36. As a hiring manager, I want published evaluation methodology, raw results, failure cases, and explicit limitations, so that I can distinguish engineering evidence from a polished demo.
37. As a hiring manager, I want to see the workflow recover after a worker is killed mid-incident, so that the 24×7 claim has runtime evidence.
38. As a hiring manager, I want a clear trust-boundary and threat-model explanation, so that the candidate demonstrates production judgment rather than only model integration.
39. As a hiring manager, I want a five-minute product demo and a deeper architecture walkthrough, so that both product outcome and technical decisions are easy to assess.
40. As a future contributor, I want domain contracts and project decisions documented with small interfaces, so that modules can evolve without spreading production semantics throughout the codebase.

## Implementation Decisions

- The initial environment is Kubernetes, not a generic multi-cloud operations platform.
- The initial incident family is a stateless service regression associated with a recent rollout: elevated 5xx, latency regression, readiness failure, or CrashLoop.
- The primary event sources are Alertmanager signals and rollout/deployment change events.
- The initial read adapters cover metrics, logs, traces, Kubernetes state/events, rollout history, and source/configuration change context.
- The Agent defaults to read-only L2 investigation. L3 allows a human-approved action. L4 is a separately promoted capability for a narrow, pre-proven rollback scenario.
- The Agent process has no production write credential and cannot submit free-form shell, SQL, or Kubernetes commands for execution.
- The system uses an Evidence Packet as the durable product record. Chat, Markdown, Slack, and HTML are projections of that record.
- The Evidence Packet includes incident identity, impact, timeline, evidence ledger, hypotheses, counter-evidence, causality, uncertainty, executed actions, proposed actions, and handoff state.
- The core module interfaces exchange five domain records: EvidencePacket, ActionProposal, PolicyDecision, ExecutionReceipt, and VerificationResult.
- Incident Runtime is a deep module responsible for normalization, deduplication, durable workflow state, checkpointing, retry budgets, backpressure, and handoff.
- Investigation is a deep module responsible for planning read-only queries, collecting evidence, maintaining hypotheses, detecting insufficient evidence, and producing an Evidence Packet.
- Action Broker is the authority module. Its interface accepts a typed proposal and returns allow, deny, or approval-required decisions. Target resolution, policy, approval, identity, leases, rate limits, and blast-radius enforcement remain inside it.
- Executor accepts only a broker-approved, versioned action and returns an execution receipt. Argo Rollouts owns rollout state transitions and rollback semantics.
- Verifier is independent from the investigator and executor. It evaluates final state, SLI, traffic integrity, dependencies, and sustained observation windows.
- All mutable operations require explicit resource identity, preconditions, idempotency, lease, dry-run, timeout, audit, verification, rollback, and escalation semantics.
- The provisional implementation split is Python for investigation and evaluation, Go for the security-critical broker/executor, Temporal for durable workflows, PostgreSQL for event/evidence/audit state, OPA for policy, Argo Rollouts for progressive delivery, and OpenTelemetry plus Prometheus/Loki/Tempo/Grafana for observability.
- The provisional stack is a discussion item, not an authorization to add all dependencies immediately. Each seam becomes real only when at least two adapters or an operational isolation requirement justifies it.
- PostgreSQL JSONB and append-only events are the initial persistence approach. Kafka, a graph database, and a vector database are not required for the first milestone.
- Every run records the model, prompt, tool schema, policy, runbook, configuration, and evaluation dataset version.
- The product uses fail-closed semantics for missing approval, missing policy context, ambiguous target resolution, expired identity, missing verification data, or conflicting concurrent action.
- The public maturity statement is production-shaped and fault-injection tested. Production-proven remains unavailable until real production operation supplies that evidence.

## Testing Decisions

- The proposed highest acceptance seam is **IncidentScenario -> IncidentOutcome**. A scenario supplies system state, events, evidence sources, permissions, failures, and expected allowed outcomes; the result contains the Evidence Packet, decisions, receipts, verification, audit, and final environment state. This seam is pending user confirmation during the next design discussion.
- Acceptance tests verify externally observable behavior. They do not assert private model reasoning, chain-of-thought, internal prompt ordering, framework-specific graphs, or implementation call counts.
- Deterministic assertions own identity, target, policy, exact action parameters, idempotency, lease behavior, state transition, final state, and forbidden-action checks.
- Model-assisted grading may assess report clarity or investigation trajectory, but it cannot be the sole oracle for safety, execution, or recovery.
- The evaluation corpus separates detection, localization, RCA, evidence quality, mitigation selection, policy decision, execution, and final-state verification.
- The corpus includes clean and noisy versions of rollout regression, configuration regression, CrashLoop, dependency latency, missing telemetry, telemetry manipulation, concurrent incidents, stale context, and malicious log/ticket content.
- Prior art for scenario design includes AIOpsLab's capability separation, SREGym's live failures/noise/concurrency/metastability, Evidra Bench's final-state and path verification, Google SRE's golden-data/nightly-eval model, and LogInject-style hostile telemetry.
- Unit tests cover pure domain validation and policy predicates through module interfaces.
- Contract tests cover each read adapter, action adapter, policy adapter, approval adapter, identity adapter, and verifier adapter against recorded fixtures and local fakes.
- Integration tests run the durable workflow, PostgreSQL state, policy engine, and rollout controller together.
- End-to-end tests deploy a real sample workload in kind or k3d, inject a bad release, generate real telemetry, trigger an incident, require approval, execute rollback, and verify recovery.
- Fault-injection tests kill workers, interrupt the model provider, duplicate alerts and approvals, delay tools, expire leases, and corrupt or withhold telemetry.
- Security tests attempt prompt injection, target confusion, approval replay, action substitution, credential exposure, policy bypass, stale data use, and cross-incident action reuse.
- Soak testing exercises at least 72 hours of triggers, retries, provider degradation, cost budgets, and workflow recovery before making a 24×7 readiness claim.
- Proposed portfolio acceptance thresholds are tracked as project targets, not industry SLAs: zero forbidden writes in the adversarial suite, zero duplicate actions in idempotency/concurrency tests, complete audit coverage for every action, p95 alert-to-first-useful-evidence at or below two minutes in the lab, and explicitly published RCA/evidence quality results on held-out scenarios.
- Failed tests and unsupported claims remain publishable evidence; they must not be removed from the corpus to improve headline results.

## Out of Scope

- Open-ended L5 autonomous operations.
- Arbitrary shell, SQL, cloud CLI, or unrestricted Kubernetes execution.
- Database schema or data migrations.
- Credential, IAM, certificate, secret, network-boundary, firewall, or security-policy mutation.
- Multi-region failover and broad disaster-recovery actuation.
- Stateful database recovery.
- Generic multi-cloud support in the first milestones.
- Building a replacement observability platform, incident-management platform, workflow engine, policy engine, or rollout controller.
- An extensive chat or dashboard UI before the evidence and safety loop works end to end.
- Training a foundation model.
- Claiming production-proof, universal RCA correctness, or generalized autonomous remediation from a local benchmark.

## Further Notes

- The project is primarily intended to demonstrate AI infrastructure, Agent platform, backend infrastructure, SRE platform, and developer-infrastructure engineering ability.
- The strongest portfolio narrative is not “the model can use kubectl”; it is “a probabilistic investigator is safely composed with deterministic authorization, execution, verification, and replay.”
- Current research supports progressive authorization, no ambient access, mandatory dry-run, circuit breakers, independent post-actuation guardians, red-button controls, and rolling evaluation against golden operational data.
- Live Agent benchmarks show that noise, correlated incidents, concurrent failures, and metastable conditions materially affect end-to-end performance; benchmark results must not be treated as a production SLA.
- Passive prompt injection and telemetry manipulation are first-class threat scenarios, not optional security extras.
- Decisions still awaiting future discussion: final project name, target job profile, one-language versus split implementation, exact workflow runtime, UI surface, first demo workload, acceptance thresholds, and whether/when a public GitHub repository should be created.
- No issue tracker was configured when this draft was created. A local issue packet carries the required `ready-for-agent` label but external publication remains blocked until the user selects a tracker or runs the project skill setup workflow.
