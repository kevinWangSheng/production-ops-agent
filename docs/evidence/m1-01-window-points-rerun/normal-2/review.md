# normal-2 独立审查（全新上下文 Agent，Fable 5.1；原文照录，未经执行者改动）

Reviewer inputs: v4 packet criteria, PRODUCT-CONSTRAINTS, ADR-0005, the model-visible REPORT_CONTRACT and tool descriptions, this directory, read-only SQL on 55431. The reviewer did not see the executor's assessment.

run_id bfc51712-3d94-5c4e-8b7d-63e947e5626c / incident_id dc2bce70-6ecb-5355-b0c5-3cd3931ab52b (case normal-2, candidate b352230, independent evidence review)

## Record integrity
- Report hash: `opspilot_steps` ctx0:round-4 (`response_committed`, finish_reason `stop`) assistant content sha256 = f9abf953…be9e3, 13895 bytes; equals report.json `report_sha256` and recomputes from `report_text`. Conclusion step `conclusion:g0:ctx0:round-4` response == incident `conclusion` (kind/assistant/conclusion); worker-attempt `status=published`; `run_completed` payload `published: true, handoff: false`.
- Evidence: 19 rows in `opspilot_evidence` = 19 in ledger.json = 19 `evidence_ids` in report.json. sha256 recomputed over DB `raw` bytea matches ledger `raw_sha256` 19/19; all `adopted=true, committed=true`. 18 views `status=ok, citable_as_fact=true`; d618f714…-t6 (http_server_request_duration_seconds_count) `status=no_data, citable_as_fact=false`, cited only in gap 4, never by a fact — correct handling.
- Run/budget: run `state=completed`, epoch 1, owner/lease NULL; budget_limit 4 / spent 4 / reserved 0 / unknown 0 (4 reservations `spent`); `tool_operations_used=19` (19 `opspilot_tool_charges` rows) ≤ 20; deadline 05:57:21.01 = created 05:27:21.01 + 1800 s; run finished 05:28:10 (≈49 s wall). Model seconds 47.6 s. Usage: 4 requests, 98077 prompt / 11836 completion tokens.
- Events: `opspilot_subject_events` 26 rows = events.jsonl 26 = sse.txt 26 (ids 1–26), kinds and payloads identical (0 mismatches): intake_accepted, run_claimed, 4×step_committed, 19×tool_committed, run_completed.
- Window: every view `window` = {12:22:21Z, 12:27:21Z} = window.json = time_policies[0] policy-window-1 (300 s). All 11 metric views: `lookback_seconds=300`, `lookback_start_at=12:22:21Z` (window start), single point at 1790512041 = 12:27:21Z (window end), `source_start_at=source_end_at=12:27:21Z` — the whole-window aggregate contract holds; no pre-window read.
- State observation: `opspilot_incidents.state` is still `queued` (lifecycle `open`) with a stored conclusion and a completed run. `persistence.publish()` only updates `conclusion` and the run row; the incident state column is never advanced. Not a recovery/permission fault in this run (gating is on `conclusion`, ADR-0005), recorded below as P3 PRODUCT.

## Criteria
1. PASS — parseable m0-report-v2, `assessment_status=completed`, `conclusion=partial`, `handoff=false`; budget not exhausted (4/4 requests used exactly at the final report, 19/20 tools), no connection failure disguised as completion.
2. PASS — core conclusion ("no observed failures on checkout or its dependency calls in this window; elevated latency undeterminable without baseline") rests on delivered ok views (t0 STATUS_CODE_ERROR=0 window-wide, dbc-t4 ten explicit 0 error series, 0a-t2/t7 all-code-0 series, six PlaceOrder traces at status 0). Fact / hypothesis / counter_evidence / recommendation / gaps are separated; no engineer answer leaks in.
3. FAIL — absent series stated as explicit zero (summary: checkout error counters "reported as 0"; claim 4 "no non-zero gRPC status"); empty `status_tags` described as `rpc.grpc.status_code 0` (claims 7, 12); incomplete-view count understated (gap 5 says two, three views are `incomplete=true`). Details in Findings.
4. PASS — no health/recovery certification, no repair, no release-gate claim; latency explicitly left unjudged; only read-only tools used.
5. FAIL — four P2 findings unprocessed.

## Findings
### P1
- none.

