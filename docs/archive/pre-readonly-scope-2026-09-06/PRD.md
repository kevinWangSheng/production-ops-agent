# Production Ops Agent — Product Requirements Document

Status: Draft; implementation requires human review.

## Problem Statement

Build a job-search portfolio project that credibly demonstrates how an always-on operations Agent can investigate Kubernetes release incidents and participate in rollback without bypassing production reliability, security, audit, and verification requirements.

## Target Users

- On-call engineers and incident commanders.
- SRE and platform engineering teams.
- Platform security and governance engineers.
- Agent engineers maintaining prompts, tools, models, and evaluations.
- Hiring managers evaluating production Agent engineering ability.

## Core Features

### F1: Domain Contracts and Evaluation Harness

**Description**: Define the incident, evidence, proposal, policy, receipt, verification, and outcome contracts together with replayable clean, noisy, adversarial, and fault-injection scenarios.
**User Story**: As an evaluator, I want stable scenario and outcome contracts so that every version is judged against external behavior.
**Acceptance Criteria**:

- [ ] The `IncidentScenario -> IncidentOutcome` acceptance seam is documented and reviewable.
- [ ] The corpus separates detection, localization, RCA, evidence, action safety, execution, and verification.
- [ ] Initial scenarios include bad rollout, missing telemetry, duplicate events, provider failure, prompt injection, and wrong target.
- [ ] Dataset, prompt, model, tool, policy, and runbook versions are recorded.

**Priority**: High

### F2: Durable Incident Runtime

**Description**: Normalize signals into durable incidents with deduplication, checkpoints, retries, budgets, leases, backpressure, and recovery.
**User Story**: As an on-call engineer, I want investigation to survive duplicate alerts and process failure so that the incident remains coherent.
**Acceptance Criteria**:

- [ ] Alertmanager and rollout events join the correct incident.
- [ ] Repeated delivery does not create duplicate incidents or actions.
- [ ] Workflow resumes from durable state after worker termination.
- [ ] Timeouts, retry budgets, circuit breakers, and dead-letter behavior are externally visible and testable.

**Priority**: High

### F3: Evidence-First Read-Only Investigation

**Description**: Query metrics, logs, traces, Kubernetes state, rollout history, and source changes to produce a structured Evidence Packet with hypotheses, counter-evidence, uncertainty, and handoff state.
**User Story**: As an on-call engineer, I want every RCA claim backed by inspectable evidence so that I can trust or challenge it.
**Acceptance Criteria**:

- [ ] Every observation records source, query, time window, freshness, and evidence reference.
- [ ] Facts, inferences, counter-evidence, rejected hypotheses, and recommendations are distinguishable.
- [ ] The Agent abstains when evidence is missing or contradictory.
- [ ] Investigation uses read-only identity and exposes no execution credential.

**Priority**: High

### F4: Typed Action Proposal and Policy Broker

**Description**: Convert remediation intent into a typed proposal and apply external target resolution, policy, approval, identity, lease, budget, and blast-radius controls.
**User Story**: As an approver, I want approval bound to one exact action so that model output cannot expand the authorized operation.
**Acceptance Criteria**:

- [ ] A proposal includes immutable target, parameters, preconditions, dry-run, risk, blast radius, verification, rollback, and expiry.
- [ ] The model process has no mutation credentials.
- [ ] The broker returns allow, deny, or approval-required with a durable reason.
- [ ] Missing or ambiguous context fails closed.

**Priority**: Medium

### F5: Deterministic Rollout Execution

**Description**: Execute only approved, versioned pause, abort, or known-good rollback actions through Argo Rollouts or an equivalent controller.
**User Story**: As a platform engineer, I want an existing rollout state machine to own production transitions so that the Agent cannot improvise deployment semantics.
**Acceptance Criteria**:

- [ ] Executor rejects free-form commands and unregistered action versions.
- [ ] Every action is idempotent, lease-protected, scoped, and time-bounded.
- [ ] Execution returns a durable receipt containing resolved target and actual controller operation.
- [ ] Duplicate approval or retry cannot duplicate the mutation.

**Priority**: Medium

### F6: Independent Final-State Verification

**Description**: Verify service recovery independently from the model and executor by checking rollout state, SLI, traffic, pods, dependencies, and a sustained observation window.
**User Story**: As an SRE, I want recovery judged by independent system state so that successful commands and lost traffic do not produce false success.
**Acceptance Criteria**:

- [ ] Verification checks deployment state, request volume, errors, latency, pod health, and dependencies.
- [ ] Missing or conflicting telemetry cannot pass.
- [ ] Failure triggers rollback, freeze, or human escalation according to the action contract.
- [ ] Verification results are durable and replayable.

**Priority**: Medium

### F7: Security, Governance, and Audit

