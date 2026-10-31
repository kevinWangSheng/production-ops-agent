# Issue 414 acceptance-test ambiguities

- The contract names `prometheus.extraScrapeConfigs` but does not fix whether
  the chart value is a YAML string or a native list. The test accepts either
  representation and checks the rendered scrape-job content.
- The contract says existing lab rules must remain unchanged, without defining
  whether byte-for-byte YAML formatting is part of that requirement. The test
  checks the existing rule's alert name, expression identity, duration, labels,
  and annotations rather than whitespace or key order.
- Alertmanager's matcher spelling (`match`, `match_re`, or `matchers`) is a
  chart/config-version detail. The test accepts the three equivalent forms but
  requires the exact `opspilot_self="true"` match and `opspilot-self` receiver.
- A stop-worker drill may record either `DEADLINE_EXCEEDED` or the explicitly
  allowed worker-return sweep. The summary test accepts either evidence shape.
- `summary.json` is opt-in lab evidence and is skipped while absent, as the
  issue requests; once present, its two drill names and required outcomes are
  mandatory.
