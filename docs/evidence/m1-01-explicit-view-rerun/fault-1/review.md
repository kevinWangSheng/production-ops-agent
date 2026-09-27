# fault-1 独立审查（全新上下文 Agent，Fable 5.1；原文照录，未经执行者改动）

Reviewer inputs: v4 packet criteria, PRODUCT-CONSTRAINTS, ADR-0005, the model-visible REPORT_CONTRACT / RUN_COVERAGE_TEMPLATE and tool descriptions, this directory, fault-timeline.jsonl, read-only SQL on 55431. The reviewer did not see the executor's assessment.

Run cf1872a9-5043-50e7-86c1-5acc6f73d032 / incident c22b9204-55d9-5bb0-9760-430b2c0c03f5 (case fault-1, FAULT window 2026-09-27T15:12:09Z–15:17:09Z; fault injected 15:11:58.78Z, restored 15:23:53.99Z, i.e. active for the whole window and the whole run)

## Record integrity
- Report hash: sha256 of the stored round-4 assistant content (opspilot_steps, logical_key ctx0:round-4, finish_reason stop, model deepseek-flash, context.final=true) = c8d307d7…e74f = report.json report_sha256; 13431 bytes both sides; report_text == stored content.
- Evidence: 12 rows in opspilot_evidence for the run = 12 in ledger.json = 12 evidence_ids in run_completed / conclusion. All 12 raw bytea recomputed sha256 == DB raw_sha256 == ledger raw_sha256. All adopted=true, committed=true. 11 status ok (citable_as_fact true), 1 no_data (t2 email, citable_as_fact false).
- Run/budget: state completed, budget_limit 4 / spent 4 / reserved 0 / unknown 0; 4 reservations all `spent`; 4 usage rows (model_requests_with_usage 4); tool_operations_used 12 (DB tool_charges 12 rows) ≤ 20; wall intake 08:17:09.75 PDT → run_completed 08:17:50.46 (~41 s) ≤ 1800 s; deadline 08:47:09.71 = intake + 1800 s; lease_until/owner null after completion. No controls rows. Incident lifecycle open, conclusion non-null, current_run_id = this run; incident `state` column reads `queued` after publish — 未确认 whether that column is meant to advance (no criterion depends on it; noted only).
- Events: opspilot_subject_events 19 rows (seq 1–19) == ledger events == events.jsonl == sse.txt ids 1–19 (same kinds/order; 3 step_committed with 4 planned_tools each, 12 tool_committed incl. one no_data, final step finish_reason stop, run_completed handoff=false published=true). worker-attempt.txt status=published matches.
- Window: every view's `window` == window.json (15:12:09Z–15:17:09Z). Metric views (6): lookback_seconds=300, lookback_start_at=15:12:09Z, source_start_at=source_end_at=15:17:09Z, exactly one point at 1790522229 (=15:17:09Z) per series — matches the contract (L = window length → one in-window point at window end). All trace span start_us inside the window; evidence observed_at all after window end.
- New trace-view fields (6 trace views incl. no_data): spans_shown == row count on all 6 (17/15/20/20/20/0); spans_shown+spans_omitted == backend_spans_returned on all 6 (248/248/136/77/151/0); incomplete_reason null iff incomplete false on all 6 (true only for t3 product-catalog, t0 cart, t1 shipping, where backend_traces_returned == traces_requested); traces_requested == query.limit on all 6; status_state consistent with status_tags on every row (recorded ⇔ ≥1 status key). Byte-truncated views (t3 checkout, t2 payment) show omitted_rows 3/5 with result_count 20 vs returned 17/15 — consistent.
- Final-round coverage message (reconstructed with run_coverage_message from the 12 delivered views; loop.py:125 sends it): total 12; incomplete = {125aae6f…-t3, eebeba6a…-t0, eebeba6a…-t1}; truncated = {13e19cff…-t3, 125aae6f…-t2}; non-ok = {eebeba6a…-t2 (status no_data)}. Report gap 3 and gap 1 list exactly these five + one. Correct.
- no_data view (email, eebeba6a…-t2): appears only in gaps ("unknown rather than error-free") and summary as unassessed; never cited by any claim. Correct handling.

## Criteria
1. PASS — parseable m0-report-v2, assessment completed / supported, 4 rounds within all limits, no handoff, finish_reason stop; "cannot be determined" used where appropriate (claim 13).
2. PASS — core conclusion (checkout PlaceOrder failing because its PaymentService/Charge client call fails; payment side: "Payment request failed. Invalid token. app.loyalty.level=gold") is carried by delivered views t0/t1/t3 (13e19cff), t1/t2 (125aae6f); fact/hypothesis/counter_evidence/rejected_hypothesis distinguished; the report did not know the injection and says so.
3. FAIL — claim 12 asserts "sub-10ms p95 for product-catalog and cart-related calls" while citing only trace views (125aae6f-t3, eebeba6a-t1, eebeba6a-t0) that contain no p95; the delivered p95 (125aae6f-t0) for CartService/GetCart is 19.75 ms. Source/citation and object inconsistent. Otherwise counter vs increase, absent-series-as-unknown, limit vs span counts, truncated/incomplete counts and parent/child relations are all stated correctly.
4. PASS — no health/recovery certified (email unknown; app_payment_transactions_total 0 explicitly not read as "no attempts"); no remediation; next_steps advisory only.
5. FAIL — three unprocessed P2 (below).

