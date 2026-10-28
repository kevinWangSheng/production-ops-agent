# Issue #396 contract ambiguities

These tests intentionally avoid assigning semantics that the ticket and the
F14 contract do not settle:

- The exact dictionary schema returned by `holmes_config` is unspecified. The
  tests accept a mapping or list under `toolsets`, and use the documented
  `enabled`, host, path, and method semantics.
- The exact environment allowlist for `container.run_args` is not named. The
  tests require the documented `HTTPS_PROXY` behavior and reject known
  telemetry, Kubernetes, Docker, and host credential names without inventing a
  larger allowlist.
- The input schema for `project_outcome(holmes_output, proxy_log_lines)` is not
  specified. The tests use the observable `status`, `report`, `tool_calls`, and
  `model_calls` fields described by the run outcome and assert only the output
  contract and query counting rule.
- No public seam or evidence schema defines how an exclusive kind-lab window,
  outgoing thinking/effort request inspection, or the isolation probe is
  represented. Those live-run checks remain opt-in evidence requirements and
  are not guessed in PR-CI tests.
- The pin record's key names are not fixed. Tests accept the documented names
  and small semantic aliases while requiring the pinned upstream commit and
  `reproducible: false`.
