# Production Ops Agent

Working title: **OpsPilot**.

An evidence-first, always-on Kubernetes release incident agent that investigates production regressions, produces auditable root-cause reports, and performs approval-scoped rollback through an external policy broker, deterministic execution plane, and independent verifier.

## Current status

This repository is in **specification draft** state. It contains the decisions already discussed and intentionally does not contain an implementation yet.

- The initial product scope is a single Kubernetes service family and release-regression incidents.
- The default target is L2 investigation plus L3 approval-scoped execution.
- L4 autonomy is a later, narrowly gated milestone.
- L5 open-ended autonomous production access is not a goal.
- The project is production-shaped, not production-proven.

## Read first

1. `SPEC.md` — conversation-derived product and engineering specification.
2. `PRD.md` — feature-oriented product requirements.
3. `ROADMAP.md` — phased work order.
4. `feature_list.json` — machine-readable acceptance inventory.
5. `issues/0001-initial-production-ops-agent-spec.md` — local issue packet awaiting tracker publication.

## Proposed product thesis

The Agent investigates and plans. A deterministic control plane owns authorization, execution, rollback, and final-state verification.

## Review gate

No feature implementation should begin until the user reviews the proposed acceptance seam, project name, target role, technology stack, and first milestone scope.
