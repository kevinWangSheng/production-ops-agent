# Issue #428 acceptance ambiguities

- The brief does not define whether a duplicate root `opspilot.run` span for
  repeated terminal `run_state` rows must be deduplicated by LangSmith; tests
  require stable IDs and leave remote deduplication observational.
- OB-R6 now uses the public `tests.integration.test_m1_loop_resume_postgres.Harness`
  with its `ScriptedModel`/`InvestigationRunner` path, redirected to the test's
  scratch DSN. The delivery thread itself remains separately injectable through
  `OutboxDelivery.deliver_once`; no additional worker-thread harness is needed
  for the endpoint-unreachable assertion.
- OTLP protobuf decoding is intentionally not prescribed. Tests assert the
  stable identity, headers, and absence of forbidden payload values; exact
  protobuf field ordering is left to the OTLP library.
- The contract says backlog drops the oldest rows but does not state whether
  the pending metric is sampled before or after that trim. The metric test
  should use the post-cycle snapshot.
- The install brief says the entrypoint passes `LANGSMITH_PROJECT` and
  `LANGSMITH_ENDPOINT` to the worker only; it does not specify whether that is
  represented as a process environment variable or an env-file export. The
  static test checks the allowlist and service scope, not the transport form.
