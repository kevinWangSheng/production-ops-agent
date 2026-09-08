# Initial investigation evaluation coverage

Status: proposed test planning, 2026-09-06. This file is not a product feature or runtime routing contract. Approved acceptance checks remain in `feature_list.json`; none are removed here.

Initial symptoms for candidate real-software tests:

- Startup/unavailability: crashing workload, failed readiness or no serving instances.
- Request failure: application errors, timeouts or observable dependency failures.
- Latency regression: workload resource pressure or downstream delay.

Each symptom must be paired with multiple possible causes, mixed symptoms, healthy controls and release/no-release context. Test delayed effects and misleading recent changes. The suite must also cover unavailable/stale/conflicting evidence, permissions, provider/tool failure, restart, duplication, cancellation and recovery ambiguity.

The orchestrator, prompt and tools cannot use this catalog's labels or hidden root causes to choose a production investigation path. Only operationally available signals may enter the Agent. Test-only APIs/injectors and ground truth stay outside Agent access.

Assess transfer with held-out cause/topology/context variants and compare base investigation against any optional skill/routing/analyzer changes under matched budgets. Passing this catalog proves only the tested coverage. Additional symptoms within supported integrations may be investigated without adding a symptom-specific implementation.

Exact workloads, fault parameters, repetitions and quantitative thresholds remain to be specified from baseline and independent oracle evidence. This is not an exhaustive or frozen benchmark.
