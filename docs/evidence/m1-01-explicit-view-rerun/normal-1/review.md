# normal-1 独立审查（全新上下文 Agent，Fable 5.1；原文照录，未经执行者改动）

Reviewer inputs: v4 packet criteria, PRODUCT-CONSTRAINTS, ADR-0005, the model-visible REPORT_CONTRACT / RUN_COVERAGE_TEMPLATE and tool descriptions, this directory, read-only SQL on 55431. The reviewer did not see the executor's assessment.

Run `1b724675-0781-5c13-997b-7d0d4b4a50ef` / incident `a78a963c-f1e8-54ed-a722-8a5553549b53` (case normal-1, candidate f8fac31)

## Record integrity
- Report hash: sha256(report.json `report_text`) = `98816b9e…dfc6b`, equals report.json `report_sha256`, ledger `run_completed.report_sha256`, DB `opspilot_steps` round-4 `assistant.content` (17458 chars) and conclusion step `report_content_sha256`. `parse` = m0-report-v2, `assessment_status=completed`, `conclusion=partial`, `handoff=false`, `handoff_reasons=[]`, `finish_reason=stop`.
- Evidence: 18 rows in DB = 18 in ledger = 18 ids in `run_completed`; all `status=ok`, `adopted=true`, `committed=true`, `citable_as_fact=true`; sha256 of DB `raw` bytea recomputed == ledger `raw_sha256` for all 18; `view_sha256` recomputed (canonical JSON) matches for all 18. `projection_revision=m1-01-tool-view-v5`.
- Run/budget: `state=completed`, `budget_limit=4`, `budget_spent=4`, `reserved=0`, `unknown=0` (4 reservations all `spent`, 360 s each); `tool_operations_used=18` ≤ 20, 18 `tool_charges` rows (epoch 1, `tool_seconds_used`=0.2625 s); deadline `08:35:19.125620-07:00` = submit 08:05:19 + 1800 s; run finished 08:06:34 (75 s). Incident row: `conclusion` set, `lifecycle=open`, `current_run_id`=this run; incident `state` stays `queued` (persistence.py:1854-1858 only updates `conclusion` and run state, so this is by design, not a violation). No `opspilot_inputs` rows, `control_generation=0` throughout.
- Events: DB `opspilot_subject_events` 25 rows (seq 1..25) = ledger 25 = events.jsonl 25 = sse.txt ids 1..25; kinds/payloads identical (intake_accepted, run_claimed, 4× step_committed, 18× tool_committed, run_completed with `published=true`). worker-attempt.txt `status=published`.
- Window: all 18 views carry `window {start 15:00:19Z, end 15:05:19Z}` == window.json; `target_id=m0-otel-20260909`.
- Metrics views (11): every expr uses `[300s]`; `lookback_seconds=300`, `lookback_start_at=15:00:19Z` (window start), exactly one point at 1790521519 = 15:05:19Z (window end) in every series, `source_start_at=source_end_at=15:05:19Z`, `truncated=false`, `omitted_rows=0`. Contract satisfied.
- Trace views (7): `traces_requested==query.limit` (20), `spans_shown==len(rows)==20`, `spans_shown+spans_omitted==backend_spans_returned` (372/378/352/338/372/372/372/302), `incomplete_reason` non-null iff `incomplete` (3 views: product-catalog t4, cart t5, frontend 6948-t7, all "backend returned as many traces as requested; more may exist", `backend_traces_returned=20`), `status_state` matches `status_tags` on all 160 rows (0 mismatches), `lookback_*` null.
- Final-round coverage message: not stored verbatim; round-4 `context.final=true` with `input_snapshot_hash`; rebuilt via `run_coverage_message` from the 18 delivered views in delivery order: "18 view(s)… incomplete true: 8a58…-t4, 8a58…-t5, 6948…-t7. truncated: none. not ok: none." Report gap 4 names exactly product-catalog, cart, frontend as incomplete — consistent.

## Criteria
1. PASS — parseable v2 report, `completed`/`partial`, no handoff or budget/connection failure masquerading as completion (4/4 model requests used, but the 4th produced the report with `finish_reason=stop`).
2. PASS — core conclusion ("no failing checkout or dependency RPC observed in window; 'elevated errors' premise unsupported; checkout ERROR series absent = unknown") is backed by delivered views 8a58-t0, 6948-t0/t1, 8a58-t2/t3 and matches observe-post; fact / hypothesis / counter_evidence / rejected_hypothesis / recommendation are separated; no engineer answer imported.
3. FAIL — citations are complete and every cited trace/span id exists in the cited view, counters are consistently written as 300 s increases, absent series are mostly written as unknown; but P2-1..P2-4 below (not_recorded spans written as status 200/0, wrong span count, wrong min–max range) are source/visibility/numeric mismatches against the delivered views.
4. PASS — no health/recovery certification (states what was not observed, gaps for absent ERROR series and no baseline), recommendations marked "Advisory only (not executed)", no release gate.
5. FAIL — 4 P2 findings unprocessed (see below); no P1.

