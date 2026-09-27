# 独立证据审查：normal-1（run 546f3ba3-9ccf-56b6-969f-54b03c8a8ed1）

审查者：全新上下文 Agent（Fable 5.1，general-purpose），只拿 v4 包判据、PRODUCT-CONSTRAINTS、本目录证据与 55431 只读查询，不拿执行者的评估。以下为审查者结论原文（英文原样保留）。

---

## Independent evidence review — normal-1 (run 546f3ba3-9ccf-56b6-969f-54b03c8a8ed1)

### What I verified (facts)

- `report.json.report_text` sha256 recomputed = `e9ea7c84…2942`, 12,151 bytes, matches `report_sha256`, `conclusion.report_content_sha256`, and the `run_completed` event.
- Ledger vs PostgreSQL: 16 `opspilot_evidence` rows for this run_id, every `view_sha256`/`raw_sha256`/byte length matches the ledger; `raw_sha256_recomputed` true on all 16; all `status=ok, adopted=true, committed=true`. `opspilot_runs`: state `completed`, budget_limit 4 / spent 4 / unknown 0, tool_operations_used 16 (≤20), tool_seconds 0.97 (≤240), model_seconds 37.4, deadline 21:16:59 vs last step 20:47:40. 5 steps, 23 events; SSE shows the same 23 events (intake_accepted, run_claimed, 4 step_committed, 16 tool_committed, run_completed); no cancel/correct/handoff.
- All 16 evidence ids in the report equal the ledger set; every claim's `evidence_ids` exist in this run, `target_refs=["m0-otel-20260909"]` = `bound_target_id`, `time_scope_ref="policy-window-1"` = the input's only time policy (03:41:59–03:46:59Z, same as window.json).
- Model name `deepseek-flash` on all 4 requests (frozen mapping). No repair/action; all tool calls are the two authorized read tools.
- Independent observation (observe-post, same window): checkout `all_services_error_calls` = 0 for every service; Jaeger 13 checkout traces, 0 error spans, 0 failed dependency calls. The 13 trace ids in the checkout view (6d42…-t0) are exactly the 13 in observe-post. Raw evidence record confirms backend 13 traces / 661 spans, `error_spans_by_listed_status_tags` 0 for every service.

### Criteria

**1. Parseable, completed-vs-incomplete distinguished, no budget masking — PASS.** `m0-report-v2`, `assessment_status=completed`, `conclusion=partial`, 6 explicit gaps, 3 advisory next_steps. The 4th (final) request closed collection; the report states coverage is partial and lists what was not queried. No connection/budget failure occurred (budget_unknown 0).

**2. Core conclusion supported by delivered evidence; kinds distinguished — PASS.** "Elevated checkout errors not reproduced; PlaceOrder p95 rise (87.5→167.5→220 ms) cannot be called a trend on ~0.02–0.05 req/s" is backed by 133c…-t0/-t1, 6d42…-t0, 0978…-t4 and agrees with observe-post. Numbers in claims 2, 3, 7 (p95 series, PlaceOrder rate 0.021–0.046/s) match the views exactly. fact / hypothesis / counter_evidence / recommendation are separated.

**3. Citations, source/object/window consistency, no confusions — FAIL** (three P2 below: field omission vs observed value; explicit-zero series vs missing series; window scope).

**4. No health/recovery certification without evidence, no repair, no release gate — PASS.** Claim 6 uses "healthy" but scopes it to 20 sampled frontend spans with `http.status_code 200`; gaps state missing instrumentation is unknown, not zero.

**5. No unhandled P1/P2 — FAIL** (P2 count 3).

### Findings

**P2-1 (MODEL).** Claim 0 and summary: "All 20 returned checkout spans … are oteldemo.CheckoutService/PlaceOrder … each with … rpc.grpc.status_code 0." View 6d42…-t0 contains 13 PlaceOrder spans (grpc 0), 5 `prepareOrderItemsAndShippingQuoteFromCart` spans with empty `status_tags`, and 2 `HTTP POST` spans with `http.status_code 200`. Span count is wrong and a status value is asserted for 7 spans that carry no such tag (field omission conflated with an observed 0). Durations cited (125166/104978/90061 µs → 9f7d…/87c3…/d80a…) are correct.

**P2-2 (MODEL).** Claim 7: "Span-call rates for the checkout dependency services show no STATUS_CODE_ERROR series"; claim 9 (counter_evidence): "all 36 returned checkout/dependency span series are UNSET or OK." View 133c…-t1 (36 series, not truncated) contains three payment `STATUS_CODE_ERROR` series (`GET`, `dns.lookup`, `tcp.connect`), each explicitly 0. This states "missing" where the evidence shows "explicit zero" and contradicts claim 1 and the summary of the same report. The conclusion direction is unchanged, but the counter-evidence claim misdescribes its own evidence.

**P2-3 (PRODUCT tool face/projection; model contributory).** Claim 1 reports, inside policy-window-1, recommendation `/flagd.evaluation.v1.Service/EventStream` ERROR rate 0.00417 at steps 1–4 then 0. Prometheus raw counter (`traces_span_metrics_calls_total{service_name="recommendation",status_code="STATUS_CODE_ERROR"}`) is 45 through 03:40:59Z and 46 from 03:41:59Z (window start); observe-post `increase(...[300s])` for the window = 0.0. The non-zero values come from `rate(...[5m])` evaluated at the window start reading samples back to 03:36:59Z. `opspilot/tools/otel_demo.py:promql_problem` deliberately allows this (docstring: "a selector at the window start still looks back up to its own length before it"), but the tool description delivered to the model says "a query that reads outside it returns an error", and the view reports `source_start_at=03:41:59` for data partly derived from before the window. Result: a background anomaly in another service is reported at the wrong time scope. (Same lookback affects every step-1 rate value, e.g. PlaceOrder 0.02083/s = 6.25 calls in the 5 min before the window; the report's "overlapping 5-minute rates" caveat does not say they cross the window boundary.) This is a boundary event (≤60 s before window start), so it does not touch the core conclusion.

**P3 (wording, meaning unchanged):**
- Summary "sub-windows of only ~19-38 s": cart sub-window is 03:45:33→03:46:58 (≈85 s); the gaps entry lists the correct start times.
- Claim 1 lists recommendation among series that "were 0 for the first steps" then says it was the only non-zero one.
- Claim 4 "(and code 0) … all reading 0": the code-0 EventStream series read 0.00417 at steps 1–4.
- Claim 5 "shipping GetQuote 7.1–18.1 ms": GetQuote spans are 7.7–18.1 ms; 7.1 ms is the `POST` child minimum.
- Gap "parent references … largely parent_is_visible=false": true for product-catalog (20/20), but cart is 10/20 and frontend 6/20.
- Claim 8 "server-side spans that stayed in the millisecond range" — checkout PlaceOrder server spans were 41–125 ms; presumably means dependency server spans.

### Product vs model

- Product evidence binding, projection provenance, budget/deadline accounting, `incomplete`/`source_start_at` narrowing on the three short Jaeger samples, and the ledger/DB/SSE consistency all check out; no product defect there.
- P2-3 is a product tool-face/projection defect: the description promises out-of-window reads are refused while the transport permits range-selector lookback, and the view's `source_start_at` understates the source range. P2-1 and P2-2 are model report defects.
- Note for the packet: this is a normal case with no fault; the report's core answer matches the engineer-side truth (0 errors, no failed dependency calls). The FAIL is on criterion 3/5 evidence-description accuracy, not on the conclusion.

VERDICT: FAIL
P1=0 P2=3 P3=6
