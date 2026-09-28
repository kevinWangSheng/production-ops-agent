# normal-2 独立审查（全新上下文 Agent，Opus；原文照录，未经执行者改动）

Reviewer inputs: v4 packet criteria, PRODUCT-CONSTRAINTS, ADR-0005，模型可见 REPORT_CONTRACT/RUN_COVERAGE_TEMPLATE/REPORT_RETRY_TEMPLATE 与工具描述，docs/tasks/2026-09-28-m1-01-upstream-alignment-b.md（B1-B6 背景），本目录，55431 只读 SQL。审查者未见执行者评估或其他审查者结论。

**VERDICT: FAIL.** Only because of one P2 in the model's report. The core no-fault verdict is correct, and I found no contamination from traffic outside the Run's window. The product side (evidence binding, projection, executor) showed no defect in this Run.

**Run facts:** Run `eab487ef…` finished `completed` with no handoff. It used 6 model requests and 17 read-only tool calls. The B3 report retry did not fire (one conclusion step, `conclusion:g0:ctx0:round-6`), and no B5 duplicate calls appear. The report returned `assessment_status=completed`, `conclusion=partial`: no failures seen, latency left unresolved.

**Actual query windows (from `ledger.json` `result.window`):**
- 13 views: 12:55:52–13:00:52Z.
- `f95df696…-t1`: 12:30:52–13:00:52Z (step 300s). It returned only the 13:00:52 point.
- `2cc73deb…-t0`: 12:45:52–12:50:52Z, `no_data`.
- `2cc73deb…-t1`: 12:35:52–12:40:52Z, `no_data`.
- The Prometheus backend I queried read-only has no series before about 12:56Z today; the lab appears freshly started. Checkout trace views come from 12:56:00–12:58:30Z, and the product-catalog view from 12:59:28–13:00:50Z.

## Criteria

1. **PASS.** The final report parses. `partial` correctly separates the answered failure question from the unanswered latency question.
2. **PASS.** The core conclusion rests on delivered views:
   - `f0b2b653…-t0`: ERROR series payment, frontend, frontend-proxy and load-generator are all 0.
   - `38d898d1…-t1`: only UNSET/OK series for checkout and its dependencies.
   - `f0b2b653…-t1`: all checkout gRPC client calls have `rpc_grpc_status_code` 0.
   - `38d898d1…-t0`: all 7 PlaceOrder spans have status 0; 0 spans have `error_by_visible_tags`; 0 spans have `error_details`.
   - Fact, hypothesis, counter-evidence, rejected hypothesis and recommendation are kept apart.
3. **FAIL.** See the P2 below. Everything else I checked matches the views exactly:
   - The 7 trace ids match the 7 in `observe-pre`.
   - Numbers match: PlaceOrder 463358 us; email durations 140706 / 39909 / 22595 / 9216 / 6322 / 5941 / 4003 us; Charge 27.7 ms (server) and 45.3 ms (client); p95 411.11 / 4.81 / 104.48 ms; client p95s; counts 7.11 / 17.50 / 10.00; CAD 1.25 and USD 4.71; product-catalog 15 traces, 131 spans, 0.046–4.319 ms.
   - Truncation is reported correctly: 360 backend spans, 182 shown, 178 omitted, covering 12:56:00–12:58:30.
   - `no_data` is reported as unknown, not zero, and missing ERROR series are called "absent", not 0.
4. **PASS.** Nothing is certified healthy and no fix is executed. All next_steps are "Advisory (human)", and all tools were read-only queries.
5. **FAIL.** One P2 remains unhandled.

## Findings

- **P2, class (a), model report defect.** The `rejected_hypothesis` says "the sampled dependency spans (payment, shipping, cart, currency, product-catalog, email) all carry rpc.grpc.status_code 0 or http.status_code 200."
  - In the delivered trace views, many of those spans are `status_state: not_recorded` with no status tag:
    - `38d898d1…-t0`: 10 cart, 3 email, 1 payment.
    - `f0b2b653…-t2`: 15 cart, 7 email, 9 payment.
    - `f0b2b653…-t3`: 14 cart, 21 email, 2 payment.
  - This includes the cart server span `POST /oteldemo.CartService/EmptyCart` in every trace.
  - The claim also cites only `f0b2b653…-t1` (a metrics view) and `83fc33e0…-t2`, which contains only product-catalog, frontend, frontend-proxy, load-generator and recommendation spans. So the span-level statement about the other five services cites no view that holds those spans.
  - The rejection itself still stands on the gRPC client counts in `f0b2b653…-t1`, so the core conclusion is not broken.
- **P3, model.** The `counter_evidence` says the ERROR series is "absent for checkout and its dependencies", but payment (a dependency) does return `STATUS_CODE_ERROR`=0. The report states this correctly elsewhere.
- **P3, model.** The cart/email fact starts with the garbled phrase "Currency against the span-status view:". It also calls the span `POST /oteldemo.CartService/EmptyCart` "EMPTYCART"; the value 7.12 is correct.

**Not defects:**
- Gap 2 ("history confined to latest instant") matches the backend: Prometheus has no series before about 12:56Z. The 30-minute query returning only the 13:00:52 point is real, not a projection bug.
- The warm-up hypothesis is labelled as a hypothesis and fits the lab start time.
- `time_scope_ref=policy-window-1` is the 24h frame, the only policy offered under B2. Claim texts state the actual 5-minute window.

**Totals:** P1=0 P2=1 P3=2

**P2 by class:** a=1 b=0 b-svc=0 c=0 d=0 e=0

**Window contamination: no contamination found.**
- Every cited or queried window ends at or before 13:00:52Z. The earliest fault today, `paymentFailure`, was injected at 13:03:39Z, restored at 13:16:48Z, and is the fault behind fault-1/fault-2 (submitted 13:11:26Z / 13:12:56Z). `productCatalogFailure` and `cartFailure` came later still.
- The earlier probed windows (12:30:52–13:00:52, 12:45:52–12:50:52, 12:35:52–12:40:52Z) also predate all faults. Before about 12:56Z they held no data at all.
- All cited trace ids fall within 12:56:00–13:00:50Z.

**Upstream-style verdict: correctly judged no fault.** `observe-pre.json` has `precondition.fault_confirmed=false` and `jaeger.traces_with_checkout_error_and_failed_dependency_child=0` over the same 7 traces the report cites. The report found no failures and rejected the dependency-failure hypothesis.
