---
status: accepted
date: 2026-09-06
---

# Complete read-only investigation instead of production actuation

The user confirmed a complete, production-standard Agent lifecycle covering incident investigation, post-release investigation, human follow-up, recovery observation and reviewed postmortems. The earlier draft included an action broker, rollback execution and later autonomy promotion; these would require a separate authority and side-effect system and divert the project from proving useful, reliable investigation. We choose a read-only product boundary with no production mutations or release-gate authority; completeness comes from effective investigation and the full delivery/operation/feedback lifecycle, not from granting write access.

## Consequences

- The Agent may persist its own state/evidence and reviewed knowledge; humans independently handle the investigated environment.
- F4/F5/F10 are retired, not passed. F6 retains recovery observation without action execution. Original checks are preserved in `../archive/pre-readonly-scope-2026-09-06/`.
- Pre-release general review, autonomous test-generation products and other operations Agents are excluded. Broader authority needs a new explicit scope decision.
- Scope is accepted; technical schemas, stack, numeric targets and acceptance details remain review-pending. This ADR does not clear implementation or authorize external operations.
- HolmesGPT is the agreed primary comparison direction and preferred reuse candidate. Exact fork/integration and specific fixes remain evidence-led engineering decisions, not frozen architecture.
