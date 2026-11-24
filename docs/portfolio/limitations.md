# Limitations

What the evidence in this repository does and does not show, as of 2026-11-11, with the benchmark runs of 2026-11-17 to 2026-11-22 added under [Evaluation](#evaluation).

## No production observation

OpsPilot has never been deployed against a real production system. There is no production evidence of any kind in this repository.

Every M3 run, test and measurement used one lab: the OpenTelemetry Demo on a kind cluster, created by [scripts/kind_lab.py](../../scripts/kind_lab.py), on a local VM or on one rented shared-vCPU host. The later benchmark runs used the benchmarks' own fixtures and applications in kind clusters on rented hosts.

- **Traffic is synthetic.** It is the demo's own load generator. In some runs, Alertmanager-format alerts were also posted straight to the intake endpoint.
- **Faults are injected.** Almost all are flagd feature flags switched by `kind_lab.py fault inject`, mostly `paymentFailure`. The F1 exam and the soak add a fixed whitelist of further faults, noise switches and operational drills (stopping the worker, cutting model egress, a tunnel outage).
- **Real parts:** the model (DeepSeek `deepseek-flash`), the open-source software in the lab (Prometheus, Jaeger, Alertmanager and the demo services) and the product image. The incidents themselves are staged.

All evidence pages mark themselves as integration runs, drills or a lab soak. None of them is a production proof.

## Scope of what was tried

- One target workload (the demo's checkout path), one HealthProfile, one model provider, one team, one replica of each runtime.
- The M3 evidence used the Prometheus and Jaeger tools only. The read-only Kubernetes, log, database, Kafka, Argo CD, Helm and Redis/Valkey tools added afterwards (`otel-demo-ext` profile) were exercised only in the benchmark runs below, not in an M3-style acceptance or soak.
- Release observation is authorized only for the dev kind cluster. The calibration manifest ([release-calibrations.json](../../opspilot/observation/policies/release-calibrations.json)) pins the target to that cluster's `cluster_uid`, and the path is fixed in `release_policy.py`. A new cluster (a rented host, a staging cluster) does not get release observation automatically; the rental installs even register the lab target with `cluster_uid` `opspilot-m1`, which the manifest does not match. Extending it needs its own contract review ([#391](https://github.com/kevinWangSheng/production-ops-agent/issues/391)).
- The soak ran for 72 hours once. Its control side ran on a host shared with other workloads, and the target side on a shared-vCPU host ([m3-soak-2026-11-07](../evidence/m3-soak-2026-11-07/README.md)). It had no numeric targets, and its latency and resource figures describe that run only.

## Evaluation

- The same-condition comparison with HolmesGPT is small: 12 paired dev scenarios and 6 paired holdout scenarios, 3 attempts each. Every confidence interval overlaps.
- The LLM judges are not calibrated against human labels, so F1 step 5 is not achieved ([#295](https://github.com/kevinWangSheng/production-ops-agent/issues/295)).
- The holdout set was written by an independent session in the same environment. There was no technical isolation and the test was not blind. Since publication, it counts as seen.
- The evaluation scripts were fixed after the exam started. Those fixes are disclosed on the [results page](../results/f1-same-condition-comparison-2026-11.md).
- No latency comparison was made, because the exam host had shared vCPUs.
- HolmesGPT public evals (53 questions × 3 trials, same model and judge): the full run of 2026-11-21 scored OpsPilot 133/159 and HolmesGPT 150/159. The post-fix 145/159 is composed from a targeted OpsPilot-only rerun on 2026-11-22, not a single full run; HolmesGPT was not rerun, and wobbly questions were not given extra trials on either side. HolmesGPT ran its upstream default configuration, including tools OpsPilot leaves out; its 5 database misses are pytest errors or skips with no answer, and whether the upstream harness is at fault is not confirmed. See the [README](../../README.md#benchmark-results-2026-11) and the [rerun record](../evidence/rental-2026-11-20/rerun-2026-11-22/run.md).
- The benchmark judge is Z.ai `glm-5.3-flashx`, not the upstream judges, and it is not calibrated against human labels either.
- ITBench-Lite (82/105) is the official evaluator's root-cause pass@1 on offline snapshots, with that judge; OpsPilot's own deterministic entity match scores the same trials 22/105, and most conclusions are `partial`. There is no HolmesGPT run on ITBench-Lite or SREGym.
- Known gaps behind failed questions: the lookback window is fixed at 24 hours by design ([#779](https://github.com/kevinWangSheng/production-ops-agent/issues/779)); there is no input for standing per-target operator instructions ([#780](https://github.com/kevinWangSheng/production-ops-agent/issues/780)); SREGym problem p12 (Valkey, 0/3) has no read-only account to give the Redis/Valkey tools ([#773](https://github.com/kevinWangSheng/production-ops-agent/issues/773)).

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
- Credential redaction works from names. Pod environment variables are judged by the variable name: a credential-like name (also `PASS`/`PWD` in env names) hides `env[].value`, and the same `"name":"X","value":…` pair inside JSON text; the Kubernetes jq and tabular tools redact the cluster objects before running the model's expression ([#574](https://github.com/kevinWangSheng/production-ops-agent/issues/574)). The text form matches any `"name":X,"value":Y` JSON, not only env entries, so it over-redacts ordinary pairs such as `{"name":"golden_pass","value":"3/3"}` or `{"name":"auth_token_ttl","value":3600}` (accepted). A command-line flag whose name looks like a credential hides its value, joined (`--db-password=v`, `--password: v`) or as the next token, across spaces, newlines, blank lines, a `\` continuation, a `# comment` and a YAML bullet, and as the next element of an `args` list, of a JSON or Python list in text (also escaped inside a JSON string, such as a `last-applied-configuration` annotation) or of a YAML flow list. Any next token not starting with `-` is hidden, so an ordinary word or the next log line after such a flag (`--session S`, `--auth-local and`) is hidden too, and an unclosed quote in the value hides the rest of the text; `--max-tokens 4000` stays (accepted; [#802](https://github.com/kevinWangSheng/production-ops-agent/issues/802)). A credential pair glued to a credential value (`"password":x&"secret": "v"`, `password=x|token :v`, `token: "password": "v"`, also with an auth scheme, `password=x|authorization=Bearer v`) is hidden after every other rule has run, up to the next whitespace or comma, so ordinary text glued on in the same run is hidden too (accepted; [#803](https://github.com/kevinWangSheng/production-ops-agent/issues/803)). It still misses a secret with no naming context, a secret under an ordinary name (`STRIPE=sk_live…`), an env pair written `value` before `name` or escaped one level deeper, env written as YAML text inside a string (a ConfigMap's `deploy.yaml`), ConfigMap data keys (not treated as env names, so `DB_PASS`-style keys follow only the general rule, also when consumed via `envFrom`), base64 data (`binaryData`, `@base64d`; no naming context), mixed or double URL encodings, and these flag forms: a flag glued to a preceding letter, digit, `_`, `.`, `-`, `}`, `)` or `]` (`{{ .Release.Name }}-credentials`), a `/password` or Unicode-dash flag, a space-separated value that itself starts with `-` (`--password -x`, read as the next flag), the rest of an unquoted value after a quote that looks like the end of the enclosing string (`--password ab",x`), a value with spaces inside escaped single quotes (`\'a b\'`), a flag and its value in separate nested lists (`[["--password"], ["v"]]`), a value after a comma outside a list (`use --token, s3cret then`; a deliberate narrowing so prose punctuation is not read as a separator), the words after a space inside a quoted whole-flag string (`"--password ab s3cret"` keeps `s3cret`, as `password=a b` keeps `b`). Redis/Valkey password directives (`requirepass`, `masterauth`, `primaryauth`) are credential names, and their config-line form (`requirepass v` in a redis.conf) hides the next word on the same line (and a bare `requirepass` element of a command list, structured or quoted in JSON text, hides the next element; an unquoted YAML flow list `[requirepass, v]` is not recognised), so the word after `requirepass` in ordinary prose is hidden too, and inside a JSON-escaped redis.conf the hidden word runs past an escaped `\n` into the next line. An assignment value that itself ends in a quoted credential pair does not hide that pair's value (`password: x"api-key=": v` shows `v`; present before for the older names and now also for the Redis names) (accepted; [#818](https://github.com/kevinWangSheng/production-ops-agent/issues/818)).
- In one F1 attempt, an injected canary got into a report.

See the [threat model](threat-model.md).

## Known defects

- Open items carry the `post-m3` label in [GitHub Issues](https://github.com/kevinWangSheng/production-ops-agent/issues?q=is%3Aopen+label%3Apost-m3).

## Documentation language

The contracts, task records and most evidence pages are in Chinese. The README, the results page and these portfolio pages are in English.
