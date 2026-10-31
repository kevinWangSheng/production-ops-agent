# Issue #393 contract ambiguities

These points remain open in the agreed seams and are intentionally not made stricter by the acceptance tests:

1. The exact terminal-state vocabulary is not named. The evidence test rejects only clearly active states and accepts any other recorded terminal value.
2. The workbench URL, authentication details, and the exact response schema for the Alertmanager webhook are not part of the #393 contract. The opt-in test uses the agreed loopback route and bearer token file, then validates the installed evidence record.
3. The synthetic alert's required label names, Prometheus fixture contents, and model prompt are open. The live test uses a minimal Alertmanager-shaped payload with stable synthetic labels.
4. The evidence timestamp format and the precise image/version identifier formats are open; tests require non-empty values without imposing a serialization format.
5. The contract does not specify live-test cleanup, retry/backoff, or the exact mechanism that makes the report visible. The live gate waits for the agreed summary artifact and does not prescribe those implementation details.
