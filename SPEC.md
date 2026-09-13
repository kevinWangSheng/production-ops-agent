# OpsPilot — Current Specification

Product boundary: **confirmed by the user on 2026-09-06**.
Investigation mechanism: **confirmed by the user on 2026-09-07** (ADR-0002).
Technical design: **C3 reviewed and approved for persistence by the user on 2026-09-07**. Exact compatibility, capacity and quantitative acceptance calibration remain for M0.
Feature implementation gate: **not cleared**; M0 compatibility evidence and the corresponding frozen acceptance packet remain prerequisites. The next work is M0 design validation, not another review of the same architecture. No product implementation or production deployment evidence exists yet. Bounded M0 protocol and real-software environment evidence are recorded separately.

2026-09-10 M0-02 update: real normal/fault investigations now return structured reports, and a related real DeepSeek/PostgreSQL cross-process protocol reconstruction has passed independent checks. The [first-flow v3 acceptance packet](docs/testing/first-investigation-v3-2026-09-10.md) is retained as the historical frozen version; the strict successor is recorded below. **The gate remains not cleared because the actual reports still contain independently confirmed factual/visibility errors; the subsequent projection fixes have only offline evidence.** See the [round result](docs/evidence/m0-real-investigation/round-02-results.md) and [independent entry review](docs/evidence/m0-real-investigation/round-02-final-delivery-gate-review.md). No product feature passes or M1 implementation are authorized by these partial results.

2026-09-10 PR review update: the v3 packet is retained as historical evidence. Review found missing full-report output binding, claim target/time guarantees and final-control consistency at that seam. The [strict v4/report-v2 packet](docs/testing/first-investigation-v4-2026-09-10.md) is frozen after offline implementation and [independent verification](docs/evidence/m0-real-investigation/round-02-pr16-v4-final-review.md) under the [reviewed bounded design](docs/evidence/m0-real-investigation/round-02-pr16-strict-seam-design.md). Legacy records must preserve missing scope/freshness as unknown, not be rewritten to pass. No new real-model evidence or M1 permission follows from this contract repair.

2026-09-12 M0 status: the bounded executable validation items covered by the current M0 plan have been exercised and their evidence is recorded across the exit matrix and task record, including PG control/recovery contracts, one real pause→resume DeepSeek Run, isolated provider protocol probes, deterministic compressor plus one real threshold Run, constrained upstream/replay comparison, trace/wall-time checks and the first small holdout rehearsal in a mount-free container. Remaining user-owned closure is judge manual calibration, supplier billing reconciliation, budget-freeze approval and the Kubernetes environment gap. Product-level gaps remain explicitly listed as partial/unknown (including product streaming/PG audit integration, compressor threshold policy, Holmes host OS isolation and formal held-out evaluation); this status does not open the gate, authorize M1, modify feature passes or expand product permissions. Scope stops at M0; M1 is not authorized. **The feature implementation gate remains not cleared.**


## Purpose

Build an effective, continuously running read-only operations investigation Agent to demonstrate complete Agent engineering for a job-search portfolio. Real-software simulated environments support this goal; operating an unrelated personal application is not a prerequisite.

The two entry points are incident alerts and observed releases. Both use the same investigation, evidence, incident state and human interaction capabilities. The product provides investigation and advisory findings, not authority over production releases.

## Confirmed scope

- Incident investigation: receive alerts, correlate signals, resolve exact targets, actively query permitted sources, test competing hypotheses and report evidence-supported findings or explicit uncertainty.
- Post-release investigation: observe before/after revisions, compare service behavior and follow delayed regressions. Results are advisory and cannot approve, block, promote or revert releases.
- Ongoing follow-up: update the same incident as evidence changes, avoid duplicate investigations, support pause/cancel/takeover/close/reopen, and preserve human corrections against late task completions.
- Recovery observation: after a human independently handles an incident, use read-only service evidence and a sustained window to report recovery, continued degradation or unknown state.
- Postmortems: draft an impact summary, timeline, findings, uncertainty, human handling and recovery evidence; reusable knowledge requires human confirmation and provenance.
- Full engineering lifecycle: integration, secure tooling, reliable runtime, interaction, software tests, evaluation, deployment, upgrades, recovery, observability, budgets, sustained operation and verified improvements.

## First complete release — capabilities and evidence

The complete release includes both incident and post-release entry points and all active PRD/acceptance features. It accepts an investigation request and available context, acquires additional evidence through authorized tools, updates findings, supports human interaction, observes recovery and produces reviewed postmortems. It must not require a caller to select a predefined fault family.

Product capability boundaries are connected environments/data sources, exact target identity, supported query interfaces, permissions, available context and operational budgets. A new symptom within that boundary is not rejected solely because it is absent from the test catalog. Missing access or evidence is reported explicitly; a general investigation mechanism does not prove universal diagnostic accuracy.

