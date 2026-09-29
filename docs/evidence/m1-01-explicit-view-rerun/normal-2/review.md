# normal-2 独立审查（全新上下文 Agent，Fable 5.1；原文照录，未经执行者改动）

Reviewer inputs: v4 packet criteria, PRODUCT-CONSTRAINTS, ADR-0005, the model-visible REPORT_CONTRACT / RUN_COVERAGE_TEMPLATE and tool descriptions, this directory, read-only SQL on 55431. The reviewer did not see the executor's assessment.

4777de1b-3a6e-5fdd-808c-16e3d8c84014 (run_id) / 2dc9e329-864d-5c04-b57e-f04707a00fca (incident_id) — case normal-2, candidate f8fac31, model deepseek-flash

## Record integrity
- Report hash: sha256(opspilot_steps ctx0:round-4 assistant.content) = 363d0ca0…37b49 = report.json.report_sha256 = sse run_completed.report_sha256 = incident.html. finish_reason=stop. OK.
- Evidence: 14 rows in DB, 14 in ledger, 14 evidence_ids in run_completed; all adopted+committed. Recomputed sha256 of raw bytes matches DB raw_sha256 and ledger raw_sha256 for all 14 (0 mismatches); view_sha256 matches for all 14.
- Run/budget: DB run state=completed, owner/lease NULL, epoch 1, budget_limit 4 / spent 4 / reserved 0 / unknown 0 (4 reservations all `spent`), model_requests_used=4 (limit 4), tool_operations_used=14 (limit 20; 14 tool_charges, 30 s reserved each, 0.256 s used), deadline 08:40:39.74 = intake +1800 s, run_completed at 08:11:27 (~48 s wall). Window 300 s. No handoff, handoff_reasons=[]; ADR-0005 publish-on-qualified-report path taken (worker log status=published).
- Events: 21 in DB = 21 in events.jsonl = 21 in sse.txt; kind+payload identical in all three, sequence 1–21 contiguous.
- Windows: query.window of all 14 views == window.json (15:05:39 → 15:10:39 UTC). All 9 metrics views: lookback_seconds=300, lookback_start_at=15:05:39 (window start), exactly one point at 1790521839 = window end (L = window length contract holds). no_data views (fa63a1cd-t1, -t3): 0 series, source_start/end null, citable_as_fact=false.
- Trace views (5): traces_requested=20=query.limit on all; spans_shown=20=row count on all; spans_shown+spans_omitted==backend_spans_returned on all (676/676/247/407/676); incomplete_reason is a string iff incomplete=true (cadb191e-t3, fa63a1cd-t4; backend_traces_returned=20≥limit) and null on the other three (13 traces <20); status_state==('recorded' iff status_tags non-empty) on all 100 rows. checkout/payment/email searches all return the same 13 traces / 676 spans, consistent with observe-post (13 checkout traces in window).
- Coverage message: round-4 wire messages are not stored verbatim (only input_snapshot_hash/request_sha256), so the message was recomputed via the `run_coverage_message` rule from the delivered views: total 14; incomplete = cadb191e…-t3, fa63a1cd…-t4; truncated = none; non-ok = fa63a1cd…-t1 (no_data), fa63a1cd…-t3 (no_data). Report gaps 3 and 5 match this exactly. (Verbatim message: 未确认.)
- Observation (PRODUCT, optional): opspilot_incidents.state remains `queued` after publish; `persistence.py publish()` updates only `conclusion` and the run row, and the web layer derives "concluded" from `conclusion IS NOT NULL`. Consistent with code; not a criterion violation.

## Criteria
1. PASS — parseable m0-report-v2, assessment_status=completed, conclusion=partial, gaps non-empty; completion is real (report on the 4th and final model request, finish=stop), not a budget/connection failure dressed as completion.
2. PASS — core conclusion ("no failures observed in delivered evidence; elevated errors neither confirmed nor refuted; latency elevation not assessable without baseline") is supported by the delivered views; fact / hypothesis / rejected_hypothesis / gaps / next_steps are separated; no engineering answer leaked in.
3. FAIL — spans with status_state=not_recorded (empty status_tags) are written as carrying rpc.grpc.status_code=0 / http.status_code=200 in claims 3, 4, 7 and the summary (field omission vs telemetry absence confused); two min–max ranges do not match the view values (claims 3, 8). Counter/increase wording, absent-series-as-unknown, limit vs row counts, and incomplete-view counts are all handled correctly.
4. PASS — explicitly "not a complete call-chain or error-free certification"; no remediation, no release-gate language; only human follow-up suggested.
5. FAIL — unprocessed P2 findings below.

## Findings
P1: none.

