# cart-fault 独立审查（全新上下文 Agent，Opus；原文照录，未经执行者改动）

Reviewer inputs: v4 判据, PRODUCT-CONSTRAINTS, ADR-0005，模型可见 REPORT_CONTRACT/RUN_COVERAGE_TEMPLATE 与工具描述，docs/tasks/2026-09-28-m1-01-alignment-c.md（C1-C3 背景），本目录，55431 只读 SQL。审查者未见执行者评估或其他审查者结论。留出场景（cartFailure），第三个也是最后一个故障，需核查是否与更早两个（paymentFailure、productCatalogFailure）事故混淆。

The cart-fault Run fails the v4 packet on one P2 finding. The report finds the cart root cause correctly and has no contamination from the earlier fault episodes. The P2 is a wrong causal claim about why cart failures are missing from the error metric.

**Verification method:** I recomputed the checks from the stored views in `ledger.json`. I compared them with `observe-pre.json` and the fault timeline. I also ran one read-only Prometheus range query after the Run to check the metric-series finding.

## Criteria

1. **PASS.** The report parses and is marked completed with conclusion "partial". It is the step 3 output, sha `84686e154cb4…`, which matches `report_text` exactly. No handoff and no budget failure: 4 of 100 model requests, 5 tools, stopped on its own.
2. **PASS for the core conclusion.** The cart EmptyCart failures, trace ids, span ids, durations and the redis error detail all match evidence views t0 (checkout) and t0 (cart) exactly. Facts, hypotheses and counter-evidence are kept apart. One exception, covered under criterion 3: a cause stated as fact in the summary and gaps.
3. **FAIL.** A missing metric series is explained as cart's telemetry recording failures as UNSET (the P2 below).
4. **PASS.** Nothing is repaired, no release decision is made, no recovery is certified. Next steps are all human follow-ups. One soft "normal-range" wording (P3).
5. **FAIL.** One P2 remains open.

## Findings

**P2-1 (class e, causality; model defect).** The report states as fact, in the summary, the gaps and claim 5, that "Cart spans are recorded with STATUS_CODE_UNSET in span metrics, so the redis failures are invisible in the ERROR metric series". The rejected hypothesis leans on the same idea. The next steps then recommend changing cart instrumentation so failing spans carry ERROR.
- In the Run's own trace views, the failing spans do carry ERROR:
  - cart server spans `f0657348ee8ec88e` and `ee2cffc5d0a3a4cd` have `otel.status_code=ERROR`;
  - checkout client spans `ac71913cd29e2776` and `680e1214c34a70f6` also have `otel.status_code=ERROR`.
- The UNSET series in the 5-minute metric view only count the successful cart spans.
- The ERROR series was missing from the 5-minute view (evaluated at 18:13:10) because the counter had only just been created. A post-hoc Prometheus read shows `traces_span_metrics_calls_total{service_name="cart", span_name="POST /oteldemo.CartService/EmptyCart", status_code="STATUS_CODE_ERROR"}` = 1 at the 18:12:30 step and 2 by 18:13:30. The checkout EmptyCart ERROR series shows the same values.
- `observe-pre.json` also has no checkout EmptyCart ERROR series.
- So a series that was absent at query time was turned into a claim about how cart is instrumented, plus a wrong recommendation. The literal wording in claim 5 ("not represented in any ERROR series of this metric") is true of the view. The overreach is in the summary, the gaps and the next steps.

**P3-1 (model).** The summary calls payment, currency, shipping and email durations "normal-range" without a baseline. The only baseline available, the preceding window, has GetQuote max 26,145 us and Charge max 27,262 us. The latest window has 53,832 us and 43,715 us.

**P3-2 (model).** Claim 7 says the load-generator, frontend-proxy and frontend spans in both earlier product-catalog traces carry `http.status_code=500` with `otel.status_code=ERROR`. The frontend `POST /api/checkout` span has only `http.status_code=500` and no otel ERROR tag.

**P3-3 (model).** Claim 6 calls the 30-minute metric "the only supplied evidence" for the reported checkout errors, but claim 7 cites the checkout PlaceOrder errors in the preceding trace view. Separately, the gaps and next steps name only the product-catalog flag as a candidate earlier episode. They leave out the payment Charge errors (8.41), which account for most of the 11.56 PlaceOrder errors over 30 minutes. This is an omission, not a wrong number.

No P1 findings. No product defects found.

## Window-contamination check

These windows include earlier fault episodes:
- **30-minute error metric (17:43:10–18:13:10):** fully contains the paymentFailure episode (17:45:17–17:51:32) and the productCatalogFailure episode (17:58:58–18:05:57).
- **Preceding checkout trace view (18:03:10–18:08:10):** overlaps productCatalogFailure. Traces `896af2bc…` and `d11a8eda…` start at 18:03:34 and 18:03:50, inside that episode.

The report handles them correctly. It puts the 30-minute errors and the "Product Catalog Fail Feature Flag" traces outside the latest 5 minutes and never assigns them to cart. Both cited cart-failure traces started after cartFailure was injected at 18:11:03: `6748b618…` at 18:11:09 and `94df17f1…` at 18:13:03. The latest-window views have no errors from either earlier episode. **No contamination found.** The only related gap is P3-3, where the payment episode is not named separately.

## C2 (repair retry)

**Not triggered.** There are 4 model requests and only one response shaped like a conclusion (step 3, the stop round). Its content is identical to the accepted report. No second attempt exists.

## C3 (span_groups)

- **Correctness:** I recomputed `span_groups` from the shown rows for all three trace views. They match exactly. Row totals equal `spans_shown` (164, 167, 161), groups are sorted by service and operation, and each view carries `span_groups_note`.
- **Use:** the report's counts are span_groups values, written in its "rows" wording. Examples: Charge 8 rows, max 43,715 us; Convert 24 rows, max 10,459 us; EmptyCart 2 errors in 8 rows; baseline PlaceOrder 7 rows, 7,264–134,097 us; cart operations max 20,969 us. The model's reasoning text is redacted, so I can't prove it read span_groups rather than the raw rows. The citations don't name the field.
- **Accuracy:** every value the report uses is correct.

## Root cause

**Correct.** The report finds cart EmptyCart failing with FailedPrecondition, "Can't access cart storage… Wasn't able to connect to redis" (ValkeyCartStore). `observe-pre.json` agrees: 2 traces with a checkout error and a failed dependency child, `failed_dependency_services: ["cart"]`, trace `6748b6188aa8ffc65edbd39fb6d28930`, spans `ac71913cd29e2776` → `f0657348ee8ec88e`, same error text.

VERDICT: FAIL
P1=0 P2=1 P3=3
P2-by-class: a=0 b=0 b-svc=0 c=0 d=0 e=1
Window contamination: no contamination found (the 30-min metric and preceding trace view overlap earlier episodes, but the report keeps them separate and never assigns them to cart)
C2 retry: not triggered
C3 span_groups: present and exactly correct in all 3 trace views; the report's per-operation counts match span_groups values and are correct (use inferred, since reasoning is redacted)
Upstream-style verdict: correct root cause (cart EmptyCart / redis connectivity); the traces and error text match `observe-pre.json`
