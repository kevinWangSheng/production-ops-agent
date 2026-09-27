# fault-2 独立审查（全新上下文 Agent，Fable 5.1；原文照录，未经执行者改动）

Reviewer inputs: v4 packet criteria, PRODUCT-CONSTRAINTS, ADR-0005, the model-visible REPORT_CONTRACT and tool descriptions, this directory, `fault-timeline.jsonl`, read-only SQL on 55431. The reviewer did not see the executor's assessment.

## Independent evidence review — fault-2 (run `0ed139d2-c6c0-57dc-b5cb-47120c3d0c2b`, incident `8ae16b22-…`)

**Basis read:** packet §有界候选与案例前提 / §确定性与报告判据, PRODUCT-CONSTRAINTS, ADR-0005, `REPORT_CONTRACT` + `unsupported_citations` (reports.py:648-696), `TOOL_SCHEMAS` + trace projection (otel_demo.py:1054 `incomplete = len(traces) >= limit`), all 16 evidence views (saved from ledger.json), report.json, observe-pre/post, events/sse/html, fault-timeline, and read-only PG queries.

### Integrity / consistency of the record (checked)
- report_text sha256 recomputed = `3c5a4837…6414` = report.json `report_sha256` = incident `conclusion.report_content_sha256` = run_completed event = sse line 93. `json.loads(report_text) == report_parsed`.
- PG: run `completed`, epoch 1, owner/lease null, `budget_limit=4 spent=4 unknown=0`, `tool_operations_used=16`, deadline = submit+1800 s. 4 budget reservations `spent` (2.74+4.66+6.98+27.54 = 41.92 s = `model_seconds_used`). 16 `opspilot_tool_charges`, 16 evidence rows all `adopted=true committed=true` (15 ok, 1 no_data); for every row raw sha256(DB bytea) = stored `raw_sha256` = ledger, byte lengths match, `view_sha256` matches. 5 steps, 23 subject events in PG = 23 in events.jsonl = 23 SSE events (1 intake, 1 claim, 4 step, 16 tool, 1 run_completed{published:true, handoff:false}). `response_model` = `deepseek-flash` on all 4 requests. **Consistent; model_requests 4 ≤ 4, tools 16 ≤ 20.** Other runs' 41 evidence rows are separate run_ids; nothing cross-cited.
- Observation (not a finding): `opspilot_incidents.state` stays `queued` after publish — `persistence.publish` (line ~1855) only writes `conclusion` and `runs.state='completed'`; `recovery.py:27` keys on `conclusion is None`. By design.

### Criterion-by-criterion
**1. Parseable final report; completed vs incomplete; no budget failure disguised — PASS.** Round-4 `finish_reason=stop`, valid `m0-report-v2`, `assessment_status=completed`, `execution=completed`, `handoff=false`, `handoff_reasons=[]`. Round 4 is the designed final-report request, not a budget exhaustion. Six gaps listed.

**2. Core conclusion supported by delivered evidence; kinds distinguished — PASS.** Failed dependency located as payment `Charge` ("Invalid token", `charge.js:37`) from views cb-t0/cb-t1/6609-t1, without the investigator ever seeing the flag. All 10 trace ids in claim[0] are exactly the 10 checkout PlaceOrder spans in cb-t0 (all `error:true`, grpc 13, start 07:35:51.98Z–07:40:12.16Z, inside window) and are exactly observe-post's 10 `traces_with_checkout_error_and_failed_dependency_child` with `failed_dependency_services=["payment"]`. Cited span ids `2e021cd321e39e1d`, `e04416b76200d771`, `fee23072dbae8713`, `ccdecc1b5b6cf47c`, `3ad140c86700ee89`, `501e33a9b1d91e9c`, `fa677d60377fab84` all present in cited views and in observe-post. Impact stated from evidence: PlaceOrder status-0 = 0 while 13 = 10.0 (6609-t4), frontend 500 (6609-t1), downstream EmptyCart/ShipOrder/orders publish = 0 (gap 5, 6609-t3). No engineering answer leaked. 13 facts / 1 hypothesis / 1 counter_evidence / 1 rejected_hypothesis / 1 recommendation, correctly labelled. All 12 numeric claims (claims 2,3,4,6,11,12) match cited view values digit-for-digit.

