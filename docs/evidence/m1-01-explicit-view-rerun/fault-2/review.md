# fault-2 独立审查（全新上下文 Agent，Fable 5.1；原文照录，未经执行者改动）

Reviewer inputs: v4 packet criteria, PRODUCT-CONSTRAINTS, ADR-0005, the model-visible REPORT_CONTRACT / RUN_COVERAGE_TEMPLATE and tool descriptions, this directory, fault-timeline.jsonl, read-only SQL on 55431. The reviewer did not see the executor's assessment.

run_id 5bd4dd44-a7c7-52a5-862d-1fd82cacd87b / incident_id 61c4803b-d0f2-5f2c-a26d-4d68674a816c (case fault-2, candidate f8fac31, model deepseek-flash, prompt-replay-candidate-177e529f603d)

## Record integrity
- Report hash: report.json `report_sha256` 97f97ec6…29bb == sha256(report_text) == sha256 of stored `opspilot_steps` round-4 assistant content (logical_key ctx0:round-4, finish_reason stop) == run_completed payload report_sha256. Conclusion step conclusion:g0:ctx0:round-4 present; incident.conclusion non-null.
- Evidence: 16 rows in `opspilot_evidence` for the run == 16 ledger rows == 16 ids in run_completed payload == report.json evidence_ids. For all 16: sha256(raw bytes in DB) == DB raw_sha256 == ledger raw_sha256 == ledger raw_sha256_recomputed; raw byte length == ledger raw_bytes; view_sha256 recomputed OK; DB view == ledger view; status/adopted match. 15 views status ok/citable_as_fact true; 1 view (4f2c7823…-t2:c40239ae…, traces_search email) status no_data, adopted true, citable_as_fact false, content [], backend_traces_returned 0.
- Run/budget: run state completed, owner NULL, lease_until NULL, epoch 1; budget_limit 4, spent 4, reserved 0, unknown 0 (4 model HTTP, usage rows 4, total prompt 106,644 / completion 12,604); tool_operations_used 16 == 16 tool_charges (≤20); wall: run_claimed 08:22:25.11 → run_completed 08:23:18.24 PDT (53 s, deadline 08:52:25 = +1800 s); window 15:17:25Z–15:22:25Z = 300 s. worker-attempt status=published, handoff false, handoff_reasons [] — consistent with ADR-0005 (publish only on qualified report, no handoff).
- Events: sse.txt 23 events == events.jsonl 23 == `opspilot_subject_events` 23 (same sequence/kind order): intake_accepted, run_claimed, 4×step_committed (rounds 1–3 tool_calls with 6/5/5 planned tools; round 4 stop), 16×tool_committed, run_completed.
- Window: every view's `window` == window.json (15:17:25Z–15:22:25Z). Metrics views (7): lookback_seconds 300, lookback_start_at == window start, source_start_at == source_end_at == window end, exactly one point at 1790522545 = 15:22:25Z (matches the L = window-length contract). Trace views: source_start_at ≥ 15:18:18.998Z, data_as_of ≤ 15:22:22Z, all in-window.
- Trace-view fields: for all 8 trace views spans_shown == row count (16/12/20/20/20/20/0/20), spans_shown + spans_omitted == backend_spans_returned (330/330/263/368/330/330/0/251), incomplete_reason null iff incomplete false (incomplete true only on product-catalog, cart, frontend: backend_traces_returned 20 == traces_requested 20), status_state consistent with status_tags on every row (not_recorded rows: all 20 cart rows, 2 payment `charge` rows).
- Fault timeline: inject 15:11:58Z (before window), restore 15:23:53Z (after window end, after run completion 15:23:18Z, after observe-post 15:23:48Z). observe-pre/post differ only in observed_at.
- Coverage message: the verbatim final-round user message is not persisted (only `input_snapshot_hash` and `request_sha256` in the step row), so I reconstructed it from the delivered views via the fixed template (`loop.py:125`): total 16; incomplete = {t2 product-catalog, t3 cart, 3e…-t0 frontend}; truncated = {t0 checkout, t1 payment}; non-ok = {4f…-t2 email (no_data)}. Verbatim-message equality: unverified (not stored).