**Description**: Enforce hostile-input handling, least privilege, short-lived identity, freeze controls, immutable audit, and explicit human handoff.
**User Story**: As a security engineer, I want the Agent treated as untrusted software so that prompt injection cannot become production authority.
**Acceptance Criteria**:

- [ ] Read, approval, execution, and verification identities are separable.
- [ ] Secrets never enter model context or Agent traces.
- [ ] Global, environment, service, and action-class freezes are enforceable.
- [ ] Inputs, evidence, proposals, policy, approval, execution, verification, and rollback are reconstructable from audit state.

**Priority**: Medium

### F8: 24×7 Operability

**Description**: Instrument and operate the Agent runtime with health, SLO, cost, queue, provider, tool, stuck-run, upgrade, backup, and disaster-recovery controls.
**User Story**: As a maintainer, I want the Agent itself operated like production software so that it does not become an invisible on-call dependency.
**Acceptance Criteria**:

- [ ] Runtime exposes traces, metrics, structured logs, queue depth, budget, and stuck-run signals.
- [ ] Provider failure degrades to deterministic behavior and human handoff.
- [ ] Versioned configuration and migrations have tested rollback paths.
- [ ] A 72-hour lab soak publishes failures, cost, recovery, and availability evidence.

**Priority**: Medium

### F9: Reproducible Portfolio Demo and Delivery

**Description**: Provide a reproducible local cluster, bad-release injector, telemetry stack, end-to-end demo, Helm deployment, documentation, and evidence artifacts.
**User Story**: As a hiring manager, I want to reproduce the full incident loop so that project quality is independently assessable.
**Acceptance Criteria**:

- [ ] One documented command creates the environment and runs the primary incident scenario.
- [ ] The demo visibly covers investigation, Evidence Packet, approval, execution, verification, and audit.
- [ ] Killing a worker during the demo proves durable recovery.
- [ ] README, architecture, threat model, operations, evaluation, limitations, and demo video are provided.

**Priority**: Low

### F10: Narrow L4 Promotion Gate

**Description**: Promote only a stateless known-good rollback scenario from approval-scoped L3 to policy-scoped L4 after explicit safety and reliability evidence.
**User Story**: As an SRE owner, I want autonomy granted progressively so that only proven scenarios operate without synchronous approval.
**Acceptance Criteria**:

- [ ] Promotion requires replay, adversarial, fault-injection, verifier, rollback, and soak thresholds approved by the user.
- [ ] L4 authorization is revocable and downgradeable at runtime.
- [ ] Any elevated risk or anomalous state automatically falls back to L3.
- [ ] L4 remains limited to one incident family, service class, environment policy, and action type.

**Priority**: Low

## Constraints

- One engineer should be able to complete the first credible vertical slice.
- Production writes must be externally governed and typed.
- Existing rollout, workflow, policy, and observability control planes should be reused where appropriate.
- The product must expose uncertainty and abstain safely.
- The project must not overclaim production proof or general autonomy.

## Non-Functional Requirements

- Durable, resumable, replayable incident workflows.
- Fail-closed security and explicit least privilege.
- Idempotent, lease-protected actions.
- Independent final-state verification.
- Complete action auditability.
- Explicit cost and retry budgets.
- Reproducible deployment, testing, and fault injection.
- Proposed lab target: p95 alert-to-first-useful-evidence at or below two minutes.
- Proposed safety target: zero forbidden writes in the maintained adversarial suite.

## Milestones

### Milestone 0: Contracts and Safety Baseline

- **Target**: Approve product scope, acceptance seam, domain records, evaluation strategy, threat model, and provisional stack.
- **Features**: F1.
- **Acceptance**: Human review resolves the open decisions in `SPEC.md`; no feature implementation is implied by this draft.

### Milestone 1: Read-Only Incident Investigator

- **Target**: A real alert creates a durable incident and a replayable Evidence Packet without production mutation.
- **Features**: F2, F3.
- **Acceptance**: Clean, noisy, missing-data, restart, and hostile-input scenarios pass the approved external acceptance tests.

### Milestone 2: Approval-Scoped Rollback

- **Target**: An exact, approved rollback passes external policy, deterministic execution, independent verification, and audit.
- **Features**: F4, F5, F6, F7.
- **Acceptance**: Duplicate, wrong-target, expired, denied, rollback-failure, and false-success scenarios behave safely.

### Milestone 3: Production-Shaped Operation and Portfolio Delivery

- **Target**: Demonstrate operability, soak evidence, reproducible installation, and a reviewable public narrative.
- **Features**: F8, F9.
- **Acceptance**: Runtime, security, evaluation, limitation, and demo evidence are independently inspectable.

### Milestone 4: Bounded Autonomous Rollback

- **Target**: Optionally promote one proven rollback scenario to policy-scoped L4.
- **Features**: F10.
- **Acceptance**: The user approves the promotion gate after reviewing all safety and reliability evidence.
