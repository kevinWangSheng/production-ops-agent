# Issue #393 contract ambiguities

These points remain open in the agreed seams and are intentionally not made stricter by the acceptance tests:

1. The exact terminal-state vocabulary is not named. The live test rejects only clearly active states and accepts any other recorded terminal value.
2. The exact response schema for the Alertmanager webhook is not part of the #393 contract. The opt-in test uses the public loopback route, extracts the returned incident and Run IDs, and authenticates the public incident page with the generated operator credential.
3. The synthetic alert's required label names, Prometheus fixture contents, and model prompt are open. The live test uses a minimal Alertmanager-shaped payload with stable synthetic labels.
4. The evidence timestamp format and the precise image/version identifier formats are open; tests require non-empty values without imposing a serialization format.
5. The contract does not specify live-test cleanup, retry/backoff, or the exact mechanism that makes the report visible. The live gate polls the incident page until it exposes a terminal Run state and a published report; an optional Docker network attachment is controlled by `OPSPILOT_393_DOCKER_NETWORK` for lab wiring.
