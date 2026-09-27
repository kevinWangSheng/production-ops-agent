# fault-1 独立审查（全新上下文 Agent，Fable 5.1；原文照录，未经执行者改动）

Reviewer inputs: v4 packet criteria, PRODUCT-CONSTRAINTS, ADR-0005, the model-visible REPORT_CONTRACT and tool descriptions, this directory, `fault-timeline.jsonl`, read-only SQL on 55431. The reviewer did not see the executor's assessment.

## Independent evidence review — fault-1 (run `03557486-0054-51f4-b4dc-508870d158c1`)

### Record consistency (DB vs exports)
Checked read-only against `host=127.0.0.1 port=55431 dbname=m0_budget`:
- `opspilot_runs`: state `completed`, epoch 1, owner/lease NULL, `budget_limit=4, budget_spent=4, budget_reserved=0, budget_unknown=0`, `tool_operations_used=10`, deadline 08:00:14Z; identical to `ledger.json`/`window.json`.
- `opspilot_steps`: 5 rows (round-1..3 `tool_result_committed`, round-4 `response_committed`, conclusion). Round-4 assistant content is byte-identical to `report_text`; sha256 recomputed = `6d70fa5a…` = `report_sha256` = `opspilot_incidents.conclusion.report_content_sha256`. `response_model=deepseek-flash` on all 4 requests; `finish_reason=stop` on the final one.
- `opspilot_evidence`: 10 rows, all `status=ok, adopted=true, citable_as_fact=true`; DB `sha256(raw)` equals each exported `raw_sha256_recomputed`. The 10 ids equal the Run's delivered `evidence_ids` and equal the set the report cites.
- `opspilot_subject_events`: 17 rows, last `run_completed{published:true, handoff:false, execution:completed}`; matches `events.jsonl` (17 lines), `sse.txt`, `worker-attempt.txt` (`status=published`). Handoff=false with publish is consistent with ADR-0005.
- Usage: 4 requests, max prompt 33,769 / completion 8,245 tokens; model wall 57.06 s; tools 0.13 s. All within the frozen limits.
- Only oddity: `opspilot_incidents.state='queued'` while a conclusion is stored (same for normal-1/normal-2). `persistence.py:1854-1858` `publish()` writes `conclusion` and run `completed` but never touches incident `state`; no code path sets incident `state='completed'`. Page shows `incident-state: queued` + badge `concluded` + `run-state: completed` (incident.html lines 34-38). Consistent DB↔export, not a recovery problem — listed as P3 product below.

### Case preconditions
Fault injected 07:24:44.69Z (`fault-timeline.jsonl`), window 07:25:14–07:30:14Z; entire window is post-injection. `observe-pre/post.json` both `fault_confirmed: true`, 8 checkout traces with a checkout error span and a failed `payment` child. Run input contains no flag/fault/inject wording (grep over `runs[0].input`); evidence_context delivers target `m0-otel-20260909` and time policy `policy-window-1` (window matches).

### Criteria
1. **Parseable final report; completed-vs-incomplete; no budget/connection failure passed off as completion — PASS.** `m0-report-v2`, `assessment_status=completed`, `conclusion=partial`, 15 claims, 5 gaps, 4 next_steps. All 4 requests and 10/20 tools succeeded; the 4th request was the forced final-report request and the model answered it. `partial` + explicit gaps correctly distinguishes "completed but unresolved" (no baseline; currency/shipping/email unqueried).
2. **Core conclusion supported by delivered evidence; kinds distinguished; no engineered answer — PASS.** Core conclusion (payment `Charge` rejects with "Payment request failed. Invalid token." gRPC 2 → checkout `PlaceOrder` gRPC 13 → frontend/proxy/load-generator HTTP 500) is fully supported by views `9633…-t2` (checkout traces), `9633…-t3` (payment traces), `093e…-t2` (a3ba4eae… chain), `9633…-t1`/`f3ad…-t1` (Charge status 2 series). All 8 cited trace ids (8b732f9c, a3ba4eae, d53617e3, bb683b6a, 30f2a348, bd4e0681, 23e56663, 063a0433) are exactly observe-post's 8 failed checkout traces, each with the cited payment child span (e34cf65f, 46d208d7, ac01f9bf, b946c321, 6df2be38, 4cc82476, 583371cb, 2332a56c) whose parent is the cited checkout Charge span. Failed dependency located = payment/Charge, matching `failed_dependency_services: ["payment"]`. The feature-flag mention is a `hypothesis` (claim 11) grounded in the delivered flagd `ResolveFloat` span `2275787c…` under the Charge span, not a guessed hidden flag.
3. **Complete citations; source/object/window consistency; counter/series/count semantics not conflated — FAIL.** Citations, target_refs, time_scope_ref are complete and belong to this Run; every fact-like claim cites only ok/citable views; no operation_id cited. Cumulative vs increase: all queries use `increase()`/`rate()`, missing series are stated as unknown (gap 2), trace views' `truncated/omitted_rows/incomplete` are disclosed (gap 4). But the numeric/time/scope errors below (P2 #1–#5) are observable mismatches against the cited views.
4. **No health/recovery certification, no repair, no release gate — PASS.** Recommendation (claim 14) says "advisory only and was not executed"; no tool other than the two read-only queries; counter_evidence is bounded by "sampled view"; nothing asserts recovery.
5. **No unresolved P1/P2 — FAIL** (five P2 below).

