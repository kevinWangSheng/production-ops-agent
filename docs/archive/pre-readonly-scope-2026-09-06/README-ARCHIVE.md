# Historical draft — superseded on 2026-09-06

These are byte-for-byte snapshots immediately before the user-confirmed read-only scope revision. They are historical evidence, not current instructions or acceptance requirements. `manifest.json` records SHA-256 digests.

The user confirmed incident investigation, post-release investigation, ongoing follow-up, recovery observation and reviewed postmortem knowledge. Production actions and release gates were excluded. No feature had passed before this revision.

Feature history:

- F4 (action broker), F5 (rollout executor), F10 (autonomy promotion) are retired, not completed. Their IDs must not be reused.
- F1/F2/F3 retain their purpose, with contracts and runtime acceptance adapted to the read-only product.
- F6 retains independent recovery verification after human handling; automated rollback/freeze execution is removed.
- F7 retains permission, hostile-input and audit verification; action authority is replaced by enforced read-only boundaries.
- F8 retains operability, provider degradation, upgrade/recovery and 72-hour lab soak requirements.
- F9 retains reproducible delivery and adds the complete read-only lifecycle in place of approval/execution.
- New F11/F12/F13/F14 cover post-release investigation, human interaction, reviewed postmortems and upstream baseline mapping.

This change records an explicitly agreed scope change, not the weakening of a failed acceptance test. All active feature passes remain false; revised acceptance steps require technical review before implementation.

Current authority: ../../../SPEC.md and ../../../docs/adr/0001-readonly-investigation-boundary.md.
