# Limitations

What the evidence in this repository does and does not show, as of 2026-11-11.

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

[feature_list.json](../../feature_list.json) shows 10 of 11 features passing. This one is still open:

| Feature | Open item | Evidence |
|---|---|---|
| F1 evaluation | Step 5 (calibrated judging) is not achieved | [results](../results/f1-same-condition-comparison-2026-11.md) |

F9 delivery passes on lab evidence only. An independent reviewer judged steps 1, 3 and 5 met and steps 2 and 4 met with limits. Evidence: step 1 [final clean-host run](../evidence/f9-final-clean-host-2026-11-11/README.md), step 3 [steps 2–4](../evidence/f9-steps-2-4-2026-11-10/README.md), steps 2 and 4 [second final run](../evidence/f9-final2-2026-11-11/README.md), step 5 this portfolio. The limits:

- It is an integration run on one rented VM with a kind lab, synthetic traffic and injected faults. It is not production proof and not a soak.
- Step 2: the handoff was forced by cutting the model egress for about 2.2 s. No report was published before the handoff, and the details of the failed model calls were not exported.
- Step 2: host records (a `su` in the system log, the ConfigMap's `managedFields`) corroborate the time of the external fault restore, 21:59:59Z, between takeover and `register_remediation`. They cannot show who ran it, because the user and the agent connect from the same machine with the same key. That the user ran it rests on the user's statement plus that corroboration.
- Step 2: the postmortem is still `under_review`, and the knowledge base has 0 entries.
- Step 4: `v0.1.0` and `v0.1.1` share runtime code and schema head, so no code or schema change was upgraded across. Staging is a second Compose project on the same VM and needed two undocumented steps to join the lab (network join without aliases, alert forwarding). An in-flight recovery observation on staging continued across the digest switch and reached `recovery_confirmed`; the main project's upgrade and rollback had no in-flight work.
- The cause of two `no_data` samples in that staging observation is unknown. These runs have no LangSmith trace and no cost reconciliation against the supplier balance.

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
