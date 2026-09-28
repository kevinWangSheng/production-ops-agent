# normal-1 独立审查（全新上下文 Agent，Opus；原文照录，未经执行者改动）

Reviewer inputs: v4 packet criteria, PRODUCT-CONSTRAINTS, ADR-0005,模型可见的 REPORT_CONTRACT/RUN_COVERAGE_TEMPLATE/REPORT_RETRY_TEMPLATE 与工具描述，docs/tasks/2026-09-28-m1-01-upstream-alignment-b.md（B1-B6 背景），本目录，55431 只读 SQL。审查者未见执行者评估或其他审查者结论。本 Run 为无故障对照窗，问题文本为"for the last 5 minutes"（非固定 300 秒窗），模型自选查询窗口（B2）。

**VERDICT: PASS.** The normal-1 report meets all five v4 criteria. I found no P1 or P2 issues and three P3 wording issues. It correctly judged that there was no fault, and none of its evidence came from outside the Run's own period.

**Run facts** (from `ledger.json`): the Run completed with 5 of 100 model requests and 10 tool calls. There was no handoff and no compaction. The B3 retry and B5 duplicate short-circuit never fired. Every tool call was a read-only `traces_search` or `metrics_range_query`.

## Criteria 1–5

**1. Parseable final report — PASS.** The report parses as `m0-report-v2` with `assessment_status=completed` and `conclusion=partial`. It keeps the unresolved parts separate from the finished ones: the latency baseline and the payment metadata errors are listed in `gaps`. Nothing budget- or connection-related is dressed up as a completed result.

**2. Core conclusion supported by delivered evidence — PASS.** The conclusion is that checkout's PlaceOrder path shows no failure and that latency is unresolved.
- **Checkout traces** (`9b09…-t0:0971…`): all four traces are present in the view. Each has PlaceOrder with `rpc.grpc.status_code=0`, and each includes visible GetCart, EmptyCart, Convert, Charge, GetProduct, GetQuote, ShipOrder and send_order_confirmation spans with status 0 or 200. No span in the view carries an error.
- **Durations**: PlaceOrder 44.1/53.5/86.7/463.4 ms and frontend `/api/checkout` 52.6/60.5/111.5/514.3 ms match the view exactly.
- **Claim kinds**: facts, a hypothesis (GCP metadata lookup), a rejected hypothesis (Charge failures) and counter-evidence (metric zeros under-report errors) are each labelled as such.

**3. Citations, sources and windows consistent — PASS.**
- **Windows**: every claim describes its own evidence's actual window correctly. Claims on the 1-hour views say "whole-hour" or "last-hour". The 1-hour payment view `f7b3…-t2` is cited for spans at 12:55:50.855–.861Z, which falls inside the 5-minute window claimed.
- **Truncation counts**: omitted-span counts match the views. Checkout: 24 omitted, 4 traces. Payment: 36 omitted. Product-catalog: 124 omitted, 20 of 20 traces, `incomplete_reason` quoted.
- **Limit vs. rows shown**: the report never confuses a `limit` of 20 with rows actually shown.
- **Missing series**: gap 3 correctly says that checkout, product-catalog, cart, currency, shipping and email have no ERROR series, so their error state is unknown rather than zero.
- **Latency figures**: p99 491.2 ms and p95 456.1 ms come from `rate[300s]` evaluated at 12:58:03, so they really are 5-minute values. The statement that each query returned a single point is confirmed against the raw bytes in PG (`opspilot_evidence.raw`: every series has 1 point), so the projection dropped nothing.
- **Payment counter-evidence**: this claim is correct. The 300s and 3600s increases are identical to many decimals, and the earliest span anywhere in the 1-hour frame is 12:55:50Z. That points to freshly created counters, whose first sample does not register as an increase.

**4. No unfounded health claim, no remediation, no release gate — PASS.** The report says the premise is "not supported" on the checkout path and does not certify health. Its `next_steps` are advisory only, and it took no actions.

**5. No unresolved P1/P2 — PASS.** Findings below.

## Findings (all P3, all model report defects; none is a product defect)

1. **Claim 1 and the summary misdescribe the 5-minute metric view.** They say the view returned "only STATUS_CODE_UNSET, plus STATUS_CODE_OK for currency". The view `9b09…-t1:c421…` also contains `{payment, STATUS_CODE_ERROR} = 0`. The second half of the sentence ("no ERROR value above zero") is correct, and gap 3 and claim 5 correctly say payment has an ERROR series. This is borderline P2 under "visible scope"; I put it at P3 because the meaning does not change and the report corrects it elsewhere.
2. **Claim 3 overstates which error spans are root spans.** It says the payment "error spans are root spans (no parent reference)". In view `f7b3…-t2`, only the two `GET` spans have no parent. The `tcp.connect` and `dns.lookup` error spans are children of those `GET` spans in the same traces. The conclusion still holds, because both traces are rooted in payment's own `GET` and are not linked to checkout. This is borderline on the packet's "same trace/parent relation" wording; P3 for the same reason.
3. **Claim 7 presents two nested spans as separate contributors.** It says trace 178b… was "dominated by" email `send_order_confirmation` (140.7 ms) "and" HTTP POST (164.8 ms). The email span's parent is checkout's HTTP POST span `d92df88e…`, so 140.7 ms is contained inside 164.8 ms, not added to it. P3.

## P2 by class
a=0 b=0 b-svc=0 c=0 d=0 e=0 (total 0).

## Window-contamination check
**No contamination found.**
- **Query windows**: every cited view's actual window, taken from `ledger.evidence[].view.window`, is either 12:53:03–12:58:03Z (`9b09…-t0` and `-t1`) or 11:58:03–12:58:03Z (the other eight).
- **Fault episodes**: every window ends at 12:58:03Z. The only fault episode today, `paymentFailure`, ran 13:03:39–13:16:48Z. productCatalogFailure and cartFailure come later still. No window overlaps any of them.
- **Where the data starts**: the earliest span in any 1-hour trace view is 12:55:50.855Z, and the metric series only begin around then. So the environment held no older data for the report to pick up.
- **The payment metadata errors**: they appear at 12:55:50Z and are the earliest spans present. They are ENOTFOUND for `metadata.google.internal` and ECONNREFUSED on 169.254.169.254, and their pattern matches a payment startup lookup, not an injected fault. The report labels them unrelated to checkout and states that link only as a hypothesis, so they did not turn into a false alarm. The engineer-side check in `observe-pre.json` also shows the payment ERROR increase at 0.

## Upstream-style verdict
**Correctly judged no fault.** `observe-pre.json` shows `precondition.fault_confirmed=false` and `jaeger.traces_with_checkout_error_and_failed_dependency_child=0`. Its three checkout traces (178b…, 13cd…, 18f1…) are among the report's four, and all are error-free in both sources.

VERDICT: PASS
P1=0 P2=0 P3=3
P2 by class: a=0 b=0 b-svc=0 c=0 d=0 e=0
Window contamination: no contamination found (all windows end at 12:58:03Z, before paymentFailure at 13:03:39Z; no data before 12:55:50Z exists)
Upstream-style: correctly judged no fault (observe-pre `fault_confirmed=false`, `traces_with_checkout_error_and_failed_dependency_child=0`; report says no checkout-path failure)
