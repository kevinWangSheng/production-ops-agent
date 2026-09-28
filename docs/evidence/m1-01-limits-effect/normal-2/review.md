# normal-2 独立审查（全新上下文 Agent，Opus；原文照录，未经执行者改动）

Reviewer inputs: v4 packet criteria, PRODUCT-CONSTRAINTS, ADR-0005, the model-visible REPORT_CONTRACT/RUN_COVERAGE_TEMPLATE and tool descriptions, docs/tasks/2026-09-28-m1-01-loop-limits.md (limits-change background), this directory, read-only SQL on 55431. The reviewer did not see the executor's assessment or any other reviewer's output.

The report fails the v4 packet. Criteria 1, 2 and 4 pass; criteria 3 and 5 fail because of three P2 errors in the trace details. All three are model report errors, not product defects. On the question that matters most, the report is correct: it judged that there was no fault in this window.

**Run facts (ledger.json / report.json):**
- **Termination:** the model returned the report on its own at round 4, using 4 of 100 requests, with no forced final request. Execution was `completed`, handoff was `false`.
- **Evidence:** all 13 evidence rows have status ok and belong to this Run, and each raw sha256 matches its recomputed hash.
- **Report integrity:** the report's sha256 and byte length (12156) match, and parsing the text reproduces the parsed object exactly.
- **Incident:** stays `lifecycle: open`.

## Criteria

1. **PASS.** The report parses. `assessment_status=completed` with `conclusion=partial` fits the contract: no errors were found, but latency could not be judged against a baseline. No budget or connection failure is involved.
2. **PASS.** The core conclusion is that the evidence "does not corroborate" elevated checkout errors. The delivered views support it:
   - The error-series query (`50d01…-t0`) returns only frontend, frontend-proxy, load-generator and payment series, all with value 0.
   - The client RPC counts for checkout (`50d01…-t1`) all carry `rpc_grpc_status_code` 0.
   - Every visible span has `error_by_visible_tags=false`.
   - Facts, counter-evidence, rejected hypothesis, hypothesis and recommendation are kept apart.
3. **FAIL.** Every evidence id, `target_refs=m0-otel-20260909` and `policy-window-1` exists and belongs to this Run. Most numbers check out: the span-metric increases 7.5/22.5/15.0, the RPC counts 6.25/20.0/13.75, the p95s 46.875/30/23.75/8.75/4.75, payment USD 4.9998 and CAD 0, "20 of 312 spans shown, 292 omitted", "6 backend traces", PlaceOrder 49–139 ms, and the single cart http series. The count of incomplete views is right: exactly two, product-catalog `t2` and cart `t3`. Missing series are correctly called unknown rather than zero. Trace limit and rows shown are not confused. Three P2 errors remain (below).
4. **PASS.** The report explicitly says "not certification of health". No repair or action was taken, and the incident stays open.
5. **FAIL.** Three P2 findings are unresolved.

## Findings

**P2 (3), all model report errors:**
- **P2-1, class (a).** Claim 2 says checkout's children "prepareOrderItemsAndShippingQuoteFromCart (63873 us), …Charge (35757 us), …EmptyCart (19329 us) also status 0". In view `ee88…-t0`, span `85822753a2064fdd` has empty `status_tags` and `status_state=not_recorded`. A missing status is written as status 0. Claim 1 handles the same span correctly, so this is inconsistent within the report.
- **P2-2, class (e).** Claim 7 says payment `grpc.oteldemo.PaymentService/Charge` spans have "durations 943-11037 us". In view `ee88…-t1`, 943 us belongs to the child span `charge` (`0efeb60e5e9ab784`, not_recorded). The actual Charge server spans range from 2037 us (`9ba9f75f9278c37c`) to 11037 us. The minimum is taken from the wrong span.
- **P2-3, class (e), visible-scope error.** The summary says "the 6 sampled checkout traces … show a completed load-generator -> frontend-proxy -> frontend -> checkout PlaceOrder path ending in HTTP 200". In the delivered views, load-generator, frontend-proxy and frontend spans appear only for trace `1be8524e…` (views `t1`, `t4`, `t5`). In the other 5 traces, checkout's PlaceOrder parent has `parent_is_visible=false`. The raw bytes (load-generator `span_count` 6) suggest the claim is true, but the model never saw that. This matters less than the other two.

**P3 (4):**
- The summary and claim 1 say all 20 checkout spans carry "rpc.grpc.status_code 0 or no status tag". Span `18e8e72d10cb6e47` (HTTP POST) actually carries `http.status_code 200`. It is still not an error, so the meaning holds.
- Claim 6 lists the flagd NaN series under "checkout's client-call" p95. Those series belong to the ad and fraud-detection services.
- Claim 8 says the counter "increased … only for USD (4.9998) and CAD (0)". CAD did not increase.
- The rejected hypothesis says "no dependency span shown carries an error tag" but cites only trace view `t1` among the span views. Claim 7 cites the other dependency views, so the report as a whole holds up.

**Product vs model:** no product defect found. Evidence binding, projection counts (`backend_*`, `spans_shown`, `spans_omitted`, `incomplete`) and the executor all behaved correctly. The payment, currency, shipping and email trace searches each returned the same 6 traces as the checkout search. That is Jaeger's own behavior: a direct read-only query to Jaeger for currency and payment over the Run window returns the same 6 trace ids. One thing I did not confirm: the incident row shows `state: queued` after the Run was published. I didn't check whether that field is supposed to change after publication.

## No-fault verdict
**Correctly judged no fault.** The report finds no failure and says the evidence "does not corroborate" elevated errors, while stopping short of certifying health. This matches `observe-pre.json`: `precondition.fault_confirmed=false` and `jaeger.traces_with_checkout_error_and_failed_dependency_child=0`. The raw bytes of `ee88…-t0` also show `error_spans_by_listed_status_tags` = 0 for every service.

VERDICT: FAIL
P1=0 P2=3 P3=4
P2 by class: a=1, b=0, b-svc=0, c=0, d=0, e=2
Upstream-style verdict: correctly judged no fault