## Criteria
1. PASS — parseable m0-report-v2, assessment_status completed, conclusion supported, no handoff, budget not exhausted; not a handoff masquerading as completion.
2. PASS — core conclusion (checkout PlaceOrder fails because checkout→payment Charge is rejected with "Invalid token"; localized to payment) is supported by cited ok views t0/t1/t5/[9]/[4]; fact/hypothesis/counter_evidence/rejected_hypothesis/recommendation distinguished; cause of the invalid token explicitly left as hypothesis; nothing beyond delivered evidence.
3. FAIL (one numeric P2, below) — otherwise: all 15 claims cite existing evidence_ids, correct target_ref m0-otel-20260909 and time_scope_ref policy-window-1; no fact-like claim cites the no_data view (it appears only in gaps by id); counters described as 300 s increases, not per-step; explicit zeros (claim 9) vs absent series (claim 5, gap 3) correctly separated; traces_requested 20 vs backend spans 330 vs shown 16/12 vs omitted 314/318 correctly distinguished; incomplete/truncated sets in gap 2 match the reconstructed coverage; all 10 cited trace ids exist in the cited views; not_recorded cart spans described only as "no error details", not as status 0/OK; parent_is_visible=false for PlaceOrder correctly noted.
4. PASS — no health/recovery certification, no repair, no release-gate claim; latency explicitly not judged elevated without baseline.
5. FAIL — one unprocessed P2 (numeric).

## Findings
P1: none.

P2:
- P2-1 [MODEL] Claim 6 and claim 13 mix parent/child span duration ranges in the shipping view (4f2c7823…-t1). Claim 6: "GetQuote spans … durations 3,918–43,257 µs" — GetQuote rows are 4,154–43,257 µs; 3,918 is the child POST span ce34a1604abec009. Claim 13: "GetQuote child POSTs returned 200 in 3,918–43,257 µs" — POST rows are 3,918–42,361 µs; 43,257 is the parent GetQuote 16dc91f6ec67f2d1. Observable numeric error; conclusion (latency small) unchanged.

P3:
- P3-1 [MODEL] Claims 4, 12, 14: "checkout span metrics show no ERROR status for GetCart/GetProduct/Convert/GetQuote/ShipOrder/EmptyCart" describes the absence of an ERROR series in view [9] as counter-evidence. Not written as zero, and claim 5 + gap 3 state explicitly that an absent ERROR series is unknown, so meaning is preserved; wording should say "no ERROR series was returned".
- P3-2 [MODEL] Claim 9 header "downstream steps that never error in this window" — the cited series are STATUS_CODE_UNSET = 0; nothing about errors is shown. Body correctly says "reported values, not proof of missing calls".
- P3-3 [MODEL] Claim 12 "their child HTTP calls returned 200" generalizes to cart/product-catalog/currency; only shipping has child POST rows (http.status_code 200) in the views.
- P3-4 [MODEL] Summary "the metric layer records … frontend HTTP 500 on POST /api/checkout": HTTP 500 comes from trace view 3e…-t0; metric view [14] shows `POST /api/checkout` UNSET=10 and `executing api route (pages) /api/checkout` ERROR=10, no HTTP status.

Product observations (not graded under v4; no report error):
- [PRODUCT] After publish, `opspilot_incidents.state` stays `queued` (lifecycle open, conclusion set); `persistence.py` publish() only updates runs.state='completed' and incidents.conclusion. Consistent with the code, but an incident with a published conclusion labelled `queued` is a state-readability gap; whether intended is unverified.
- [PRODUCT] The model-visible final-round input (incl. the coverage message) is stored only as a hash, so coverage statements can be checked only against the deterministic reconstruction.

## P2 by class
- (a) empty status / not_recorded written as status 0/OK: 0 (cart not_recorded rows described as "no error details"; payment `charge` not_recorded rows not described).
- (b) absent series written as 0 / "no non-zero code": 0. (b-svc) absent series in by-service grouped query written as zero/no errors for that service: 0 (claim 5 and gap 3 state unknown; the "show no ERROR status" wording in claims 4/12/14 is recorded as P3-1).
- (c) trace limit confused with span row counts: 0 (claims 1, 2, gap 2 state 20 requested / 330 backend / 16 or 12 shown / 314 or 318 omitted correctly).
- (d) wrong count of incomplete/truncated views: 0 (gap 2: truncated = checkout, payment; incomplete = product-catalog, cart, frontend; matches reconstructed summary).
- (e) other — parent/child span min–max range mix (claims 6, 13, shipping view): 1.

## Upstream-style verdict
Did the report correctly identify the root cause / failing call: yes — it localizes to the checkout→payment Charge call failing with "Payment request failed. Invalid token" and lists exactly the 10 trace ids observe-post.json marks as checkout error with failed dependency child = payment (failed_dependency_services ["payment"], 10/10 traces), matching payment ERROR=10 and checkout Charge rpc_grpc_status_code=2 = 10.0 in the engineer-side metrics.

VERDICT: FAIL
P1=0 P2=1 P3=4