### Findings

**P1 — none.** Permissions, human control, budget, state recovery and the core conclusion are intact.

**P2 (MODEL report defects unless noted)**
1. *Wrong value ranges.* Summary: ERROR series "9.28–16.75 per step"; view `9633…-t0` values are 9.283, 12.283, 8.141, 9.641, 7.428, 8.428, 16.748 → range 7.43–16.75. Claim 1 and summary: Charge `rpc_grpc_status_code=2` "4.06–8.37"; view `9633…-t1`/`f3ad…-t1` values 4.63, 6.13, 4.065, 4.815, 3.71, 4.21, 8.368 → range 3.71–8.37. Both ranges were taken as third-value-to-last, not min–max.
2. *Step miscount.* Claim 0, claim 1 and summary say the ERROR / status-2 series are absent "in the earliest two steps" and present "from the third step onward". With `step_seconds=30` the points are 07:25:14, 07:25:44, 07:26:14, 07:26:44, 07:27:14…; both series (7 of 11 points, first ts 1790494034) are absent for the first four points and present from the fifth.
3. *Onset misdated from an increase[300s] artifact, contradicted by cited traces.* next_steps 4 calls "~07:27:14Z (third metric step)" the "checkout ERROR burst onset" and the summary repeats "no ERROR values in the first two steps". The view is `increase(…[300s])` with `lookback_start_at=07:20:14` (tool description: first points reflect pre-window samples), and the report's own cited checkout view shows errored PlaceOrder/Charge spans at 07:25:39 (8b732f9c), 07:26:04 (30f2a348), 07:26:28 (23e56663), 07:26:36 (d53617e3) — all before 07:27:14. Injection was 07:24:44, pre-window.
4. *Rolling 300 s increase labelled "per step".* Claim 0/1 and summary present each value as an amount "per step" (30 s); each value is the increase over the trailing 300 s at that step (the pairwise-identical values reflect the 60 s scrape). Read literally this overstates the error rate ~10×.
5. *Visible-scope overreach in counter_evidence.* Claim 13: "cart spans in the window are all non-errored". Cited view `093e…-t3` is `incomplete: true`, `limit 10`, 20 sampled spans with `source_start_at=07:29:45` — it covers ~26 s of the 300 s window. Claim 6 and gap 4 phrase it correctly ("in the cart trace view… returned spans"); claim 13 does not.

**P3**
6. Claim 9 (MODEL): frontend-proxy ERROR series is nonzero from 07:26:14 (1.25), two points before checkout/payment/frontend/load-generator; the recommendation `STATUS_CODE_ERROR` series (1.25 at the first four points, i.e. inside the lookback region, 0 thereafter) is not mentioned. No in-window claim is falsified.
7. Claim 2 and summary (MODEL) quote the error text as "…Invalid token." while the delivered value ends "… Invalid token. app.loyalty.level=gold" (claims 10/11 quote it fully).
8. PRODUCT (persistence/presentation): after `publish()` the incident stays `state='queued'` with a stored conclusion (all three incidents in the DB); page shows "queued" beside "concluded" and run "completed". Durable rows are consistent and recoverable; the label is misleading to an operator. Not a report defect; owner should decide whether publish should advance incident state.

**Product-defect check on P2s:** none attributable to evidence binding, projection, executor or tool description — every value the model misreported is present and correctly labelled (`lookback_seconds/lookback_start_at`, `incomplete`, `source_start_at`, step timestamps) in the delivered views.

**Trace-id reality:** all cited trace/span ids exist in the delivered views and in `observe-post.json`; no fabricated ids.

VERDICT: FAIL
P1=0 P2=5 P3=3
