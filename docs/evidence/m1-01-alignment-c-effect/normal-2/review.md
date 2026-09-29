# normal-2 独立审查（全新上下文 Agent，Opus；原文照录，未经执行者改动）

Reviewer inputs: v4 packet criteria, PRODUCT-CONSTRAINTS, ADR-0005，模型可见 REPORT_CONTRACT/RUN_COVERAGE_TEMPLATE 与工具描述，docs/tasks/2026-09-28-m1-01-alignment-c.md（C1-C3 背景），本目录，55431 只读 SQL。审查者未见执行者评估或其他审查者结论。本 Run 未触发 C2 重试。

No P1 or P2 findings survived verification for normal-2, and the report correctly judged that there was no fault. I read only the stored evidence (report.json, ledger.json, observe-pre.json, events.jsonl, stats.json) plus the v4 packet and the `_span_groups` code; I edited nothing.

**Run facts:**
- The run completed normally in 6 model requests and 23 tool calls, with no handoff.
- Round 6 ended with `finish_reason=stop` (`self_terminated: true`).
- The events contain only intake, claim, 6 step commits, 23 tool commits and completion. There was no report-validation failure, so the C2 retry was **not triggered** and was not exercised in this run.

## v4 criteria

1. **PASS.** The final report parses (`assessment_status=completed`, `conclusion=partial`, `execution=completed`). "Completed but uncertain" is kept separate: the latency trend is stated as unknown in the gaps. No budget or connection failure is involved.
2. **PASS.**
   - The core conclusion (no observed failures in checkout or its six dependencies) rests on evidence the tools actually delivered.
   - Facts, rejected_hypothesis, the open hypothesis about the 283 omitted spans, counter_evidence, gaps and next_steps are kept distinct.
   - Nothing in the report comes from the engineer-side observation.
3. **PASS.** I checked every cited number against its view:
   - 92.33 ms average and p99 245.5 ms (`70d684c0…-t0`/`-t2`)
   - payment transactions: USD 7.52, CAD 0 (`1bd11170…-t4`)
   - 456/173/283 spans (backend returned / shown / omitted), and 9 traces from a trace limit of 40, so limit and shown rows are not confused
   - the four 5-minute ERROR series are frontend, frontend-proxy, load-generator and payment, all 0
   - checkout returned only UNSET across 11 span names (`3e1e3285…-t0`)
   - all gRPC status codes are 0
   - the raw series starts at 17:37:31 with points at 17:37:31, 17:39:31, 17:41:31 and 17:43:31
   - both baseline queries at 17:03–17:08 and the one at 17:28–17:33 returned no_data

   Missing series are treated as unknown, not zero, both in claim 1 and in the gaps. Counters are described as increases. For each of the 9 traces, payment `grpc.oteldemo.PaymentService/Charge` has status 0 and email `POST /send_order_confirmation` has 200. Cart GetCart and EmptyCart are cited through checkout's client spans, which do carry status 0. The cart server spans are `not_recorded` and are never called good.
4. **PASS.** The report does not certify health: it says "not corroborated", concludes `partial`, and names the truncation and missing-baseline gaps. It performs no remediation and does not claim a release gate.
5. **PASS.** No P1 or P2 findings; only P3 wording issues.

## Findings
- **P3, model defect:** The summary says the 5-minute queries returned "no ERROR-status series for checkout, …, or any other backend service". Payment, a backend service, did return a STATUS_CODE_ERROR series with an increase of 0. The same sentence then lists payment correctly, and claim 2 is accurate.
- **P3, model defect:** Gap 1 says both baseline attempts returned no_data for the "average and p99 queries". The 17:28–17:33 attempt only ran the average query; p99 was run only for 17:03–17:08.
- Observation, not a defect: the counter_evidence claim calls its source a "1-hour window", but metric data only starts around 17:37:31. The report discloses this in gap 2.

## P2 by class
a=0 b=0 b-svc=0 c=0 d=0 e=0 (total 0)

## Window contamination
**No contamination found.**
- Every tool window is either the 5-minute window (17:38:31–17:43:31) or a query the model chose explicitly for a baseline, 30 minutes or 1 hour. None of those returned data from before 17:37:31.
- All five trace views have `source_start_at` ≥ 17:38:33.97 and `source_end_at` ≤ 17:43:25.18.
- All 9 cited trace IDs fall inside the 5-minute window, and observe-pre's Jaeger query found the same 9 traces, all in window.
- All 5-minute metric points are at 17:43:31. No history from earlier packages reached this run's evidence: Prometheus had no checkout series before about 17:37:31, which the report states itself.

## C3 span_groups
- **(a) They appear correctly.** All 5 `traces_search` views carry `span_groups` and `span_groups_note`, sorted by `(service, operation)`. Group row counts sum to `spans_shown` (173, 173, 174, 24, 150). An independent recount of rows, error_rows and duration min/max from the shown rows matches exactly, and `_span_groups(content)` reproduces the stored value in all 5 views.
  - Spot-check, checkout view: PlaceOrder has rows=9, status `rpc.grpc.status_code=0`: 9, error_rows=0, duration 63357–120282.
  - Spot-check, payment view: `grpc.oteldemo.PaymentService/Charge` has rows=9, status 0, and one span per trace in all 9 traces.
- **(b) Used implicitly, never quoted.** The report's PlaceOrder range "63,357 to 120,282 microseconds" equals that group's min/max, and "no error status tags" matches error_rows=0 in every group. No claim quotes `rows`, a `status` dict or `error_rows` by name, and the per-trace lists could equally come from raw rows. So whether the model read the groups cannot be proven.
- **(c)** Every value that matches a group is used correctly, including not calling `not_recorded` groups a good status.

## Upstream-style verdict
**Correctly judged no fault.** observe-pre agrees on every point: `expect=normal`, `all_services_error_calls` positive series = 0, and 9/9 traces in window with no checkout error spans and no failed dependency children.

VERDICT: PASS
P1=0 P2=0 P3=2
P2-by-class: a=0 b=0 b-svc=0 c=0 d=0 e=0
Window contamination: no contamination found
C3-span_groups-usage: present and correct in all 5 views; the report never quotes group fields, but its PlaceOrder duration range and "no error" statement match group values and are correct
Upstream-style verdict: correctly judged no fault (observe-pre: expect=normal, 0 error series, 9/9 traces with no failed dependency spans)