The earlier S1/S2/S3 symptom-based support proposal is withdrawn. Startup/availability, request failure and latency are initial evaluation candidates only, maintained in `docs/testing/initial-investigation-coverage.md`. They are not runtime enums, classifier labels, prompt-selection keys or mandatory investigation paths. The earlier one-HTTP-service-pattern restriction had no completed capability analysis and is not a frozen release requirement. The approved technical plan selects OTel Demo as the initial workload; pinned versions, actual source mappings and capacity require M0 evidence.

### Investigation design basis

Design references are not limited to the preferred implementation base. Source-grounded comparisons of HolmesGPT, OpenSRE, Stratus and K8sGPT are recorded in `docs/research/investigation-design-basis-2026-09-06.md`, with exact revisions, observed mechanisms, trade-offs and proposed validation. HolmesGPT remains a reuse candidate, not an authority that all design choices must follow.

Confirmed mechanism (2026-09-07): a shared investigation loop with context assembly, authorized tool discovery, tool observations fed back into context, on-demand retrieval of relevant reviewed domain knowledge, and bounded stopping/handoff. See `docs/adr/0002-context-driven-investigation.md` for the decision and trade-offs. The mechanism is accepted; its runtime effectiveness and exact implementation remain unvalidated.

No symptom-specific prompt routing is adopted. Task-specific skills or deterministic analyzers are not prohibited: introduce them only for an identified information/accuracy/cost problem, with relevant-source context and evidence from comparisons. They must neither receive hidden test answers nor dictate a conclusion against contradictory observations. Any dynamic routing needs a documented benefit, misrouting/fallback analysis and evaluation before adoption.

The human workflow includes progress/evidence inspection, follow-up, correction, pause/cancel/takeover, close/reopen, recording external human handling, independent recovery observation and explicit review of knowledge updates. These are confirmed product requirements; the research loop does not replace durable state, access enforcement or independent outcome checks.

### Release evidence

All active features remain required: F14 upstream mapping, F1 contracts/evaluation, F2 runtime, F3 investigation, F11 post-release observation, F12 interaction, F6 recovery observation, F13 reviewed knowledge, F7 read-only security, F8 operation and F9 delivery. No acceptance step is removed by moving test categories out of product scope.

Release evidence includes pinned baseline/candidate comparisons, reproducible real-software environments, independent outcomes, failure and held-out results, deployment/upgrade/process-recovery checks and sustained operation, including the retained minimum 72-hour lab soak. Numeric quality and resource/cost targets still require baseline evidence and technical review. Neither a source reference nor passing three symptom categories establishes universal or production-proven ability.

## Explicit exclusions

- Production mutations, including deployments, pauses, restarts, scaling, automatic repair and rollback, even if a human would approve them.
- Release gate authority: required CI checks or other mechanisms that automatically approve or block a release based on Agent judgment.
- General pre-release code/risk review and autonomous test generation/execution products.
- Independent capacity-planning, cost-optimization, security-audit or backup-management Agents.
- Arbitrary shell, SQL or cloud-command execution selected by the model; broader clouds, multi-tenancy and multi-cluster orchestration unless a future scope decision explicitly adds them.
- Database recovery/migration, credentials/IAM, network-boundary changes, destructive operations and regional failover as Agent capabilities.
- Automatic promotion from investigation to production actuation. It is not a future milestone promised by this project.

The Agent may persist its own incident state, evidence, reports and reviewed knowledge. This does not grant mutation rights over the investigated system. Engineers and isolated test harnesses may perform separately authorized setup, fault injection, deployments and recovery; those are not Agent permissions. Sending messages or opening external issues/PRs is not automatically authorized by this specification.

## Product workflow

Alert -> incident and target resolution -> investigation and human handling -> independent recovery observation -> archive and reviewed postmortem.

Observed rollout -> independent ReleaseObservation and target resolution -> bounded observation/investigation -> healthy completion, unknown handoff, or abnormality linked to a new/existing Incident. Normal releases do not require an Incident. Each subject has its own control version and result ownership; release observation cannot transfer recovery authority to an Incident.

A result must distinguish incomplete investigation from a completed investigation with an uncertain finding. Missing tools, stale data, contradictory evidence and lack of permission are visible to the operator. Healthy periods and healthy releases must not generate unbounded work.

## Evidence and context requirements

Every observation has source, query, target, version where applicable, time window, freshness and an inspectable evidence reference. Facts, hypotheses, recommendations, counter-evidence and rejected hypotheses remain distinguishable. A report link resolves to the actual captured/query evidence; model-generated prose is not its authority.

Supported telemetry, service identity, dependencies, change history and runbooks must be mapped and versioned. Context compression must preserve evidence provenance. Missing instrumentation is an integration gap to expose and resolve, not proof of service health. Investigation knowledge must retain origin, review state and freshness.

