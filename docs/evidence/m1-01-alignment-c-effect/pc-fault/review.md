# pc-fault 独立审查（全新上下文 Agent，Opus；原文照录，未经执行者改动）

Reviewer inputs: v4 判据, PRODUCT-CONSTRAINTS, ADR-0005，模型可见 REPORT_CONTRACT/RUN_COVERAGE_TEMPLATE 与工具描述，docs/tasks/2026-09-28-m1-01-alignment-c.md（C1-C3 背景），本目录，55431 只读 SQL。审查者未见执行者评估或其他审查者结论。留出场景（productCatalogFailure），未用于调优本候选，需核查是否与更早的 paymentFailure 事故混淆。

**Bottom line:** the report finds the right root cause and does not mix in the earlier paymentFailure episode. It still fails the packet, because five observable P2 errors remain: two where `not_recorded` spans were written as an explicit good status, and three other number, latency or source errors.

Everything I checked came from the evidence directory `pc-fault/`: `report.json`, the `ledger.json` evidence views and steps, and `observe-pre.json`. I also recomputed span_groups with the candidate's `_span_groups` at `ce6972e` and with a separate script of my own. I did not query PostgreSQL, because the stored raw hashes already match their recomputed values.

### Criteria 1-5
1. **PASS.** The report parses. It ends `assessment_status=completed` with `conclusion=partial`. There is no handoff, the Run state is `completed`, it made 5 model requests and 11 tools, and it stopped on its own.
2. **PASS.** The core conclusion is backed by delivered trace views: checkout PlaceOrder errors (gRPC 13) are caused by product-catalog GetProduct returning `Product Catalog Fail Feature Flag Enabled`, linked by a visible CHILD_OF edge (18203d6e37340f04 → 0146f663e7e52410 in trace 896af2…). The flag mechanism is correctly labelled a hypothesis.
3. **FAIL.** `not_recorded` status is written as grpc 0 / 200 (two findings), and one fact cites views that do not contain what it claims (details below).
4. **PASS.** It claims no recovery and performs no remediation. "Payment shows no failures" rests on explicit zero series and sampled grpc-0 Charge spans.
5. **FAIL.** Five P2 findings are open.

### Findings (all MODEL defects)
**P2 (a): `not_recorded` written as an explicit good status**
- **Claim 6.** It says all sampled payment spans, "the internal 'charge' operation" included, "have rpc.grpc.status_code=0". In `a9f3…-t2` span_groups, `payment/charge` is `{"not_recorded": 7}`. The tool description states that `not_recorded` is not status code 0.
- **Claim 8.** It says cart "POST / POST /oteldemo.CartService/EmptyCart / flagd ResolveBoolean all rpc.grpc.status_code=0", and that email "send_email" is "200 or rpc.grpc.status_code=0". In span_groups, cart `POST` is `not_recorded:6`, cart `EmptyCart` is `not_recorded:7`, and email `send_email` is `not_recorded:6`.

**P2 (e): other observable errors**
- **Claim 9 and the summary.** They say checkout client p95 latency "sits in the same range across all six points" and that "no dependency-call latency elevation is visible". The report's own data in `5ca0…-t1` says otherwise:
  - EmptyCart is 220 ms at 17:59:07 and 92.5 ms at 18:04:07, against 23-47.5 ms earlier.
  - GetCart is 45 ms at 17:59:07, against 4.75-8.5 ms.

  The report hedges PlaceOrder's 412.5 ms as possible noise but asserts flatness for these as fact.
- **Claim 10.** It says product-catalog ListProducts server p95 stays "~4.75-4.85 ms throughout". `a9f3…-t1` shows 7.25 ms at 17:44:07. Impact is low.
- **Claim 12 (source mismatch).** This fact says the 17:24:07-17:29:07 baseline queries and the cart/currency/shipping/email error query "returned status no_data". It cites `5ca0…-t0/t1`, which are ok views and contain none of that. The actual no_data views are `3f1d…-t0`, `3f1d…-t2` and `a9f3…-t0`. The prompt says non-ok views belong in gaps. The same content does appear correctly in gaps, so it is true but mis-sourced.

