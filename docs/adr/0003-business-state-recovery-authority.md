---
status: accepted
date: 2026-09-07
---

# PostgreSQL business records own recovery; graph checkpoints are rebuildable caches

OpsPilot must preserve evidence, human control and bounded work across worker failure. The user-reviewed C3 design makes committed PostgreSQL business records the sole cross-process recovery authority: stable ModelStep inputs/responses, tool operations and evidence rebuild each fresh execution attempt. LangGraph remains a loop candidate; its checkpoint cannot override those records or authorize continuation from an old epoch.

## Basis and trade-offs

[Runtime source research](../research/runtime-selection-2026-09-07.md) established that graph checkpointing does not transact with external queries or the application's evidence/control state. The earlier proposal used conditionally accepted checkpoint pointers and cross-epoch fork/import; the [adversarial review](../reviews/technical-design-c3-review-2026-09-07.md) exposed overlapping recovery authority and incomplete input reconstruction. C3 replaces that approach with application-owned, versioned input snapshots and committed steps.

This requires more explicit domain recovery logic and reduces the original benefit attributed to LangGraph. M0 must test whether its remaining orchestration benefit justifies retaining it. Exactly-once external queries are not promised: an uncommitted result may require a bounded repeat, while committed observations retain their original collection time.

## Consequences

Human control, lease/epoch and subject ownership condition every accepted submission. Incident recovery and normal release observation have independent subject identities and observation authorization; old results cannot silently acquire new authority. Version-incompatible continuation blocks for explicit migration or a new Run rather than silently changing semantics.

See the [complete technical plan](../design/technical-proposal-2026-09-07.md) for contracts, model adapter choices and M0–M3 verification. The decision is accepted as design, not as runtime proof or permission to deploy. Acceptance flags remain unchanged.
