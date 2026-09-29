# normal-1 独立审查（全新上下文 Agent，Opus；原文照录，未经执行者改动）

Reviewer inputs: v4 packet criteria, PRODUCT-CONSTRAINTS, ADR-0005，模型可见 REPORT_CONTRACT/RUN_COVERAGE_TEMPLATE 与 C2 修复反馈构造器、工具描述，docs/tasks/2026-09-28-m1-01-alignment-c.md（C1-C3 背景），本目录，55431 只读 SQL。审查者未见执行者评估或其他审查者结论。本 Run 触发了 C2 修复反馈重试（round5→round6），审查者被要求独立复算 round5 的拒绝原因与 round6 是否修对。

Verdict: FAIL, because of one P2. The report correctly found no fault, and it cited span_groups values heavily. But one duration figure in a round-6 claim comes from a different view than the one the claim cites. That error was introduced when the model rewrote the report for the retry.

## Criteria (v4 packet)

1. **PASS.** The final report parses (round 6): `assessment_status=completed`, `conclusion=partial`, no handoff. It used 6 of 100 model requests. Round 5 was rejected and round 6 was the forced retry.
2. **PASS.** The core conclusion ("the delivered evidence does not corroborate elevated checkout errors") rests on delivered ok views:
   - the checkout span-status series is UNSET only, ≈124.998;
   - the non-UNSET query returned no checkout ERROR series, and the ERROR series it did return show increase 0 (explicitly returned zeros);
   - all 8 checkout PlaceOrder spans have `rpc.grpc.status_code=0`.

   Facts, hypothesis, counter-evidence, rejected hypothesis, gaps and next steps are kept distinct.
3. **FAIL.** One claim's number and source disagree (P2-1 below). Everything else checks out:
   - The metric numbers match `ledger.json` exactly: 124.998; 8.76/27.52/18.76; averages 15.64/16.11/15.08/3.91/2.98/1.02/0.34; p95 41.25/9.92/8.25/4.75; service volumes; the 1-hour ERROR series at increase 0.
   - Trace counts are right: 8 traces, 416 spans, 172 shown, 244 omitted, truncated; 7 of 17 views truncated, 2 incomplete, 3 no_data.
   - Missing series are stated as "unknown rather than zero", and not_recorded spans are stated as not explicit success.
4. **PASS.** It explicitly refuses to certify health ("neither 'no errors' nor 'elevated latency' can be certified"). It takes no remediation action and makes no release-gate call.
5. **FAIL.** P2-1 is open.

## Findings

**P2-1 (class e; MODEL defect).** Claim 12 says "cart spans … 73-27,542 microseconds" and cites only the cart view `a6f7eea4-…-t4`.
- In that view, cart-service durations top out at **18,908 µs**, both in the shown rows and in all 407 raw spans in PostgreSQL. The largest cart group there is `POST /oteldemo.CartService/EmptyCart` at 7436-18908.
- 27,542 is the cart `POST /…/EmptyCart` maximum in *other* views: checkout t2, and the payment, email, shipping and currency views (t1, t2, t5, t6).
- Round 5's claim 9 cited t3-t6 together, so 27,542 was supported by t5/t6. Round 6 split that into one claim per view, kept the range, and cited only t4. So the retry introduced this error.
- It does not change the conclusion.

**P3-1 (MODEL).** Gap 2 describes view `52365e55-…-t3` as "checkout outbound client calls with rpc_grpc_status_code != "0"". The actual query has no `service_name` filter, so it covers all services. The wider query returning no_data still implies nothing for checkout, so the meaning holds. This is borderline between wording and scope; I scored it P3.

**P3-2 (MODEL).** Gap 7 says "Only two evidence contexts were delivered for metrics". The run had one evidence context and 11 metric views, so the count has no basis. It sits in a gap, not a fact claim.

**P3-3 (MODEL).** Claim 3 calls frontend, frontend-proxy and load-generator "dependencies"; they are upstream of checkout.

