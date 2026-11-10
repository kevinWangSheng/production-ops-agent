# Limitations

What the evidence in this repository does and does not show, as of 2026-11-10.

## No production observation

OpsPilot has never been deployed against a real production system. There is no production evidence of any kind in this repository.

Every run, test and measurement used one lab: the OpenTelemetry Demo on a kind cluster, created by [scripts/kind_lab.py](../../scripts/kind_lab.py), on a local VM or on one rented shared-vCPU host.

- **Traffic is synthetic.** It is the demo's own load generator. In some runs, Alertmanager-format alerts were also posted straight to the intake endpoint.
- **Faults are injected.** Almost all are flagd feature flags switched by `kind_lab.py fault inject`, mostly `paymentFailure`. The F1 exam and the soak add a fixed whitelist of further faults, noise switches and operational drills (stopping the worker, cutting model egress, a tunnel outage).
- **Real parts:** the model (DeepSeek `deepseek-flash`), the open-source software in the lab (Prometheus, Jaeger, Alertmanager and the demo services) and the product image. The incidents themselves are staged.

All evidence pages mark themselves as integration runs, drills or a lab soak. None of them is a production proof.

## Scope of what was tried

- One target workload (the demo's checkout path), one HealthProfile, one model provider, one team, one replica of each runtime.
- Tools read Prometheus and Jaeger only. There are no Kubernetes or log tools ([#307](https://github.com/kevinWangSheng/production-ops-agent/issues/307)).
- The soak ran for 72 hours once. Its control side ran on a host shared with other workloads, and the target side on a shared-vCPU host ([m3-soak-2026-11-07](../evidence/m3-soak-2026-11-07/README.md)). It had no numeric targets, and its latency and resource figures describe that run only.

## Evaluation

- The same-condition comparison with HolmesGPT is small: 12 paired dev scenarios and 6 paired holdout scenarios, 3 attempts each. Every confidence interval overlaps.
- The LLM judges are not calibrated against human labels, so F1 step 5 is not achieved ([#295](https://github.com/kevinWangSheng/production-ops-agent/issues/295)).
- The holdout set was written by an independent session in the same environment. There was no technical isolation and the test was not blind. Since publication, it counts as seen.
- The evaluation scripts were fixed after the exam started. Those fixes are disclosed on the [results page](../results/f1-same-condition-comparison-2026-11.md).
- No latency comparison was made, because the exam host had shared vCPUs.

## Acceptance status

[feature_list.json](../../feature_list.json) shows 8 of 11 features passing. These are still open:

| Feature | Open item | Evidence |
|---|---|---|
| F1 evaluation | Step 5 (calibrated judging) is not achieved | [results](../results/f1-same-condition-comparison-2026-11.md) |
| F8 operability | Evidence exists for steps 1–4, but `passes` has not been flipped | [drills](../evidence/f8-step1-drills/README.md), [upgrade/rollback](../evidence/f8-step3-upgrade-rollback/README.md), [soak](../evidence/m3-soak-2026-11-07/README.md) |
| F9 delivery | Step 1 is partial: on the clean host, connecting the agent needed undocumented steps. The docs were fixed in #532, but the run has not been repeated from a clean host. Step 2 is partial: no real person took a handoff, handled the system outside OpsPilot and registered a remediation, and the postmortem was not reviewed. Step 4 is partial: both versions had the same schema head, the documented rollback's downgrade step refused with exit 1 (UPGRADE.md was corrected afterwards in #534), and the local rollback image was not a pinned artifact | [step 1](../evidence/f9-clean-install-2026-11-09/README.md), [steps 2–4](../evidence/f9-steps-2-4-2026-11-10/README.md) |

Investigation quality is uneven. Published reports are often `partial` rather than `supported`; for example, two of the three Runs on the clean host were `partial` ([steps 2–4](../evidence/f9-steps-2-4-2026-11-10/README.md)). The M1-01 strict criterion (no unresolved P1/P2 review finding in any accepted Run) was never met ([closure record](../tasks/2026-09-28-m1-01-closure.md)).

## Security and architecture gaps

- The worker holds both the model key and the target read credentials. The C3 Controller / Tool Gateway separation is not built ([#197](https://github.com/kevinWangSheng/production-ops-agent/issues/197)).
- No TLS proxy and no outbound allow-list ship.
- Read-only access to the data sources depends on the credentials the deployer supplies.
- There are known gaps in credential redaction ([#359](https://github.com/kevinWangSheng/production-ops-agent/issues/359), [#365](https://github.com/kevinWangSheng/production-ops-agent/issues/365), [#366](https://github.com/kevinWangSheng/production-ops-agent/issues/366)).
- In one F1 attempt, an injected canary got into a report.

See the [threat model](threat-model.md).

## Known defects

- A budget reservation stays `reserved` when a worker is killed mid model call ([#527](https://github.com/kevinWangSheng/production-ops-agent/issues/527)).
- The Observer's `/readyz` briefly reports `POLL_STALE` during slow samples ([#522](https://github.com/kevinWangSheng/production-ops-agent/issues/522)).
- Container logs are not rotated ([#476](https://github.com/kevinWangSheng/production-ops-agent/issues/476)).
- Other open items carry the `post-m3` label in [GitHub Issues](https://github.com/kevinWangSheng/production-ops-agent/issues?q=is%3Aopen+label%3Apost-m3).

## Documentation language

The contracts, task records and most evidence pages are in Chinese. The README, the results page and these portfolio pages are in English.
