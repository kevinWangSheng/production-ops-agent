# cart-fault 独立审查（全新上下文 Agent，Opus；原文照录，未经执行者改动）

Reviewer inputs: v4 packet criteria, PRODUCT-CONSTRAINTS, ADR-0005，模型可见 REPORT_CONTRACT/RUN_COVERAGE_TEMPLATE/REPORT_RETRY_TEMPLATE 与工具描述，docs/tasks/2026-09-28-m1-01-upstream-alignment-b.md（B1-B6 背景，含 pc-fault 审查发现的 FIXED_WINDOW 提示词矛盾），本目录，55431 只读 SQL。审查者未见执行者评估或其他审查者结论。留出场景（cartFailure），未用于调优本候选。第三个也是最后一个故障，需核查是否与更早两个（paymentFailure、productCatalogFailure）事故混淆。

**Cart-fault Run review: FAIL.** The report finds the right root cause and stays within permissions, but it has four P2 errors, so criterion 5 fails. None of the four changes the root-cause conclusion. I did not edit any files.

Run `7fd3bd70`, commit `e22e6c4`, evidence in this directory (`docs/evidence/m1-01-alignment-b-effect/cart-fault/`). [本机路径已由执行者脱敏为仓库相对路径，审查结论原文未改]

**How the Run went (from the stored files):**
- It made 4 model requests and 7 tool calls, then stopped by itself at round 4 with `finish_reason=stop`.
- It did not use the B3 report retry. `events.jsonl` shows only 4 `step_committed`; the one "retry" in `sse.txt` is the SSE `retry: 2000` reconnect line.
- No duplicate calls were removed and no result was too large. The final state is `completed` with `handoff=false`.

**Actual query windows (from `ledger.json` `evidence[].view.window`):**
- Six views cover 13:36:39–13:41:39Z: checkout traces, cart traces, span-metrics all statuses, span-metrics ERROR only, rpc client counts, and rpc average latency.
- One view (`26d3ac67…-t1`, the latency baseline) covers 13:31:39–13:36:39Z.

The system prompt still says "do not supply start/end". It caused no confusion here: every window stated in the report matches the ledger exactly.

## Criteria

1. **PASS.** The report parses as JSON, with `assessment_status=completed`, `conclusion=partial` and no handoff. "Partial" fits the evidence: the traces confirm the failures, but the metrics do not.
2. **PASS.** The core conclusion rests on evidence the tools actually delivered. The cart view `39b4b46a-t0` holds 3 checkout EmptyCart spans with ERROR and gRPC status 9, each with a cart server child span whose error says "Wasn't able to connect to redis … ValkeyCartStore.EnsureRedisConnected". The redis/Valkey cause is labelled a hypothesis. Facts, hypotheses, counter-evidence, gaps and next steps are kept apart.
3. **FAIL** (P2-1 and P2-2 below). Everything else here checks out:
   - All trace and span ids and the durations 12.46 s, 10.40 s and 10.05 s match the views.
   - The PlaceOrder (status 0) and `/api/checkout` (HTTP 200) spans match.
   - Truncation is reported correctly: 171 of 547 spans shown with 376 omitted, and the cart search marked `incomplete`.
   - The report says outright that the missing cart ERROR series is unknown, not zero.
   - All 7 average latencies in the current and baseline windows match.
4. **PASS.** The report certifies no health or recovery, takes no remediation action, and leaves the next steps to a human.
5. **FAIL.** Four P2 findings remain unresolved.

## Findings