## Runtime and human control requirements

Durable incident state, bounded retries, tool/query timeouts and cleanup, deduplication, concurrency control, cancellation, budgets, backpressure, external health detection and explicit handoff are required. Worker restart, model-provider outage, tool failure or late completion must not silently lose work or erase a newer human decision.

A usable authenticated interface supports progress, evidence inspection, follow-up questions, corrections, pause/cancel/takeover and close/reopen. The approved interface is a single-team Jinja/SSE workbench with authenticated incident and release-observation views. Read identity is scoped outside the model's authority; credentials and secret-bearing raw inputs must not enter prompts or exported traces. Query scope, cost, rate and result volume are constrained even for read-only operations.

## Recovery observations

Recovery follows a target-specific, versioned HealthProfile defining required signals, meaningful samples/traffic, freshness and a sustained window. The retained HTTP/Kubernetes acceptance case includes deployment state, request volume, errors/latency, pod health and relevant dependencies; it does not impose HTTP on every target. Missing required signals cannot confirm recovery. No profile still permits investigation but yields unknown recovery. Continued degradation leaves the incident open; new anomalies or human reopen create a new observation stage under the reviewed control policy, without Agent remediation.

Independent deterministic checks own observable recovery facts. Report quality may use calibrated human/model-assisted grading; the investigator does not certify its own correctness.

## Upstream strategy

- HolmesGPT is the primary comparison and preferred code-reuse candidate.
- OpenSRE supplies secondary product/workflow references; K8sGPT is a simpler analyzer-plus-explanation comparison.
- kagent is a specialized runtime reference, not a mandated dependency.
- AIOpsLab/SREGym and other real-software environments are validation candidates, not the product itself.

Fix an upstream version and actual configuration, run the original, reproduce relevant issues and inspect existing fixes, then map requirements to reuse/configuration/fix/extension. Preserve license and provenance, matched baseline comparisons and an upstream-update strategy. An open issue or a source observation is not a locally reproduced defect. See `docs/research/upstream-led-project-plan-2026-09-06.md`.

Reuse may mean implementing an upstream idea, protocol or code logic in our chosen stack; ready-to-import code is not required. This confirms the comparison direction, not an irrevocable fork/API integration choice. The approved technical plan selects Python/FastAPI, PostgreSQL, a DeepSeek-compatible client, LangSmith and Compose/Helm; LangGraph is the loop candidate whose net benefit remains to be validated. Hosting purchases and exact dependency locks are not selected by this decision. Older unselected-technology statements are superseded for selection status only; scope and permission restrictions remain in force.

## Model priority and design ownership

Confirmed user priority on 2026-09-07: adapt the first implementation to DeepSeek. Keep a replaceable model boundary for future GLM or other providers; this does not require implementing those providers in the first release. User decision on 2026-09-09: use Flash by default and call its explicit alias rather than relying on Pro rerouting. The current validation profile is DeepSeek official Chat Completions-compatible service, deepseek-v4-flash, thinking/high; its availability and exact dependency/protocol compatibility require M0 verification and recorded versions.

Approved implementation approach: shared investigation instructions and output/evidence contracts; provider/model-version capability configuration for protocol differences; minimal prompt adjustments only when official requirements or controlled evaluation justify them. Do not fork a complete investigation prompt or workflow for every vendor by default. Record effective prompt, adapter, model, endpoint/mode and tool-schema versions per run. See `docs/research/model-adaptation-and-design-ownership-2026-09-07.md` for current evidence and responsibilities.

The assistant owns routine implementation details and prepares the M0 validation packet from the approved technical plan and inspected sources. The user reviews consequential trade-offs; routine reversible details are owned by the assistant. Evidence, alternatives, cost and validation accompany material custom choices. Deduplication, storage and scheduling reuse standard mechanisms, while incident correlation, time-sensitive evidence, model/tool recovery and human-decision precedence require explicit product semantics. This does not add symptom-specific workflows or clear the implementation gate.

## Operating constraints

User decisions (2026-09-07): no backup or disaster-recovery system for catastrophic disk loss in the first project scope; assume persistent storage survives process/container restarts. Durable state, process interruption recovery and upgrade compatibility remain required. The retired F8 backup check is preserved in `docs/archive/pre-no-backup-scope-2026-09-07/`; it was not completed.

Use CNY 1,000 as an adjustable initial project-budget planning reference, not a monthly commitment or purchasing authorization. Estimate one-off and recurring infrastructure, model and evaluation costs separately; propose additional spend when needed. Investigation effectiveness takes priority over premature cost optimization, while timeouts and loop/budget monitoring prevent unproductive runaway work.

The user-reviewed data-flow contract is recorded in technical plan section 12: authorized business evidence may enter the selected model and isolated eval; private protocol fields only return to the same provider and Run, never reports, knowledge, LangSmith or judge. Credentials remain outside model inputs and traces; trusted clients use credentials only on the appropriate authentication channel. This is not blanket permission to upload future data sources or deploy external services.