### P2
1. MODEL — summary sentence 2: "span-error counters for **checkout**, frontend and frontend-proxy (GET, /api/data, AdService/GetAds, tcp.connect, ingress, router egress) … were reported as 0". No checkout STATUS_CODE_ERROR series exists in any view: 0a267b15…-t1 returns only `{checkout, STATUS_CODE_UNSET} = 82.5`; dbc913cc…-t4 has no checkout series; all six listed span names belong to frontend/frontend-proxy. Absent series presented as explicit zero, contradicting claim 2 and gap 2 of the same report (the later caveat does not undo the assertion).
2. MODEL — claim 4 (0a267b15…-t2): "all recorded with rpc_grpc_status_code=0 … i.e. no non-zero gRPC status on these calls". The unfiltered query returned only code-0 series; per the tool contract a series not returned is unknown, not zero. Values themselves (6.25/16.25/6.25/6.25/10.0/6.25/6.25 vs 6.2499…/16.2499…/9.99996…) are correct to 2 dp.
3. MODEL — claims 7 and 12: payment child `charge` spans (921, 1018, 1105, 1227, 1309, 2988 us) in 0a267b15…-t5 and cart `HGET` spans (3979, 3464 us) in d618f714…-t0 have `status_tags: {}`; the report says they carry `rpc.grpc.status_code 0`. The lower endpoint 921 us of claim 7's "921–4323 … with rpc.grpc.status_code 0" is such a tagless span. Same over-statement in summary and counter_evidence ("every/all sampled … spans carried non-error status tags"); only `error_by_visible_tags=false` is supported. Ranges/values otherwise verified: 4323 max, cart GetCart 11738/9019/6489/6381/4529/4430/3978/3416, EmptyCart 7424/6208, cart coverage 12:24:07–12:26:48Z, `incomplete=true`.
4. MODEL — gap 5: "two views reported incomplete=true (cart, product-catalog)". Three views are `incomplete=true`: d618f714…-t0 cart, d618f714…-t4 product-catalog, and dbc913cc…-t3 frontend (backend returned 20 traces; source interval 12:26:38–12:27:17Z, i.e. only the last ~39 s). Claim 13's frontend statement omits this limit.

### P3
1. MODEL — claim 11: product-catalog spans "mostly 111–1064 us"; view d618f714…-t4 also contains a 5956 us GetProduct span (trace 33439d28…). "mostly" hedges; range stated as if bounds.
2. MODEL — claim 1 calls the increase()-derived window totals a "span-status counter"; "window-wide … over 300s" preserves the increase meaning. Wording only.
3. PRODUCT — `opspilot_incidents.state` stays `queued` after a successful publish (lifecycle `open`, conclusion present, run `completed`); `publish()` never writes the incident state column, so exported/ledger records show a queued incident with a published conclusion. Whether the workbench page renders this label was not confirmed (未确认). No effect on this run's control gating (conclusion-based).

Verified correct (not findings): claims 1, 2, 3, 5, 6, 8, 9, 10, 13, 14 numbers, trace ids and status tags match the cited views exactly (PlaceOrder 46098/44551/43869/42220/38681/29578 us on aaeb6c60…, 16ef87a8…, 931eeed6…, 3a5dc37a…, 43258cc7…, a63ddc9b…; Charge 9917/9271, Convert 11209; p95 client 9.6875/15.25/9.6875/4.75/4.75/9.375/4.75; server p95 48.75/4.75; server counts 6.25/112.5/8.75; payment transactions 6.25; currency 184–2603 us with OK/0; shipping GetQuote 2570–4989, ShipOrder 11–157; email 200 max 7420; frontend aaeb6c60… 52501/51939/51539/48877). Every cited trace id exists in the cited view. No fact cites the no_data view or an operation_id.

## Cross-check vs observe-post.json (engineer ground truth, same window 12:22:21–12:27:21Z)
- `expect: normal`, `precondition.fault_confirmed=false`, `control_window_ok=true`, dependency traffic present.
- checkout span calls: 11 series, all STATUS_CODE_UNSET (PlaceOrder 6.25, Convert 16.25, GetProduct 10.0, others 6.25) — matches report claims 2/4 values; no checkout ERROR series exists, confirming P2-1 is a report error, not a data disagreement.
- payment: Charge 6.25 UNSET, ERROR series (GET, dns.lookup, tcp.connect) all 0.0 — matches claim 3.
- all_services_error_calls: frontend/payment/load-generator/frontend-proxy all 0.0 — matches dbc-t4.
- Jaeger: 6 checkout traces in window, ids identical to the 6 cited by the report, `checkout_error_spans=[]`, `failed_dependency_child_spans=[]`, `failed_dependency_services=[]`.
- Core conclusion (no observed failure; latency not judged) is consistent with ground truth.

## Upstream-style verdict
Did the report correctly determine that there was NO fault in this window: **yes** — it reports no observed failures on checkout or its six dependencies and declines to call latency elevated, and observe-post.json shows every checkout/payment series UNSET/OK, all ERROR series at 0, and all six in-window checkout traces free of error spans and failed dependency children (`fault_confirmed=false`).

VERDICT: FAIL
P1=0 P2=4 P3=3
