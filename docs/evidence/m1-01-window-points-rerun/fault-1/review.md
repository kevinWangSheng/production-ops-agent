# fault-1 独立审查（全新上下文 Agent，Fable 5.1；原文照录，未经执行者改动）

Reviewer inputs: v4 packet criteria, PRODUCT-CONSTRAINTS, ADR-0005, the model-visible REPORT_CONTRACT and tool descriptions, this directory, fault-timeline.jsonl, read-only SQL on 55431. The reviewer did not see the executor's assessment.

Run d826e986-fd5b-5cf7-a8f1-32a736861ddc / incident 2892bfb0-8ccf-5a7c-b049-da81402a238f (case fault-1, candidate b352230, FAULT window 2026-09-27T12:29:20Z–12:34:20Z; fault injected 12:28:42Z, restored 12:41:14Z, so the whole window is under fault).

## Record integrity
- Report hash: sha256(report.json.report_text) = 74495c12…6b043c = report_sha256 = conclusion_other_fields.report_content_sha256 = sha256 of opspilot_steps seq 3 (`dfbce256…`) `/assistant/content` (16636 bytes). report_text parses to report_parsed exactly. incident.html contains the same hash.
- Evidence: 18 rows in opspilot_evidence for the run = 18 in ledger.json = 18 in run_completed.evidence_ids = 18 tool_charges. Raw bytes re-hashed from the DB `raw` bytea: all 18 raw_sha256 match the ledger and byte counts match. view_sha256 recomputed with `opspilot.tools.registry.canonical_hash` over the jsonb `view`: 0 mismatches. projection_revision m1-01-tool-view-v5 on all rows; 17 status ok / adopted / citable_as_fact true; 1 no_data (`24b58d22-…-t3`, traces_search email, content [], citable_as_fact false, data_as_of null).
- Run/budget: opspilot_runs state completed, epoch 1, owner/lease NULL, budget_limit 4 / spent 4 / reserved 0 / unknown 0 (4 model HTTP, 4 usage rows, 4 reservations all `spent`, model seconds 4.94+13.71+7.07+43.43 = 69.15 = model_seconds_used), tool_operations_used 18 ≤ 20, tool_seconds 0.1645 = Σ tool_charges. Deadline 06:04:20 PDT = created 05:34:20 + 1800 s; final step 05:35:32 (72 s wall). Token totals reconcile per request (prompt 104692, completion 16256, reasoning 9595, cache-hit 29440). Model: deepseek-flash.
- Incident row: state queued, lifecycle open, control_generation 0, conclusion set. `persistence.publish` only writes `conclusion` and flips the run to completed; consistent with ADR-0005 (qualified report + no handoff → publish). worker-attempt: status=published reason=None; conclusion step handoff=False, execution completed, rounds 4.
- Events: opspilot_subject_events 25 rows = events.jsonl 25 lines = sse.txt 25 `event:` frames, same kinds/order (intake_accepted, run_claimed, 4×step_committed, 18×tool_committed with matching step_id/ordinal/status incl. the single no_data, run_completed published=true).
- Windows: every one of the 18 views has window == window.json (12:29:20Z–12:34:20Z) == evidence_context.time_policies[0] `policy-window-1`; target m0-otel-20260909 delivered in scope_facts.target_ids and bound_target_id. All 10 metric views: lookback_seconds 300, lookback_start_at 12:29:20Z, exactly one point at 1790512460 = 12:34:20Z (window end), source_start_at = source_end_at = 12:34:20Z — matches the window-points contract for L = window length. Trace views: lookback fields null, source_start/end inside the window.

