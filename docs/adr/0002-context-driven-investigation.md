---
status: accepted
date: 2026-09-07
---

# Shared context-driven investigation with bounded execution

The user confirmed a shared investigation loop that selects authorized tools from the request, available context and evolving evidence, with on-demand access to relevant reviewed knowledge. Test symptom categories must not become product admission rules, fixed prompt-routing keys or hardcoded investigation paths. This preserves exploration across overlapping symptoms and causes while keeping permissions, budgets, cancellation, durable state and independent outcome checks enforced by the system.

## Basis and trade-offs

[Cross-project source analysis](../research/investigation-design-basis-2026-09-06.md) compares HolmesGPT, OpenSRE, Stratus and K8sGPT at pinned revisions. Shared loops and optional specialist knowledge coexist upstream; no claim is made that specialized workflows are universally inferior. Compared with predetermined symptom routing, dynamic investigation permits changing direction as evidence develops but requires control of cost, latency and incomplete conclusions.

## Consequences

- Context is assembled and updated from the request, target/environment identity, available sources, observations and relevant reviewed knowledge; it is not a one-time bundle of logs.
- Skills and deterministic analyzers may support investigation where justified by the task and measured benefit. No one-skill-per-test-category implementation is prescribed.
- Distinguish supported findings, uncertainty, incomplete investigation, budget termination and human handoff. Model termination alone does not certify correctness or recovery.
- Architectural inspiration may come from multiple projects; HolmesGPT remains a preferred reuse candidate, not a mandatory design template. Every material custom choice needs its problem, evidence/analysis, trade-offs and validation plan.
- Exact framework, language, persistence, model, knowledge retrieval, tool discovery and any additional reviewer or agent topology remain undecided. No extra classifier or multi-agent architecture is implied.
- This accepts the mechanism, not its runtime effectiveness or the complete implementation design. The SPEC implementation gate remains unchanged, and all acceptance flags remain false.

Subsequent decision (2026-09-07): the user-reviewed [C3 technical plan](../design/technical-proposal-2026-09-07.md) records concrete technology direction; [ADR-0003](0003-business-state-recovery-authority.md) records recovery ownership. The undecided technologies above describe the state when ADR-0002 was accepted, not the current selection status.
