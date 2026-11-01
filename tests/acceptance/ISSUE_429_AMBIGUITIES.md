# Issue #429 acceptance ambiguities

- The repository's established opt-in for temporary PostgreSQL acceptance runs
  is `M1_DURABLE_POSTGRES=1`; the brief also names the M0_B_POSTGRES lab
  concept, but does not define a separate environment variable for it.
- “Business tables untouched” is checked as unchanged row counts for every
  public `opspilot_*` table except `opspilot_trace_outbox`. The contract does
  not require a full row-by-row snapshot, and the outbox table is explicitly
  handled as the Q18 exception.
- The “newer than current” CLI case is represented with the known `0024` target
  while the scratch database is at `0023`; the current migration build has no
  known revision above `0024` to use as a second target.
- The downgrade success assertion treats the Q18 counts as operator-visible
  stderr text, following the public seam. It checks the three counts and never
  constrains formatting beyond the specified line anchors.
- Compose equality uses YAML parsing of `origin/main` and removes only
  `services.worker.stop_grace_period` and `services.observer.stop_grace_period`;
  YAML anchor expansion is therefore compared by its parsed values.
