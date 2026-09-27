# 独立证据审查：normal-2（run f771d566-5ef2-5311-b7ce-71f1878c1e23）

审查者：全新上下文 Agent（Fable 5.1，general-purpose），只拿 v4 包判据、PRODUCT-CONSTRAINTS、ADR-0005、本目录证据、产品源码与 55431 只读查询；被明确要求不读执行者的 `handoff-cause.md`。以下为审查者结论原文（英文原样保留）。

---

**Independent evidence review — Run normal-2 (`f771d566-5ef2-5311-b7ce-71f1878c1e23`, incident `2599753a-…`)**

Sources checked: request/window/events/sse/report/ledger/observe-* in the evidence dir; `opspilot/investigation/reports.py` (`parse_report`, `unsupported_citations`), `loop.py` `_validated_report` (l.495–513), `context.py` `delivered_view`; read-only PG queries on this Run's rows only. `handoff-cause.md` was not read.

## Why the product rejected the report (verified offline)

- `parse_report(report_text, finish_reason="stop")` → parses OK (`m0-report-v2`, `assessment_status=completed`, `conclusion=partial`, 16 claims). `sha256(report_text)` = `d36b0744…f2fd` = `report_sha256` = round-4 step content (`opspilot_steps` seq 3, `finish_reason=stop`, model `deepseek-flash`).
- `unsupported_citations` over the 16 delivered views (all `target_ids={m0-otel-20260909}`, `time_scope_refs={policy-window-1}`) → **True**. Isolating per claim: only **claim index 1** (`kind=fact`) fails; it cites a single evidence id `504f8012…-t2:98d0eeeb…` whose view has `status="no_data"` (`result_count 0`, raw = Prometheus `{"status":"success","data":{"result":[]}}`), tripping `reports.py:670` `any(view.status != "ok")`. All other 15 claims bind (ids exist, belong to this Run, ok status, correct target/policy).
- Round 4 was the 4th of 4 HTTP (`budget_limit=4, budget_spent=4`, `final=True`), so `_settle` returned `("failed", ("REPORT_INVALID",))`; no retry was possible.
- Is the rejection correct per v4? Yes: the packet's deterministic counterexamples include "三类事实…失败引用", and the code's rule is that fact-like claims rest only on `ok` views. A `no_data` view is not observed evidence of anything; the model should have expressed that absence as a gap/hypothesis (which it also did, gap #3), not as a `fact`.
- Product-side caveat: `REPORT_CONTRACT` (reports.py l.56–76) tells the model only to cite "at least one complete evidence_id actually supplied in this Run"; it never says non-`ok` views cannot back a fact, and the tool result the model saw for that view carried `"adopted": true` plus an evidence_id (round-1 `tool_results[2]`). Prompt and checker disagree — see P3-1.

## Criteria

**1 — Parsable final report; completion vs incomplete; no budget/connection failure passed off as completion.** Report is parsable; handoff is accurately recorded (`run_handoff` seq 23: `handoff=true, execution=failed, published=false, reasons=[REPORT_INVALID]`, `report_sha256` matches). Budget honest: 4/4 requests spent, `budget_unknown=0`, 16/20 tools, `tool_seconds 0.54`, model 44.4 s, finished 20:57:43 < deadline 21:26:56. **Accurate handoff → allowed, but per the packet it does NOT count as a positive normal-case pass.** As a normal-capability Run: **FAIL (no qualifying report)**.

**2 — Core conclusion supported by delivered evidence; fact/hypothesis/counter/unknown distinguished.** **PASS.** The substantive conclusion ("elevated errors not corroborated in-window; latency trend unknown without baseline") matches the engineer-side record: observe-pre/post `precondition.control_window_ok=true, fault_confirmed=false`, `all_services_error_calls` all 0.0, Jaeger 11 checkout traces with 0 error spans. All 11 trace ids in claim 0 exist in view `…-t0:e3d3f443` and in the observe Jaeger set; the 7 in claim 6 exist in `…-t0:3a640e8b`. Kinds are used correctly (fact / hypothesis / rejected_hypothesis / counter_evidence / recommendation; gaps list unknowns). Not an engineered answer: the investigator received only question/window.

**3 — Citations complete; source/object/window consistent; counters/missing/limits not conflated.** **FAIL** on one item (P2-1), otherwise clean:
- Numbers verified against views: p50 `58.33,58.33,56.25,56.25,62.5×6,60` and p95 `92.5,92.5,73.125,73.125,93.75,93.75,92.5,92.5,197.5,197.5,175` exact (`…-t3:10d2f114`, `…-t3:0f6689d7`); client means Charge 8.93–13.50, Convert 2.13–3.06, GetQuote 6.37–8.95 etc. exact (`…-t2:27e9c10d`); span duration ranges 74–4888, 1088–8090, 313–2359, 2976–12134, 2530–21463 us exact; payment charge 1046–9404 us ("1.0–9.4 ms") exact; cart HTTP 0.196→0.329 exact; PlaceOrder max 102262 us exact.
- All spans in every trace view lie inside the window; `error_by_visible_tags=False`, `omitted_error_detail_field_count=0` everywhere.
- `rate()` series are described as rates ("not error rates"); `no_data` is read as unknown, not zero (claim 1 text, gap #3); `incomplete:true` (Jaeger returned ≥limit traces, otel_demo.py l.1012) is reported as "partial view"; result_count=returned_count=20 with 0 omitted, so no backend/view conflation.

**4 — No uncertified health/recovery, no repair, no release gate.** **PASS.** Only `traces_search`/`metrics_range_query` were dispatched (23 events); no control/mutation. Summary explicitly refuses to certify ("not corroborated inside this 300s window; … remains unknown"), conclusion `partial`.

**5 — No unhandled P1/P2.** **FAIL** (one P2 below).

## ADR-0005 / state consistency (PASS)

PG now: run `state=waiting_human, owner=NULL, lease_until=NULL`; incident `lifecycle=open, conclusion IS NULL, current_run_id` = this Run; 16/16 evidence rows with raw sha256/length/status identical to ledger; 23 events, last `run_handoff`; 5 steps. `incident.html` shows `waiting_human`, `REPORT_INVALID`, and POST controls follow_up/correct/cancel/new_run/pause/resume. Not published, incident open, human controls available — consistent with ADR-0005 §1. `reasoning_content` withheld (C3 §12). No other Run's rows touched.

## Findings

**P1 — none.**

**P2-1 (MODEL report defect; the rejection trigger).** Claim index 1 is a `fact` whose sole citation is the `no_data` view `504f8012…-t2:98d0eeeb…`. A view with no rows is not observed evidence; the same content belonged in `gaps`/hypothesis (where the model also put it). Root cause of the no_data: the model queried `status_code="ERROR"` while the label value is `STATUS_CODE_ERROR` (its later `=~".*ERROR.*"` query returned 9 series); the report never identifies this label mismatch.

**P3-1 (PRODUCT: prompt/checker mismatch — loop/prompt).** `REPORT_CONTRACT` does not state that fact-like claims may cite only `status=ok` views, and the model-visible tool result for the `no_data` view says `adopted: true` with an evidence_id. The checker rule is correct per the packet, but the contract as communicated to the model does not carry it; this cost a normal-case Run its only report attempt (final request, no retry).

**P3-2 (MODEL).** Claim 2 reports recommendation `/flagd…/EventStream` ERROR "at ~0.00417/s" as the only nonzero ERROR series; the delivered series is 0.00417 for 8 samples then 0 for the last 3, and the engineer-side `increase(...[300s])` over the exact window is 0.0. With the model's own `[5m]` lookback on a 300 s window, the nonzero samples reflect pre-window activity. Service scope (outside the dependency set) is correct; the temporal caveat is missing. Meaning unchanged.

**P3-3 (MODEL).** Claim 3 phrases rates as "rises from ~0.267/s to ~0.575/s" (and similarly for payment/currency/shipping/email): the low value is the series minimum (sample 5–6), not the first sample (checkout starts at 0.317). Values exist in the series; direction and substance unchanged.

**P3-4 (MODEL).** Claim 2 calls payment `dns.lookup`/`tcp.connect` "children" of span `GET`; the metric view carries no parent/child relation (packet criterion 3: 同 trace/parent 关联不得混淆). Values (all 0) unaffected.

**P3-5 (MODEL).** Recommendation says to re-run trace searches "without the incomplete-flag constraint"; `incomplete` is a result flag (`len(traces) >= limit`), not a query constraint. Summary's "sub-15ms except GetQuote ~12ms" is self-contradictory wording. No meaning change.

## Bottom line

The product behaved correctly: it parsed the text, found one fact resting on a `no_data` view, refused to publish, parked the Run as `waiting_human` with the incident open and controls available, and recorded budget truthfully. The model's substantive conclusion is right and numerically faithful, but the report as delivered is not a qualifying normal-case report, and this Run cannot be counted toward the "2 normal Runs" requirement. Actionable product item: align `REPORT_CONTRACT` wording (or the model-visible view stub) with the `ok`-only citation rule so a correct absence-of-data finding is not forced into a `fact`.

VERDICT: FAIL
P1=0 P2=1 P3=5