**P3-4 (PRODUCT).** The C2 reason name `missing_target_refs` is also used when the refs are present but unauthorized (here `"checkout"`, `"currency"` and so on). The feedback does not name the offending ref. The model fixed it anyway, so this is wording only.

No P1 findings:
- The run stayed read-only.
- Budget: 6 of 100 requests. Peak prompt was 251,805 tokens, inside the run's `context_tokens=1,000,000`. That limit comes from the L3a change recorded in the loop-limits task, which replaced 131,072.
- No human-control or state issue: completed and published, events 1-26 in consistent order.

## C2 retry
- **(a) Round 5's rejection was correct.** I re-ran the product validator (`delivered_view` + `report_retry_feedback`) on round 5's content. All 14 claims fail with `missing_target_refs`: every claim added service names (`checkout`, `currency`, `payment`, …) to `target_refs`. The evidence context has no `target_catalog`, and the authorized targets are only `m0-otel-20260909`. Evidence ids, time_scope and ok/citable status were otherwise all valid.
- The reconstructed feedback reads "…(REPORT_INVALID). claim 0: missing_target_refs. … claim 13: missing_target_refs." It is itemized, in order, and contains no view content. This matches C2.
- **(b) Round 6 fixed that problem cleanly.** Every claim now has `target_refs=["m0-otel-20260909"]`, and the validator returns no failures.
- Round 6 also fixed two round-5 content errors the validator had not flagged: shipping GetQuote "27–26,379" became the correct 4,379-26,379, and the garbled "5 spans in span_groups / 8 rows" was removed.
- **(c) One new error was introduced:** P2-1.

## C3 span_groups
- **(a) Correct in the evidence.** All 7 traces_search views carry `span_groups` and `span_groups_note`. I recomputed rows, status dict, error_rows and duration min/max from each view's shown `content`: 0 mismatches. Groups cover exactly the shown rows (the checkout groups sum to 172) and are sorted by `(service, operation)`.
- **(b) Used heavily.** Claims quote group values directly:
  - rows: PlaceOrder 8, GetProduct 36, ListProducts 5, Convert 24;
  - status keys, e.g. `otel.status_code="OK";rpc.grpc.status_code=0`;
  - "zero error_rows";
  - exact min-max ranges: 58,469-165,554; 1,231-6,415; 5,537-20,901; 3,452-13,967; 12-1,905; 590-6,354; 4,379-26,379; 17-73; 233-4,992; 72,142-185,753.
- **(c) Used correctly except once.** The combined frontend range 60,912-183,138 is a correct union of three groups. The single misuse is P2-1, a range carried over from other views.
- The raw data had 0 error spans in every trace view, including the omitted spans.

## Window contamination
No contamination found.
- All trace rows in every cited trace view fall between 17:36:55Z and 17:41:14Z, inside the query window 17:36:15-17:41:15. The raw records show `spans_outside_window=0`.
- All 8 cited trace ids appear in `backend_trace_ids` and in the checkout and payment views.
- The only wider metric window (16:41-17:41) returned ERROR series at increase 0, and the report labelled it as the 1-hour window.
- The 15:36 and 16:36 windows are no_data and were reported as gaps.
- No earlier fault traffic was reported as a fault.

## Upstream-style verdict
Correctly judged no fault. observe-pre.json (17:36:10-17:41:10) shows 9 checkout traces, 0 with checkout errors, and `all_services_error_calls` positive series = 0 (`fault_confirmed=false`). That matches the report's "not corroborated".

VERDICT: FAIL
P1=0 P2=1 P3=4
P2-by-class: a=0 b=0 b-svc=0 c=0 d=0 e=1
Window contamination: no contamination found
C2 retry: round 5 deserved rejection (all 14 claims had unauthorized service-name `target_refs`, correctly reported as `missing_target_refs`); round 6 fixed it cleanly but introduced one new P2 (cart range 73-27,542 now cites only t4, whose cart maximum is 18,908)
C3 span_groups usage: present and exactly correct in all 7 trace views; the report quoted group rows, status, error_rows and min/max extensively and correctly, except the one cross-view cart range (P2-1)
Upstream-style verdict: correctly judged no fault