P2 (all MODEL; every number below verified against the ledger/DB view content):
- P2-1 [claim 3, 044fbee8…-t3] "durations 23.4 ms-105.0 ms for PlaceOrder": PlaceOrder rows range 29,358 µs (a5174226…) to 105,007 µs (0d1e30bb…); 23,426 µs is the `HTTP POST` span in 7c9ce912…, not PlaceOrder. (The summary's "29-105 ms" is correct.) Class e (min–max range).
- P2-2 [claim 3, 044fbee8…-t3] "Sampled checkout spans … show … rpc.grpc.status_code=0 or http.status_code=200": 3 of 20 rows (prepareOrderItemsAndShippingQuoteFromCart in 6f69732d…, 7c9ce912…, 7411dd8b…) have status_state=not_recorded, status_tags={}. Class a.
- P2-3 [claim 4, cadb191e…-t2] "(operation grpc.oteldemo.PaymentService/Charge and charge) show … rpc.grpc.status_code=0": all 7 `charge` rows are not_recorded with empty status_tags; only the 13 Charge rows carry status 0. Class a.
- P2-4 [claim 7, fa63a1cd…-t5] "Sampled email spans show … http.status_code=200": 8 of 20 rows (`send_email`) are not_recorded; only the 12 `POST /send_order_confirmation` rows carry 200. Class a.
- P2-5 [summary] "Sampled spans for checkout, payment, product-catalog, cart, and email carried … grpc status 0 or HTTP 200": in the cart view (fa63a1cd…-t4) 17 of 20 rows are not_recorded (only 3 flagd ResolveBoolean rows carry status 0); no claim asserts a cart status, so this is a distinct over-statement. Class a.
- P2-6 [claim 8, fa63a1cd…-t4] "other cart operations 2.9-20.5 ms": the cart `POST` span in 0d1e30bb… is 41,798 µs (41.8 ms), outside the stated range (min 2,872 µs GetCart 89e119f1… and max 20,536 µs AddItem 4b6e2ff9… are otherwise correct). Class e (min–max range).

P3 (MODEL unless noted):
- [claim 1, 044fbee8…-t2] "Checkout's outbound gRPC calls in the window all record rpc_grpc_status_code=0": the 7 listed series are exactly the view's 7 series, all code "0"; "all" reads as a population statement, but gap 2 and the rejected_hypothesis ("every visible status code … not proof of zero errors") keep absent series as unknown. Wording only.
- [claim 8] "including a 50.0 ms and 43.1 ms flagd ResolveBoolean child": 50,035 µs is the EmptyCart server span itself; 43,127 µs is the flagd child. Attribution garbled, values correct.
- [claim 10, cadb191e…-t0] lists payment GET/dns.lookup/tcp.connect STATUS_CODE_ERROR series (value 0) then says "no ERROR series for checkout or its dependency services is present in that view" — payment is a dependency; the first half and gap 2 state it correctly.
- [PRODUCT, optional] incident.state stays `queued` after publish (see Record integrity); round-4 wire messages not persisted verbatim.

Verified correct (no finding): claims 2, 5, 6, 9, 11 values (11 UNSET checkout series; p-c 195 µs–1,642 µs; cart/shipping/email UNSET + currency OK; p95 167.5/4.75 and 23.8/7.1/61.2/4.75/4.75/22.9/4.75; totals 381.3/197.5/43.8/55.0/27.5+0/182.5/41.3); all cited trace ids exist in the cited views; spans_omitted 656/387/227 correct; "increase … totals" wording correct for 300 s selectors; no fact cites a no_data view (both no_data views appear only in a hypothesis and in gaps).

## P2 by class
- (a) empty status / not_recorded written as status 0 or OK: **4** — P2-2 (claim 3), P2-3 (claim 4), P2-4 (claim 7), P2-5 (summary).
- (b) absent series written as 0 / "no non-zero code": **0**. (b-svc) absent series in a by-service grouped query written as zero/no errors for that service: **0** — claims 2, 6, 10 say "appears/present in that view"; gap 2 states checkout error counts are "unknown, not zero"; the payment ERROR=0 series is explicitly returned.
- (c) trace `limit` confused with span row counts: **0**.
- (d) wrong count of incomplete/truncated views: **0** — 2 incomplete, 0 truncated, 2 no_data, all correctly named.
- (e) other — min–max duration range does not match view values: **2** — P2-1 (claim 3), P2-6 (claim 8).

## Cross-check vs observe-post.json
Ground truth: expect=normal, precondition.fault_confirmed=false, all_services_error_calls positive_series_count=0, checkout 11 series all UNSET, payment ERROR series value 0.0, Jaeger 13/13 checkout traces with zero checkout_error_spans and zero failed_dependency_child_spans. The report's core conclusion (no failures on checkout or dependencies in the delivered evidence; rejected failure hypothesis; error-free not certified) matches the ground truth; no observation contradicts it.

## Upstream-style verdict
Yes — the report found no failing status codes or error series for checkout and its six dependencies in the 300 s window and rejected the failure hypothesis, which observe-post confirms (0 error calls, 13/13 clean traces); it did not over-certify, calling the window "partial" rather than fault-free.

VERDICT: FAIL
P1=0 P2=6 P3=3