**3. Complete citations; source/object/window consistency; counter/missing-series/count semantics — FAIL** (three P2 below). What passes: every `evidence_id` exists in this Run, complete (dispatch suffix, never `operation_id`); no duplicates; fact-like claims cite only `status=ok, citable_as_fact=true` views; the `no_data` view 780-t5 is cited by no claim and used only in gaps[0]; `target_refs=m0-otel-20260909` and `time_scope_ref=policy-window-1` match the input snapshot; `increase()/rate()` wrapping is correct and labelled "(per 300s)"; missing ERROR series for cart/product-catalog/shipping treated as unknown (gap 4); truncation reported correctly (checkout 16/20, 4 omitted; payment 12/20, 8 omitted) and `incomplete` views named (product-catalog, cart, frontend, frontend-proxy — matches flags); "all … sampled" scoping used for trace statements; parent visibility limits acknowledged (gap 3).

**4. No unevidenced health/recovery certification, no repair, no release gate — PASS.** Email left unknown; dependencies described only as "sampled spans… no error tags"; recommendation says "this report authorizes no change". Only the two read-only tools were used.

**5. No unresolved P1/P2 — FAIL** (P2 ×3 below).

### Findings

**P2-1 — pre-window lookback point reported as in-window (MODEL).** Every cited metrics view has `lookback_seconds=300, lookback_start_at=07:30:47Z, source_start_at=07:35:47Z`; with `step_seconds=300` the first point (ts 1790494547 = 07:35:47Z) is an `increase()/rate()` over 07:30:47–07:35:47, entirely before the window (the tool description says so explicitly). Claims 2, 3, 4, 6, 11, 12 say "in the window … 7.4998 then 10.0" etc., the summary says "~7.5-10 per 300s", and gaps[1] calls them "the two in-window steps" while asserting no pre-window baseline exists — the first point *is* a pre-window value. Not P1: the second point (07:40:47, exactly the window) independently supports every conclusion (PlaceOrder 13 = 10.0, 0 = 0; Charge ERROR 8.75; p95 ≤ 22 ms).

**P2-2 — cart "recorded gRPC status 0" not in the cited view (MODEL).** rejected_hypothesis (claim index 15, cites 780-t3) and the summary ("gRPC status 0 in all sampled spans (product-catalog, cart, currency, shipping)") assert cart status codes were 0; all 20 sampled cart spans in 780-t3 have `status_tags: {}` — no status code recorded. Claim 7 itself is correctly worded ("carry no error tags"). Conclusion unaffected.

**P2-3 — explicit-zero series called "unknown — not zero" (MODEL).** gaps[0]: email "call volume and latency are unknown — not zero". Delivered view cb-t2 (ok, cited by claim 4) contains `{service_name="email", status_code="STATUS_CODE_UNSET"} -> [0, 0]`: a returned explicit zero, the exact confusion criterion 3 names. Conservative direction; it also drops an impact fact (email received no calls because checkout aborts at payment).

**P3-1** — recommendation/next_steps call `app.loyalty.level=gold` a "feature-flag context referenced in the error text"; the text is an attribute value, "feature-flag" is inference (flagd appears only as a payment span name with 0 calls in 6609-t3). Advisory, not asserted as fact, not a cause claim. (MODEL)
**P3-2** — counter_evidence mentions frontend-proxy spans but cites 6609-t1 (frontend) not 6609-t2; claim 10 cites 6609-t2 correctly. (MODEL)
**P3-3** — claim 2 calls increase() counts "rates"; claim 8's "3786–16001 us" is the GetQuote-only range (POST child spans go down to 3556 us). Wording. (MODEL)

No PRODUCT defect found: evidence binding, projection flags (`lookback_*`, `truncated/omitted_rows`, `incomplete`, `citable_as_fact=false` on no_data), executor limits and DB/export consistency all behaved as specified; all three P2s are the model misreading fields the views exposed.

**VERDICT: FAIL**
**P1=0 P2=3 P3=3**
