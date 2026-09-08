# Superseded release-content proposal

Historical only. Replaced after the user clarified that symptom categories belong to test coverage, not product routing or admission.

## First complete release — content proposal

Status: proposed release detail for review on 2026-09-06, within the confirmed boundary above. This is not a new multi-version roadmap or a claim of upstream/runtime support. The capability map must validate this proposal before the exact support matrix and numeric acceptance baseline are frozen. Do not silently remove capabilities if the baseline exposes gaps; report the evidence and revise the concrete proposal.

### Operating envelope and entry points

Proposed envelope: one registered Kubernetes environment and a bounded set of HTTP services plus their observable direct dependencies. One operator/team context; cross-cluster and tenant-isolation products are excluded. Exact software, service count, connectors and deployment topology remain design choices. A dependency may be stateful, but observing dependency degradation does not promise database-internal diagnosis or recovery.

Both incident alerts without a recent release and observed post-release behavior are included in the same release. A follow-up can be started from the authenticated incident interface; it does not create a general-purpose infrastructure chat product. Normal releases and healthy periods are supported outcomes, not incidents to manufacture.

### Proposed incident support matrix

- **S1: Service startup or availability failure.** Investigate repeated exits/CrashLoop, failed readiness and unavailable serving instances using workload state, events, application logs and service observations. Determine the affected service/instances and the best-supported observable mechanism. Do not promise root-cause diagnosis of every startup exception.
- **S2: Increased request failures.** Investigate HTTP failures and upstream timeouts using request metrics, application logs, dependency observations and change context. Include both a genuine version/configuration regression and a dependency failure unrelated to any release.
- **S3: Increased request latency.** Investigate response-time degradation using traffic/error/latency observations, resource metrics and available dependency spans or equivalent evidence. Initial candidate mechanisms are CPU pressure/throttling and observable downstream latency; exhaustive network or database query-plan diagnosis is excluded.

Version/configuration changes, resource pressure and dependency degradation are candidate causes, not separate unlimited incident families. Their exact injectors and expected outcomes must be pinned with the workload. Delayed post-release degradation is required; memory growth/OOM is a candidate scenario, not a promise to locate arbitrary memory leaks in source code.

For every supported family, define the observable symptom, permitted sources, required signals, accepted localization/causal evidence, handoff conditions and recovery checks. Evaluate release-related, release-unrelated and no-release cases as appropriate; do not use the presence of a release as the boundary between product versions.

### Complete user-facing behavior

1. Receive the signal and open or update an incident with exact environment/service identity, time window and change context.
2. Acquire evidence actively, test competing hypotheses and show progress. Preserve unavailable sources and uncertainty; separate investigative failure from an uncertain finding.
3. Present impact, timeline, ranked supported hypotheses, counter-evidence, links and concrete next checks or human handling recommendations. Recommendations cannot be executed by this product.
4. Allow authenticated follow-up, added facts, corrections, pause/cancel/takeover, close/reopen and review of prior history. New signals update the incident without overriding human control.
5. Record external human handling and independently observe service recovery over time. Traffic disappearance, missing telemetry or a closed alert alone cannot confirm recovery.
6. Generate the postmortem draft and let a human approve/reject reusable knowledge updates with provenance, version and reversal history. Draft text never silently becomes trusted guidance.

A single usable authenticated operator surface is sufficient; no particular UI framework or external chat integration is mandated. Persist incident state, evidence snapshots/references, reports and reviewed knowledge; make current and historical records inspectable.

### Engineering content and release evidence

The first complete release includes all active feature IDs in PRD/feature_list.json: F14 upstream baseline mapping; F1 independent evaluation/contracts; F2 reliable incident runtime; F3 investigation; F11 post-release checks; F12 human workflow; F6 recovery observation; F13 reviewed postmortems; F7 enforced read-only security; F8 continuous operation; F9 deployment and delivery. Existing acceptance steps remain unchanged by this content proposal.

The verification suite includes healthy behavior, missing/stale/contradictory evidence, unavailable permissions, malicious input, duplicate/unrelated signals, worker restart, late completion after cancellation, tool/provider failure, query budgets, delayed degradation and false recovery. These are required cross-cutting behaviors, not future add-ons. Implementations and environments may be reused or ported, but their actual behavior must be checked.

Deliver a pinned upstream/candidate comparison, repeatable environment and traffic/fault setup, documented deployment/upgrade/restore, an evidence-linked incident walkthrough, held-out quality/failure results, runtime/cost measurements, and the retained minimum 72-hour lab soak. Numeric efficacy, false-positive/abstention, delay, resource and cost limits remain to be set before acceptance. This document is proposed product content, not a fully specified executable release contract.

### Deliberate limits

Do not promise arbitrary incident RCA, root cause down to a code line, arbitrary database/network diagnosis, additional clouds or broad integrations. For unsupported or under-observed problems, provide observed impact, evidence gaps and human handoff. The complete release covers a bounded matrix well; it does not claim general SRE replacement.