## Verification and delivery

The proposed highest acceptance seam remains `IncidentScenario -> IncidentOutcome`, with a read-only outcome contract still to be detailed. Observe inputs, evidence, states, permissions, human interaction and final service observations; never test hidden chain-of-thought.

Use unit/contract/integration/end-to-end tests, real-software fault injection, security checks, offline/live evaluation and sustained operation. Separate development and held-out cases; isolate injected ground truth from the Agent; compare versions under matched data, permissions and budgets; record model, prompt, code, tool, knowledge, policy and evaluator versions. Publish failures and repeat non-deterministic cases.

The acceptance inventory retains a minimum 72-hour lab soak requirement. It alone does not establish 24x7 readiness or production proof. The old p95 alert-to-useful-evidence target of two minutes remains a proposed target, not an achieved result or an accepted SLA. Numeric quality, tail-latency, cost, resource and recovery thresholds must be set before corresponding implementation/evaluation acceptance.

Deployment must be reproducible and versioned, with CI, staging checks, upgrades, in-flight-state compatibility and an explicit application-version rollback or process-recovery path. Business telemetry, Agent execution traces, runtime health and evaluation feedback must be correlated and operable. LangSmith or another vendor is a tooling choice, not the whole lifecycle.

All active features and approved checks must pass before completion. Claims distinguish static inspection, integrated simulation, fault injection, soak and real production observations. No current feature is implemented or production-proven.

## Next validation work and implementation gate

The approved [technical plan](docs/design/technical-proposal-2026-09-07.md) is the current engineering contract. The [C1–C3 full-review record](docs/reviews/technical-design-c3-review-2026-09-07.md) records independent adversarial review; [ADR-0003](docs/adr/0003-business-state-recovery-authority.md) explains the recovery authority decision. PostgreSQL committed business records own cross-process recovery; graph checkpoints are attempt-local rebuildable caches, not accepted cross-epoch recovery pointers.

Next is M0: pin and validate dependencies/model protocol, persistent reconstruction and cancellation/upgrade behavior; establish workload/data-source/permission mappings and measured capacity; calibrate and freeze the eval packet before candidate assessment. F14 now has bounded real upstream attempts, but successful active fault reporting, complete reuse mapping, live-compatible IncidentScenario/IncidentOutcome contracts, calibrated thresholds and task estimates still require this work. They are explicit validation and acceptance deliverables, not unreviewed product scope or an invitation to repeat the same technical selection.

The reviewed [M0 execution plan](docs/plans/m0-validation-plan-2026-09-07.md) now details experiment coverage, evidence, sequence and decision ownership. Its [isolated-context review](docs/reviews/m0-plan-adversarial-review-2026-09-07.md) closed two planning omissions; persistence does not constitute runtime evidence.

### Conditions for entering implementation

2026-09-09 bounded evidence update: [the real investigation report](docs/evidence/m0-real-investigation/results.md) records one complete Flash fixture/PG/trace chain, pinned OTel/Holmes operation, a qualified normal report, independently confirmed fault and post-restoration observations. Both active fault investigations produced no final report; the final business-evidence handoff exhausted its output allowance without final content. The gate remains **not cleared**. The next bounded packet is [fault-report completion and dynamic evidence acceptance](docs/plans/first-vertical-investigation-2026-09-09.md): calibrate input/output/time/cost together, resolve dynamic visible evidence and real target/dependency identity contracts, and validate the first slice's step reconstruction and current control authority. This does not require completing all future fault cases or product UI before implementation. All feature passes remain unchanged.

The C3 architecture user review is complete. Before starting feature implementation, obtain the relevant M0 evidence, resolve any incompatibility it exposes, and freeze the corresponding acceptance criteria and required environment/resources under this approved design. Low-level reversible implementation details need not all be specified in advance. Design-validation experiments that resolve open questions are separately scoped design work; they do not require the finished product to have already passed acceptance.

When those conditions are met, record the evidence and decision/date, update this document's current gate status and move ROADMAP to implementation. A new material design change requires review; unchanged approved choices do not require repeated user confirmation. Remove obsolete pending/blocking statements; keep the historical decision record. Temporary instructions about when to stop a conversation are not standing project constraints. Working name or public-repository choice need not block local design; external actions retain their existing authorization boundaries.

## Document ownership

`SPEC.md` owns current scope and cross-cutting constraints; ADRs record why a consequential decision was made; `PRD.md` describes user capabilities; `feature_list.json` owns their acceptance steps; `ROADMAP.md` records sequence/status. `CONTEXT.md` is a glossary only. Research/brainstorm/archive files are non-normative evidence/history. See `docs/README.md` for navigation and update rules.
