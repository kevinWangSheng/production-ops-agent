# fault-1 独立审查（全新上下文 Agent，Opus；原文照录，未经执行者改动）

Reviewer inputs: v4 packet criteria, PRODUCT-CONSTRAINTS, ADR-0005，模型可见 REPORT_CONTRACT/RUN_COVERAGE_TEMPLATE/REPORT_RETRY_TEMPLATE 与工具描述，docs/tasks/2026-09-28-m1-01-upstream-alignment-b.md（B1-B6 背景），本目录，55431 只读 SQL。审查者未见执行者评估或其他审查者结论。

I judge this Run a **FAIL** under the v4 packet. The core conclusion is correct and well supported: checkout PlaceOrder fails because payment Charge rejects the request. But the report has 3 unresolved P2s, all model defects, which fails criteria 3 and 5. I found no product defect and no contamination from any other window or fault episode.

**Actual query windows.** All 12 views cited (`ledger.json` `tool_results[].result.window`) cover 2026-09-28T13:06:27Z–13:11:27Z. The model chose this window itself (B2), inside the 24h outer frame. Both trace views returned spans from 13:10:00.48 to 13:10:47.73. All metric points are a single evaluation at 13:11:27 (epoch 1790601087) with `[5m]`, so they also cover 13:06:27–13:11:27. The Run finished in 4 requests with no B3 retry, no handoff, and state `completed`.

## Criteria
1. **PASS.** The report is parseable (`m0-report-v2`), `assessment_status=completed`, `conclusion=partial`, `handoff=false`, 4 of 100 requests used, `run_completed published=true`.
2. **PASS.** The core conclusion is supported by trace view `56549a44…-t0`. All 5 checkout PlaceOrder spans are ERROR with rpc.grpc.status_code 13 and the description "failed to charge card … Invalid token". The payment `grpc.oteldemo.PaymentService/Charge` server spans are ERROR. The parent references checkout PlaceOrder → checkout Charge client → payment Charge are visible in all 5 traces. The causal chain is correctly kept as a hypothesis, and facts, hypotheses, rejected hypotheses, counter-evidence and gaps are kept apart.
3. **FAIL.** Duration ranges mix client-side and server-side spans, and one span with no recorded status is presented as a good status (F1–F3 below). The report does correctly separate the 20-trace limit from the 5 traces returned and the 151 of 159 spans shown (8 omitted). It also keeps `no_data` distinct from an explicit zero: checkout `status_code="ERROR"` and payment `rpc_client…` come back `no_data`, while the email spans are real zero series.
4. **PASS.** It does not certify health or recovery and does not attempt remediation. All next steps are "Have a human…". Email is left unknown.
5. **FAIL.** Three P2s are unresolved.

## Findings
- **F1, P2 class (a), model defect.** Claim 5 says "the corresponding cart … server spans also show rpc.grpc.status_code=0 / http.status_code 200". Every cart `POST /oteldemo.CartService/GetCart` server span in the view has `status_state=not_recorded` and `status_tags={}`. The tool description (`opspilot/tools/otel_demo.py:355-357`) says not_recorded means "Unset …, not status code 0 and not OK". The cart spans carry no status tag at all, so "status code 0 / 200" is not in the evidence.
- **F2, P2 class (e), model defect.** Claim 7's duration ranges are wrong or mix span types:
  - Currency Convert "1,435–38,044 us" matches neither set. Checkout's client spans run 3,623–38,044; currency's server spans run 923–22,371.
  - Cart GetCart "1,317–67,385" matches neither. Client spans run 4,806–67,385; server spans run 1,024–4,181.
  - Product-catalog GetProduct "339–12,573" is the server minimum joined to the client maximum. Server spans run 339–979; client spans run 628–12,573.
  - PlaceOrder and the checkout-side Charge ranges are correct.
- **F3, P2 class (e), model defect.** The summary says "checkout PlaceOrder server spans 127–348 ms, payment Charge client spans 12–137 ms".
  - 127.8 ms is frontend's PlaceOrder client span; checkout's own server minimum is 123.2 ms.
  - 12.0 ms is the payment server-span minimum; the client minimum is 37.3 ms.
  - The summary therefore contradicts the report's own claim 7.
- **P3, model.** Claim 4 says frontend `POST /api/checkout` spans show `error=true`. They carry only `http.status_code 500`.
- **P3, model.** Claim 3 lists 4 traces with a failed payment server Charge span. All 5 have one, including c4142662…; the omission understates rather than overstates.
- **P3, model.** The counter-evidence treats "UNSET increase 0 for PlaceOrder" and "app_payment_transactions_total increase 0" as not supporting the failure. A zero under UNSET, and zero transactions, are equally consistent with every call failing. The real gap is the missing ERROR series, which the report does record in its gaps.
- **P3, model.** The summary's shorthand "5 of 20 traces" reads as if 20 existed. The gap text says it correctly: "5 of 20 requested".

**P2 by class:** a=1 b=0 b-svc=0 c=0 d=0 e=2 (total 3).

## Window-contamination check
**No contamination found.** Every cited view covers 13:06:27–13:11:27Z, entirely inside the `paymentFailure` injection (13:03:39Z–13:16:48Z). The trace spans fall at 13:10:00–13:10:47Z. No productCatalogFailure or cartFailure had been injected by then.

The low-traffic artifact does touch the window's first part (up to about 13:10Z). The evidence is sparse: only 5 traces in the window, and all of them after 13:10. The PlaceOrder span metric shows increase 0, and the product-catalog GetProduct p95 of about 3975 ms is plausibly contention noise. The report did not over-attribute any of this: it kept product-catalog latency as a hypothesis with counter-evidence and did not call it elevated.

## Upstream-style verdict
**Root cause correctly identified.** observe-pre.json (window 13:06:19–13:11:19) reports 5 traces, `traces_with_checkout_error_and_failed_dependency_child=5` and `failed_dependency_services=["payment"]`. It lists the same 5 trace ids the report cites, with the same "Invalid token" error.

VERDICT: FAIL
P1=0 P2=3 P3=4
P2-by-class: a=1 b=0 b-svc=0 c=0 d=0 e=2
Window contamination: no contamination found. All cited views cover 13:06:27–13:11:27Z, inside the paymentFailure window (13:03:39–13:16:48Z); evidence is sparser because of the low-traffic artifact but not wrong.
Upstream-style: PASS. Root cause payment Charge failure ("Invalid token"), matching observe-pre.json's 5/5 traces with failed dependency payment.