## Findings
P1: none.

P2:
- P2-1 (MODEL, class a) summary: "the sampled checkout, cart, product-catalog, currency, shipping and email spans show rpc.grpc.status_code 0, http.status_code 200/"200", or otel.status_code OK" — cart view 8a58-t5 has 18/20 rows `status_tags={}` / `not_recorded`, email 6948-t6 13/20, checkout 8a58-t2 4/20; those spans show no status at all (OTel Unset), not 0/200/OK.
- P2-2 (MODEL, class a) claim 11: "All sampled email spans returned http.status_code 200" — only the 7 `POST /send_order_confirmation` rows carry `http.status_code 200`; the 13 `send_email`/`sinatra.render_template` rows are `not_recorded`.
- P2-3 (MODEL, class e: span count) claim 8 says "four error-tagged spans" and then lists five (454a2a69e53bb8db, dcd535999b0bad7e, ffba21702d8d2292, c6734e36592f72c5, ef40c4e034e9a105); view 8a58-t3 has 5 rows with `error_by_visible_tags=true`. Repeated as "four" in claim 16, gap 3, recommendation 2 and next_step 2 ("0-vs-4").
- P2-4 (MODEL, class e: min–max range) summary "PlaceOrder 40–67 ms" — the non-outlier PlaceOrder rows in 8a58-t2 are 40,491–71,484 µs (dc796… bc9b99b5beb2927b = 71,484); claim 6 itself states 40,491–71,484 correctly, the summary understates the max.

P3 (wording, meaning unchanged):
- P3-1 (MODEL) claim 3 / claim 16 / summary: "no non-zero gRPC status code is present/appeared for any checkout dependency call" — literally describes view 6948-t1 (only `rpc_grpc_status_code="0"` series returned) and does not say "zero errors", but should say "no non-zero-code series was returned (unknown, not zero)" per the tool contract. Not counted in class (b).
- P3-2 (MODEL) claim 7: "2e3004e0c5b67a10 is the visible parent of 9fc59e80d41e5542" — the parent ref is `parent_is_visible=false` inside the shipping view; the parent is visible only in the separately cited checkout view.
- P3-3 (MODEL) claim 12: PlaceOrder 5ac568b34083a9ab called "child" of POST /api/checkout 732d265e8108befc; it is a grandchild via ccadb67dcfd2dd04.
- P3-4 (MODEL) claim 18: "error-tagged payment spans … with empty parent_references" — only the two GET root spans have empty refs; the three children reference those GETs.
- P3-5 (MODEL) claim 16 "checkout had no ERROR series at all in either query" cites ee70-t0 whose regex excludes checkout; true for 8a58-t0/t1 and 6948-t0.
- No PRODUCT findings: evidence binding, projection fields, executor budgets and coverage message all consistent.

## P2 by class
- (a) empty status / not_recorded written as status 0/OK/200: **2** (P2-1 summary; P2-2 claim 11).
- (b) absent series written as 0 / "no non-zero code": **0** (claim 3/16 wording graded P3-1; checkout ERROR absence is correctly written as unknown in summary, gap 2, next_step 3). (b-svc) absent by-service series written as zero/no errors for that service: **0**.
- (c) trace limit confused with span row counts: **0** ("20 of N spans shown" used correctly in claim 6, 14, gap 4).
- (d) wrong count of incomplete/truncated views: **0** (3 incomplete views named correctly; none truncated).
- (e) other: **2** — span count 4 vs 5 (P2-3); summary min–max range 40–67 ms vs 40,491–71,484 µs (P2-4).

## Upstream-style verdict
Did the report correctly determine that there was NO fault in this window? **Yes** — it concludes no failing checkout or dependency RPC was observed (all checkout series `STATUS_CODE_UNSET`, ERROR-filtered series 0, all sampled Charge/GetQuote/Convert/GetCart spans status 0, payment errors isolated to metadata lookups), which matches observe-post.json (`fault_confirmed=false`, `all_services_error_calls` all 0, 7 traces with no checkout error or failed dependency child spans), while correctly refusing to certify zero checkout errors from an absent series.

VERDICT: FAIL
P1=0 P2=4 P3=5
