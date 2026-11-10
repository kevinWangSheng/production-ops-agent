# Portfolio walkthrough

A path through the project for a reviewer. Allow about 20 minutes. OpsPilot is a read-only incident investigator. It reads telemetry and writes evidence-backed reports. A separate process checks whether the service recovered, and every decision stays with a human. It was built and measured only against a lab with synthetic traffic and injected faults. It has never run in production ([limitations](limitations.md)).

## 1. What it is and what it refuses to do (3 min)

- [README](../../README.md): what it does, current status and how to install.
- [PRODUCT-CONSTRAINTS.md](../../PRODUCT-CONSTRAINTS.md): the runtime rules. There are no production writes, no release gates and no model-chosen shell or SQL.
- [Architecture](architecture.md): the processes and what each one owns, one incident's path through them, and where the code still differs from the design.

## 2. One incident, end to end (5 min)

This incident ran on a freshly reinstalled rented host. Both the kind lab with the OpenTelemetry Demo and OpsPilot (via Compose) were installed with the repository's scripts. Connecting them took three steps the docs did not yet describe; those were added afterwards in #532 ([step 1 evidence](../evidence/f9-clean-install-2026-11-09/README.md)). Times are UTC on 2026-11-10. Each row below is a database record listed in [f9-steps-2-4 evidence, step 2](../evidence/f9-steps-2-4-2026-11-10/README.md#第-2-步一次事故从告警到归档):

| Time | What happened |
|---|---|
| 07:07 | The lab injects the flagd fault `paymentFailure` (injected fault, synthetic load-generator traffic) |
| 07:13 | Alertmanager fires `CheckoutPlaceOrderErrorRatioHigh`. OpsPilot opens an incident and a Run |
| 07:13–07:16 | 12 model rounds and 20 read-only Prometheus/Jaeger queries. The report is `supported` and the root cause matches the injection |
| 07:16 | Recovery observation is authorized automatically after the qualified conclusion |
| 07:19 | The fault is removed by the lab script |
| 07:43 | The Observer records `recovery_confirmed` after a sustained healthy window, using its own account and no model |
| 07:45 | The worker drafts a postmortem, which waits for human review |
| 09:39 | The user closes the incident in the workbench |

The same evidence page lists what this run did not cover. No human took a handoff, nothing was handled outside OpsPilot, and nobody reviewed the postmortem. On the same host, the worker was killed in the middle of a Run, and the Run resumed on the same incident (step 3). The release image was also upgraded, rolled back and upgraded again with work in flight (step 4).

An older Chinese walkthrough of the M1 flow, including knowledge approval, is in [docs/demo/m1-end-to-end.md](../demo/m1-end-to-end.md).

## 3. How good is it (5 min)

- [Same-condition comparison with HolmesGPT](../results/f1-same-condition-comparison-2026-11.md). The setup was the same lab, faults, model, read-only proxy and budgets for both systems, with 3 attempts per scenario. The page reports dev and holdout results, the failures, what changed after the exam started, and why the judges count as uncalibrated. Its numbers come from the sealed [S10 evidence](../evidence/f1-s10-scoring-2026-11-06/).
- [72-hour lab soak](../evidence/m3-soak-2026-11-07/README.md) (Chinese). It covers availability, missing observations, stuck work, resource use and cost under scheduled injected faults and drills. It is a lab soak, not production proof.

## 4. How it is operated and secured (4 min)

- [Operations](operations.md): health checks, the usual failure situations and how to handle them, upgrade and rollback, data and cost.
- [Threat model](threat-model.md): each product rule, what enforces it, the evidence and the remaining gap.
- [Upstream attribution](upstream-attribution.md): what was copied from HolmesGPT and the DeepSeek tokenizer, and under which licenses. It also covers what was compared and what only runs in the lab.

## 5. How it was built (3 min)

- Contracts first. Design decisions are in the Chinese [C3 technical design](../design/technical-proposal-2026-09-07.md) and the [ADRs](../adr/). Acceptance steps are in [feature_list.json](../../feature_list.json). A feature's `passes` flag is flipped only when every step has evidence.
- Evidence over claims. Real runs leave a frozen `summary.json` under [docs/evidence/](../evidence/), with raw logs kept outside the repository. Partial results and failures are recorded as such: F9 steps 1, 2 and 4 are marked partial in their own evidence.
- Separate authors. Acceptance tests and reviews come from agents that did not write the implementation, as required by the project rules in [AGENTS.md](../../AGENTS.md). The work order and status are in [ROADMAP.md](../../ROADMAP.md) (Chinese).

## Try it

For one clean Ubuntu 24.04 host, follow the README section [The OpenTelemetry Demo lab on the same host](../../README.md#the-opentelemetry-demo-lab-on-the-same-host). You need a DeepSeek API key. The lab takes about 5 minutes to come up. After `fault inject`, the checkout alert fires within about 5–7 minutes.