**P3 (wording only)**
- Claim 11 says checkout PlaceOrder ERROR is "absent/0 at 17:59:07". It is an explicit 0.0.
- Claim 8 says "shipping … ShipOrder", but only checkout's client ShipOrder span is visible (1 row). No shipping-service ShipOrder span is shown.
- The exception message is quoted with a stray extra `'`.
- The six-point latency comparison includes the 17:49 and 17:54 points, which fall in the paymentFailure episode, without saying so. It does not change any conclusion.

**P1: none.** Permissions, human control, budget and state are clean.

### C2 retry
Not triggered. The ledger has exactly one conclusion-shaped response: step `4d032596…`, `conclusion:g0:ctx0:round-5`. Rounds 1-4 are `tool_calls`, and round 5 is a single `stop` whose output was accepted as the report.

### C3 span_groups
- **(a) Correctness:** all three traces_search views check out. My recompute matched both the candidate function and my own independent count for rows, error_rows and duration min/max. Groups are sorted by `(service, operation)`, the row totals equal `spans_shown` (159, 171, 143), and `span_groups_note` is present.
- **(b) Usage:** the report uses span_groups.
  - Claim 1 cites it explicitly: "span_groups … count 3 error rows of 10 PlaceOrder spans".
  - The counter-evidence says "3 error spans" for checkout client GetProduct, which matches `error_rows: 3`.
  - The payment duration range of 1.2-10.7 ms matches the group min/max.
  - The 30-388 ms range for successful PlaceOrder spans was correctly taken from raw rows, since the group min includes error rows.
- **(c) Accuracy of that use:** the counts and durations are right. However, the model misread the `not_recorded` status buckets in the charge, cart and email groups — these are the two P2 (a) findings.

### Window contamination
**No contamination found.**
- **Trace views:** all three trace views are windowed 17:59:07-18:04:07. The spans actually shown start between 17:59:11 and 18:03:58, which is after the productCatalogFailure injection at 17:58:58 and more than 7 minutes after the paymentFailure restore at 17:51:32 (`fault-timeline.jsonl`).
- **Metric views:** the four one-hour metric views (17:04-18:04) do span the paymentFailure episode (17:45:17-17:51:32).
- **How the report handled the overlap:** it explicitly split the 17:49:07 and 17:54:07 PlaceOrder and Charge error values (6.57 and 3.75) into "an earlier, separate episode in which failed payment charges (not product-catalog) were the failing dependency" (hypothesis 14). It noted that the Charge error series is 0 at both 17:59:07 and 18:04:07, and that product-catalog GetProduct ERROR appears only at 18:04:07 (7.14). Nothing from the payment episode is attributed to product-catalog.
- **Scope of the fault:** the report does not generalize to "product-catalog entirely down". It gives 3 of 10 PlaceOrder spans as errors, and the product-catalog view shows GetProduct at 8 errors of 30 rows.

### Upstream-style verdict
Root cause correctly identified. `observe-pre.json` has `failed_dependency_services=["product-catalog"]` and three fault traces (dd282313…, 896af2bc…, d11a8eda…), exactly the three checkout failure traces the report cites.

VERDICT: FAIL
P1=0 P2=5 P3=4
P2-by-class: a=2 b=0 b-svc=0 c=0 d=0 e=3
Window-contamination: no contamination found. The one-hour metric views overlap paymentFailure (17:45:17-17:51:32), but the report correctly labelled those points as a separate probable payment episode. Every trace view is post-injection (17:59:11-18:03:58).
C2-retry-verdict: not triggered (single conclusion response, round 5)
C3-span_groups-usage: span_groups present and exactly correct in all 3 trace views. The report used them for its error and duration counts correctly, but read the `not_recorded` status buckets (payment charge, cart POST/EmptyCart, email send_email) as grpc 0 / 200.
Upstream-style verdict: correct root cause (product-catalog GetProduct flag failure), matching `observe-pre.json`'s failed dependency and all three fault trace ids.