- **P2-1 (class b), MODEL.** The summary says "every returned STATUS_CODE_ERROR series for checkout, payment, product-catalog, currency, shipping and email has value 0". No ERROR series came back for currency, shipping or email (views `58598ffc-t1` and `39b4b46a-t1`). Currency returned only `STATUS_CODE_OK` and the other two only `UNSET`. Absent series are presented as zero.
- **P2-2 (class b-svc: payment), MODEL.** Claim 5 lists payment "charge" among the STATUS_CODE_ERROR series with value 0. Payment `charge` returned only an `UNSET` series with value 10.0.
- **P2-3 (class e), MODEL.** Claim 4 says the successful EmptyCart calls "took roughly 20–23 ms". One of the three cited spans, `a89a6841419f407e`, took 12.147 ms (`duration_us` 12147). The "orders of magnitude slower" point still holds.
- **P2-4 (class e, counter-evidence and causality), MODEL.** The report's claim 7 and its counter-evidence use checkout's `rpc_client_duration` averages to argue against elevated latency, EmptyCart included ("EmptyCart ~12.68 vs ~11.51 ms, no elevation"; "statistically indistinguishable").
  - The report's own claim 6 shows that metric had no status-9 EmptyCart series, so the 10–12 s failing calls were not in the average.
  - A read-only Prometheus query I ran afterwards, at 13:43:39Z, shows `rpc_grpc_status_code="9", rpc_method="EmptyCart"` = 7.09. This points to metric export lag at 13:41:39.
  - Also, the injection at 13:40:33Z falls in the last ~66 s of the 300 s window, which dilutes any window-wide average.
  - The report partly hedges ("if the pipeline is trustworthy", and a gap entry), so this is borderline P2. It does not change the root cause.
- **P3-1, MODEL.** The latency baseline window partly overlaps the productCatalogFailure episode; details under the contamination check below. No wrong attribution follows from it.
- **P3-2, MODEL.** "Statistically indistinguishable" is wording only; no statistical test was run.

No PRODUCT defects were found in this Run. The prompt's `FIXED_WINDOW` sentence still contradicts the B2 tool schema, but it had no effect here.

**P2 by class:** a=0 b=1 b-svc=1 c=0 d=0 e=2 (total 4).

## Window-contamination check

Per `fault-timeline.jsonl`:
- The six current-window views (13:36:39–13:41:39Z) start after paymentFailure was restored (13:16:48Z) and after productCatalogFailure was restored (13:34:53Z). Actual span starts are 13:36:44Z–13:41:38Z.
- Every error span shown is cart EmptyCart and starts at or after 13:40:55Z, inside the cartFailure episode that began 13:40:33Z.

One cited view does reach into an earlier episode: the baseline `26d3ac67…-t1` (13:31:39–13:36:39Z) overlaps productCatalogFailure (13:29:20–13:34:53Z) by about 3 min 14 s. Prometheus at 13:36:39Z confirms it holds `GetProduct` status-13 calls (2.5 calls). So the baseline GetProduct average of ~1.20 ms includes failed product-catalog calls.

The report does not attribute these to cart and does not mix the episodes. It only uses the baseline as "the immediately preceding 5 minutes" without noting it was not a clean period (P3-1). The stale status-13 GetProduct and status-2 Charge series in `39b4b46a-t2` are left over from the earlier episodes. The report reports them correctly as value 0 and draws nothing from them.

## Upstream-style verdict

**Correct.** The report names cart EmptyCart failing because cart cannot reach its redis/Valkey storage. This matches `observe-pre.json`: 2 traces with a checkout error and a failed dependency child, `failed_dependency_services: ["cart"]`, and the same "Wasn't able to connect to redis" error on traces 6158c49d and 4fbc8cf1. The report cites those two traces plus a third, c12d0844.

VERDICT: FAIL
P1=0 P2=4 P3=2
P2-by-class: a=0 b=1 b-svc=1 c=0 d=0 e=2
Window contamination: no misattribution found. The baseline latency window (13:31:39–13:36:39Z) overlaps the productCatalogFailure episode (13:29:20–13:34:53Z) and contains GetProduct status-13 errors; it is used only as an unflagged comparison baseline (P3). No cited window touches the paymentFailure episode, and all cited error traces fall inside the cartFailure episode.
Upstream-style: PASS. The report identifies the cart EmptyCart → redis/Valkey connection failure, consistent with observe-pre.json (`failed_dependency_services=["cart"]`, 2 confirming traces).