## Criteria
1. PASS — parseable m0-report-v2, assessment_status completed, conclusion partial (unquantified error rate, no baseline, email unknown); no budget/connection failure masquerading as completion; not a handoff.
2. PASS — core conclusion (checkout PlaceOrder failing because payment Charge rejects with "Invalid token", surfaced as HTTP 500) is supported by delivered views t4/t5/t2/t3; fact / hypothesis / counter_evidence / rejected_hypothesis / recommendation used as distinct kinds; the question named payment only as one of six dependencies, not as the answer.
3. PASS — every fact cites full evidence_ids that exist and are ok/citable; the no_data email view appears only in gaps (no claim cites `…-t3`). Counters described as `increase(...[300s])` over the window, never as cumulative or per-step; zeros stated only where a series with value 0 was actually returned (t0/t1/t4/t5/t6/e211-t0); absent series called unknown; sample limits stated (20 sampled, payment 18 of 20 returned, product-catalog/cart/frontend "incomplete"); status_tags {} described as "empty status tags/untagged", not code 0; parent/child chain for 2ae907f7 verified span-by-span across t2/t4/t5; all numbers checked (see below).
4. PASS — no health/recovery certification (email "unknown, not healthy"; sampled dependencies only "no error tags" with sampling gaps listed), no remediation executed, no release-gate claims.
5. PASS — no P1/P2 found (details below).

Numeric/time verification against views: claim 1 six PlaceOrder ids+durations (37570/24798/19526/17483/12307/6726 µs) = t4 rows 0,1,2,4,5,7; claim 2 payment Charge 561–10098 µs, 18 of 20 rows = t5 (truncated true, omitted_rows 2); claim 5 186–893 µs = t1 min/max, 15 ≈ 14.99975 (t4); claim 6 6085 µs, HTTP 200 children, 3.75 (t4); claim 7 7384 µs, 60×200 (e211-t1); claim 8 105–2562 µs, 11.25 / 92.5 / 6.25 (t4, t5), incomplete true; claim 9 p95 4.75/9.25/4.75/9.625 + NaN×3 (e211-t2), product-catalog 4.75, checkout NaN (t3); claim 10 CAD/USD 0 (t6); claims 11–12 all zeros present in t0/t1/e211-t0/t4/t5; claim 15 ResolveFloat 5615 µs, code 0 (t5 row 7); gap 4 source_start/end times equal the view fields for checkout/payment/cart/product-catalog/frontend. No value is presented as an in-window interval endpoint or fault start time; report states no fault onset.

## Findings
P1: none.
P2: none.
P3:
- [MODEL] Claim 10: "no successful transactions recorded" — the qualifier "successful" is an interpretation of app_payment_transactions_total not present in view t6; the stated fact (increase over [300s] = 0 for CAD and USD) is correct.
- [MODEL] Claim 3 / summary: "visible parent references" for the checkout-Charge → payment-Charge edge; in t5 that parent (79cf243de5edbb64) has parent_is_visible=false and the join is made across views via t4 (correct span id, cited). Meaning unchanged.
- [MODEL] Claims 5–8 round 14.99975→15, 3.7499→3.75, 92.498→92.5, 6.2499→6.25 without saying so. Meaning unchanged.
Optional (not graded): the report omits the one nonzero error counter in e211-t0 (frontend-proxy STATUS_CODE_ERROR 7.5), which corroborates rather than contradicts its conclusion; claim 4 additionally cites t2, which has no 620959b9 frontend spans (surplus citation, the cited e211-t3 carries the fact). [PRODUCT, optional] trace-view `incomplete: true` (backend returned ≥ limit traces) is not explained in the model-visible description; the model handled it conservatively.

## Cross-check vs observe-post.json
Engineer truth: 6 checkout traces in window, all 6 with checkout error + failed dependency child, failed_dependency_services = ["payment"]; trace ids 4b03188d…, 2ae907f7…, 9f6e0865…, 97a61f6b…, 3bbf07cc…, 620959b9…. The report cites exactly these 6 ids and localizes the failure to oteldemo.PaymentService/Charge (payment). Engineer metrics also show checkout/payment STATUS_CODE_ERROR = 0 and frontend-proxy = 7.5, so the report's counter_evidence reflects real spanmetrics lag, not a model error.

## Upstream-style verdict
Did the report correctly identify the root cause / failing call for this window: yes — it names the payment PaymentService/Charge call ("Payment request failed. Invalid token") as the failing dependency under checkout PlaceOrder, cites the same 6 trace ids observe-post.json lists, and rejects cart/currency/shipping/product-catalog on evidence.

VERDICT: PASS
P1=0 P2=0 P3=3
