# Issue #396 contract ambiguities

These tests intentionally avoid assigning semantics that the ticket and the
F14 contract do not settle:
- The exact environment allowlist for `container.run_args` is not named. The
  tests require the documented `HTTPS_PROXY` behavior and reject known
  telemetry, Kubernetes, Docker, and host credential names without inventing a
  larger allowlist.
- No public seam or evidence schema defines how an exclusive kind-lab window,
  outgoing thinking/effort request inspection, or the isolation probe is
  represented. Those live-run checks remain opt-in evidence requirements and
  are not guessed in PR-CI tests.
- The pin record's key names are not fixed. Tests accept the documented names
  and small semantic aliases while requiring the pinned upstream commit and
  `reproducible: false`.
