---
title: "Draft the evidence-first production operations Agent"
labels:
  - ready-for-agent
state: draft
implementation_gate: human-review-required
---

# Draft the evidence-first production operations Agent

This is a local issue packet created because the project does not yet have a configured remote issue tracker. It is ready for an Agent to refine the specification, but not ready for feature implementation until the human review gate is cleared.

## Problem Statement

A job-search portfolio project needs to demonstrate a credible 24×7 operations Agent rather than an unrestricted Kubernetes tool-calling demo. The project must prove evidence-backed investigation, durable workflows, external production authority, deterministic rollback, independent verification, replayable evaluation, hostile-input handling, and explicit limits.

## Solution

Build a narrow Kubernetes release incident Agent that automatically investigates rollout regressions and produces an Evidence Packet. It may propose an exact rollback, but an external broker owns target resolution, policy, scoped approval, identity, lease, and blast-radius enforcement. Argo Rollouts owns execution semantics, and an independent verifier owns the recovery decision.

The complete conversation-derived specification, user stories, implementation decisions, testing decisions, and out-of-scope list are maintained in `SPEC.md` and should be copied into the configured issue tracker after project setup.

## Proposed Acceptance Seam

Use one high external seam: `IncidentScenario -> IncidentOutcome`. Validate evidence, policy, execution, audit, and final environment state without asserting private model reasoning.

## Review Required Before Implementation

- Project name and target job profile.
- First workload and release-regression scenario.
- Acceptance thresholds.
- Technology split and dependency budget.
- Public versus private repository and tracker.
- L3 approval experience and future L4 promotion policy.