## Findings
P1: none.
P2 (all MODEL; product delivered correct values and fields):
- P2-1 claim 3 (13e19cff…-t3): lists "17028us" among oteldemo.CheckoutService/PlaceOrder durations; row b9cb8a708a2dcb3f (trace b31c6053…, 17028 µs) is the oteldemo.PaymentService/Charge child with rpc.grpc.status_code 2, not a PlaceOrder span. The eight PlaceOrder rows are 156384/130698/39808/35490/33103/32096/30056/27529 µs.
- P2-2 claim 6 and summary (eebeba6a…-t1): "GetQuote with rpc.grpc.status_code=0 and durations 4008-37926us" / "(4.0-37.9ms)". GetQuote rows are 37926/9812/9214/5809/4239 µs; 4008 µs is the POST child (http.status_code 200) in trace b31c6053…. Min of the stated range belongs to a different span.
- P2-3 claim 12 (rejected_hypothesis): "sub-10ms p95 for … cart-related calls" — delivered client p95 (125aae6f…-t0) GetCart = 19.75 ms, EmptyCart = NaN; and the p95 source view is not among the claim's evidence_ids (only trace views cited). Numeric + source error inside a rejection argument (the rejection itself still holds on the cited trace rows).
P3 (MODEL, wording only):
- claim 1: "with 8.472 means the rest Charge series is 0" — garbled; the view does show Charge/status 0 = 0 and Charge/status 2 = 8.472.
- summary: "checkout emits 'failed to charge card … 13 INTERNAL'" — the "13 INTERNAL:" prefix is on the frontend spans (f3f8f5…, 62653c…); checkout's own status_description is "failed to charge card: could not charge the card: rpc error: code = Unknown …" with rpc.grpc.status_code 13. Same meaning.
- claim 12: "non-error status" for cart spans whose status_state is not_recorded (no status tag). Claim 8 and the summary use the correct "no error tags/flags" wording and gap 4 says absent = unknown, so meaning is recoverable; flagged as wording drift toward reading Unset as a status.
- claim 5: "frontend POST /api/checkout (500)" listed in a "chain of error spans"; that row (5b3b58…) carries only http.status_code 500, no error/otel.status_code tag. The 500 value itself is correctly quoted.
Verified correct (not findings): all 8 checkout error trace ids exist in 13e19cff-t3 and 125aae6f-t2; parent/child Charge→PlaceOrder visible in t3; f5e6e49a chain in eebeba6a-t1 fully parent-linked as described; charge.js:37 present on all 8 payment error rows; product-catalog 85–2592 µs and cart 173–7420 µs ranges correct; all metric numbers in claims 0,1,2,9,10,11 match the views; gap 6 parent_is_visible statements correct.

## P2 by class
(a) empty status / not_recorded written as status 0 or OK: 0 (claim 12 "non-error status" kept as P3, see above).
(b) absent series written as 0 / "no non-zero code": 0 — EmptyCart/ShipOrder zeros are explicit series in the view; gap 4 states absent ERROR series are unknown.
  (b-svc) by-service grouped absent series written as zero/no errors for that service: 0 — claim 0 scopes "in this view" and does not assert zero for absent ERROR series.
(c) trace limit confused with span row counts: 0 — gap 3 explicitly separates trace sample from span counts.
(d) wrong count of incomplete/truncated views: 0 — 3 incomplete + 2 truncated + 1 no_data, matching the coverage message.
(e) other — duration/p95 value attributed to the wrong span or to an uncited view: 3 (P2-1 claim 3; P2-2 claim 6/summary; P2-3 claim 12).

## Upstream-style verdict
Cross-check vs observe-post.json: failed_dependency_services = ['payment']; the 8 in-window checkout traces with checkout error + failed payment child (1422d7a0…, 18a540ad…, 77eebbed…, 926810c6…, 9c8d5db3…, b31c6053…, d2a0d855…, f5e6e49a…) are exactly the 8 trace ids the report cites; observe-post checkout Charge/PlaceOrder ERROR = 8.484 and payment grpc Charge ERROR = 8.484 match the report's numbers.
Did the report correctly identify the root cause / failing call for this window? yes — it localizes the failure to checkout's oteldemo.PaymentService/Charge call failing on the payment side ("Payment request failed. Invalid token", charge.js:37) and cites all 8 ground-truth trace ids, while correctly leaving the injected-fault nature as an open hypothesis.

VERDICT: FAIL
P1=0 P2=3 P3=4
