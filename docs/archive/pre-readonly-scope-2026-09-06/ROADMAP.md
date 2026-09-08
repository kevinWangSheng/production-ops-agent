# Roadmap

Status: Specification draft; implementation is blocked on human review.

## Review Gate

- [-] Agent-first project decision and full production-standard lifecycle — see `docs/complete-project-brainstorm-2026-09-06.md`. Agent effectiveness and sustained operation are central; simulated workloads and infrastructure are supporting design topics deferred until scope is settled. Authority boundary, supported incidents, and acceptance targets remain open.
- [-] Upstream-led implementation route — HolmesGPT recommended as primary benchmark/base; current source and concrete issue candidates recorded in `docs/research/upstream-led-project-plan-2026-09-06.md`. Runtime reproduction remains pending; product boundary and reuse/fix/extension mapping are the next design decision.
- [ ] Confirm final project name and target job profile.
- [ ] Confirm the highest test seam: `IncidentScenario -> IncidentOutcome`.
- [ ] Confirm the first incident family and sample workload.
- [ ] Confirm the provisional technology split and dependency budget.
- [ ] Confirm lab acceptance thresholds and public-release intent.

## High Priority

- [ ] **F1: Domain Contracts and Evaluation Harness** — Define the acceptance seam, corpus, scoring, and versioned run records.
- [ ] **F2: Durable Incident Runtime** — Normalize, deduplicate, persist, resume, and budget incident workflows.
- [ ] **F3: Evidence-First Read-Only Investigation** — Produce an auditable Evidence Packet from real read-only sources.

## Medium Priority

- [ ] **F4: Typed Action Proposal and Policy Broker** — Resolve and authorize exact actions outside the model process.
- [ ] **F5: Deterministic Rollout Execution** — Delegate approved rollback to a versioned rollout controller action.
- [ ] **F6: Independent Final-State Verification** — Decide recovery from durable system evidence.
- [ ] **F7: Security, Governance, and Audit** — Enforce least privilege, hostile-input treatment, freeze, and replayable audit.
- [ ] **F8: 24×7 Operability** — Instrument, recover, degrade, upgrade, and soak the Agent runtime.

## Low Priority

- [ ] **F9: Reproducible Portfolio Demo and Delivery** — Package the cluster, scenario, docs, Helm deployment, and videos.
- [ ] **F10: Narrow L4 Promotion Gate** — Optionally automate one proven stateless rollback scenario.

## Completed

- [x] Initial conversation-derived specification and project management skeleton — 2026-08-25.

---

Legend: `[ ]` Todo | `[-]` In Progress | `[x]` Completed
