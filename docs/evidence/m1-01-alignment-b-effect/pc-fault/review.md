# pc-fault 独立审查（全新上下文 Agent，Opus；原文照录，未经执行者改动）

Reviewer inputs: v4 packet criteria, PRODUCT-CONSTRAINTS, ADR-0005，模型可见 REPORT_CONTRACT/RUN_COVERAGE_TEMPLATE/REPORT_RETRY_TEMPLATE 与工具描述，docs/tasks/2026-09-28-m1-01-upstream-alignment-b.md（B1-B6 背景），本目录，55431 只读 SQL。审查者未见执行者评估或其他审查者结论。留出场景（productCatalogFailure，checkout 上间歇性故障），未用于调优本候选。审查者被特别要求核查是否与更早的 paymentFailure 事故混淆。

**Verdict: FAIL, but only just.** One borderline P2 decides it: a product-catalog error description that was only seen in a truncated trace sample is stated as true of every error the metric counted. If you read that "all" as meaning only the spans the model could see, it drops to P3 and the Run passes. Nothing else in the report is a P1 or P2. The root cause is right, and I found no mixing with the earlier `paymentFailure` episode.

I checked all 18 evidence views in `ledger.json`, their actual `view.window`, the raw tool results (`backend_traces_returned`, `incomplete_reason`), `observe-pre.json` and `fault-timeline.jsonl`. I also queried Prometheus read-only once, to check the 12:27Z no_data.

## Criteria 1-5
1. **PASS.** The report parses. `execution=completed`, `handoff=false`, `conclusion=partial`. The Run stopped by itself at round 7 after 18 tools, with no retry and no forced final. "Completed but uncertain" is kept separate from "not finished".
2. **PASS.** The core conclusion rests on delivered evidence: checkout PlaceOrder errors come from product-catalog GetProduct gRPC 13 "Product Catalog Fail Feature Flag Enabled". It is labelled a hypothesis ("correlation, not proven causation"). Facts, counter-evidence, rejected hypotheses, gaps and next steps are kept apart.
3. **FAIL** (finding P2-1). Every other number matches its view, including:
   - PlaceOrder ERROR 1.25, product-catalog ERROR 14.33925 against 125 UNSET.
   - Frontend 13.01 / 9.00 / 4.34 / 1.25, frontend-proxy 13.75, load-generator 12.5 / 1.25.
   - Payment ERROR explicit zeros; cart/currency/shipping/email ERROR series absent and correctly called unknown, not zero.
   - All p95 values (client 4.75 / 23.95 / 22.375 / 5.5 / 23.6875; server 190 / 73.5 / 223.75; product-catalog 4.83 / 4.75 / 4.80).
   - Trace counts: checkout 9 traces returned out of 40 requested, 201 of 374 spans omitted, `incomplete_reason` null. Product-catalog 40 of 40, 292 of 436 omitted, "more may exist".
   - The failing PlaceOrder durations (3.5 and 18.8 ms) and the successful range (31.4–121.9 ms).
   - The 12:27–12:32Z no_data is real: Prometheus `count(up)` at 12:32:27Z returns empty, and the report treats it as unknown.
4. **PASS.** No health or recovery is certified ("whether still active… unknown"), no remediation, no release gate. Rejecting payment as the cause rests on explicit zero series.
5. **FAIL**, because P2-1 is unresolved.

## Findings
- **P2-1 (e), MODEL, borderline.** In the fact claim about product-catalog's own spans, the model writes "~14.34 ERROR calls versus ~125 UNSET… **all with** otel.status_description 'Error: Product Catalog Fail Feature Flag Enabled' and rpc.grpc.status_code 13".
  - The metric view has no description field.
  - The description is visible on only 13 error spans, inside a truncated and incomplete trace view (`backend_traces_returned=40` = limit, "more may exist", 292 of 436 spans omitted).
  - So the claim carries details seen in a sample over to the whole metric count, which the "visible scope" rule in the packet forbids. The substance is almost certainly true in reality; the problem is evidential support.
- **P3-1, MODEL.** A counter_evidence claim calls the 2-minute series "internally inconsistent": 14.34 for the 2 minutes ending 13:31:27 against 4 for the 2 minutes ending 13:32:27. Those windows overlap and the values fit together: most errors fall in 13:29:27–13:30:27, right after the 13:29:20Z injection. The reasoning is wrong but the effect is only extra caution; no number is wrong.
- **P3-2, MODEL.** The payment episode is dated "around 13:05-13:18" (also "13:05-13:12Z" in the gaps). The earliest evidence the model gathered starts at 13:07:27; the true injection was 13:03:39.
- **P3-3, MODEL.** The baseline claim says checkout's dependency series in 13:22:27–13:27:27 were "all STATUS_CODE_UNSET or absent". The checkout Charge ERROR series was present there, with an explicit value of 0.
- **P3-4, MODEL.** Typo "PlacementOrder".
- **Product observation (not scored, 未确认 whether it reached this Run's prompt bytes).** At e22e6c4, the prompt variant `replay-candidate` (`opspilot/investigation/loop.py:77`) still includes `FIXED_WINDOW` (`opspilot/instructions/discipline.py:82,127,172`): "do not supply start/end tool parameters". That contradicts B2, and the model supplied start/end anyway. This is a PRODUCT inconsistency for batch B to look at.

**P2 by class:** a=0 b=0 b-svc=0 c=0 d=0 e=1 (total 1).

## Window-contamination check
No contamination found. `paymentFailure` ran 13:03:39–13:16:48Z; `productCatalogFailure` was injected 13:29:20Z.
- Only two cited views reach into the payment episode: `614c39fc-…-t1` (window 13:07:27–13:12:27) and the 2-minute series `50312108-…-t2` (13:12:27–13:32:27, points from 13:14:27). The report explicitly calls both "an earlier, separate episode".
- The error signature there is payment Charge, with no product-catalog ERROR series. Payment ERROR drops to 0 from 13:18:27, which fits the 13:16:48 restore.
- Every current-fault fact uses views whose 5-minute lookback starts at 13:27:27, over 10 minutes after the restore.
- All 13 cited trace IDs start between 13:30:12 and 13:32:20Z, after the injection, and all exist in the views. No payment error is attributed to product-catalog, or the other way round.
- The report does not overstate the fault: it gives 14.34 ERROR against 125 UNSET and 2 of 9 checkout traces, and never says product-catalog is "entirely down".

## Upstream-style verdict
Root cause is correct. `observe-pre.json` has `failed_dependency_services=['product-catalog']`, the same two failing traces (bba16e134f9d6d3b336b9ccedb287ba7, f502de3917ad131fc7387031dcec491e) and product-catalog error increase 14.33925. The report names the same dependency, traces and value.

VERDICT: FAIL
P1=0 P2=1 P3=4
P2-by-class: a=0 b=0 b-svc=0 c=0 d=0 e=1
Window-contamination: no contamination found. Payment-episode windows are explicitly separated and attributed to payment; all current-fault evidence is from 13:27:27Z onward, and every cited trace is after the 13:29:20Z injection.
Upstream-style: correct. Product-catalog GetProduct failure matches `observe-pre.json` (`failed_dependency_services=['product-catalog']`, same two trace IDs, 14.33925 errors).
