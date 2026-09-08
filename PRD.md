# OpsPilot — Product Requirements

Status: product boundary confirmed on 2026-09-06; C3 technical design reviewed and approved for persistence on 2026-09-07; quantitative acceptance calibration remains for M0. No implementation is complete.

## Purpose and users

Build an effective, continuously running read-only operations investigator for on-call engineers. The job-search portfolio demonstrates the whole Agent lifecycle through real-software simulation, observable outcomes, reliability and actual improvements over upstream.

`SPEC.md` owns current scope and constraints. `feature_list.json` is the single source of executable acceptance steps; this document describes user capabilities without duplicating those steps. Before implementation, the reviewed acceptance baseline must be frozen. All active features remain unpassed.

## Active capabilities

First-release capability boundaries and source-grounded investigation design are maintained in SPEC.md. Symptom categories belong only to `docs/testing/initial-investigation-coverage.md`; they do not define product admission, prompt routing or separate implementations. In acceptance references, a support matrix means integrations, context availability and permissions; evaluation coverage is maintained separately. Exact runtime validation and numeric acceptance review remain pending.

### F1: Domain Contracts and Evaluation Harness

Define observable incident outcomes, versioned scenarios, independent scoring and reproducible comparisons.

Acceptance: `feature_list.json` entry `F1`.

### F2: Durable Incident Runtime

Maintain resumable incidents across duplicate signals, concurrent work, cancellation and dependency failures.

Acceptance: `feature_list.json` entry `F2`.

### F3: Evidence-First Read-Only Investigation

Actively gather and challenge evidence, maintain hypotheses and make uncertainty visible.

Acceptance: `feature_list.json` entry `F3`.

### F6: Independent Recovery Observation

Observe recovery after human handling using independent service evidence; never execute recovery actions.

Acceptance: `feature_list.json` entry `F6`.

### F7: Read-Only Security and Audit

Enforce target-scoped permissions, query limits, hostile-input handling and reconstructable incident audit.

Acceptance: `feature_list.json` entry `F7`.

### F8: Continuous Operability

Operate the Agent with external health detection, budgets, durable recovery, version management and sustained evidence.

Acceptance: `feature_list.json` entry `F8`.

### F9: Reproducible Deployment and Delivery

Deliver the complete read-only lifecycle with repeatable installation, versioned releases and operational documentation.

Acceptance: `feature_list.json` entry `F9`.

### F11: Post-Release Investigation

Observe deployed versions and delayed regressions without approving, blocking or executing releases.

Acceptance: `feature_list.json` entry `F11`.

### F12: Human Interaction and Incident Follow-Up

Provide a usable incident interface for evidence inspection, follow-up, corrections and controlled handoff.

Acceptance: `feature_list.json` entry `F12`.

### F13: Postmortems and Reviewed Knowledge

Produce evidence-linked incident reviews and promote reusable knowledge only after human confirmation.

Acceptance: `feature_list.json` entry `F13`.

### F14: Upstream Baseline and Capability Mapping

Use HolmesGPT as primary reference and preferred reuse candidate; prove actual gaps before committing to custom work.

Acceptance: `feature_list.json` entry `F14`.

## Scope revision and history

F4 (action broker), F5 (rollout executor), and F10 (autonomy promotion) are retired by the user-confirmed scope decision, not completed. IDs are never reused. F6 now observes recovery after human handling; F7 enforces read-only access; F9 delivers the complete read-only workflow. Original descriptions and all original checks are preserved in `docs/archive/pre-readonly-scope-2026-09-06/` with a change mapping.

## Scope refinement — 2026-09-07

Backup and catastrophic disk-loss recovery are excluded by user instruction. The F8 backup acceptance step is retired, not passed, and preserved in `docs/archive/pre-no-backup-scope-2026-09-07/`. Process/task recovery and upgrade compatibility remain required.

## Completion

All approved steps for every active feature must pass with evidence. The system must support the entire incident and post-release investigation workflow, interaction, recovery observation, postmortem review, reliable deployment and operation. Simulation does not establish production proof. The approved technical plan selects the initial workload and configuration candidates; exact compatibility, capacity and quantitative thresholds require M0 evidence under `SPEC.md`.
