---
title: "Finalize the read-only investigation Agent design"
labels:
  - ready-for-agent
state: design
implementation_gate: human-review-required
---

# Finalize the read-only investigation Agent design

Local tracking packet; not published to an external tracker. The user confirmed the product boundary on 2026-09-06: incident and post-release investigation, ongoing human follow-up, recovery observation and reviewed postmortems. No production writes or release-gate authority.

Current requirements live in `../SPEC.md`, user capabilities in `../PRD.md`, and exact acceptance checks in `../feature_list.json`. Do not copy the full specification into this issue and create another source of truth. The old packet is preserved in `../docs/archive/pre-readonly-scope-2026-09-06/`.

Next: map upstream capabilities and reproducible gaps, propose support matrix, read-only contracts, numeric targets, realistic simulation, deployment resources and integration design. Review that concrete packet before feature implementation. The ready-for-agent label currently means design/refinement only.
