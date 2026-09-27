# 独立证据审查：fault-2（run 86fe860c-699f-5694-a752-65d9e671412a）

审查者：全新上下文 Agent（Fable 5.1，general-purpose），拿 v4 包判据、PRODUCT-CONSTRAINTS、ADR-0005、本目录证据（含独立观察）、产品源码与 55431 只读查询，并被告知工程侧真相（`paymentFailure` 100%，04:03:12Z–04:21:52Z）；被明确要求不读执行者的 `handoff-cause.md`。以下为审查者结论原文（英文原样保留）。

---

## Review of fault-2 Run `86fe860c-699f-5694-a752-65d9e671412a`

### Why the product rejected the report (verified offline)

`parse_report(report_text, finish_reason="stop")` succeeds (valid `m0-report-v2`, no DSML, sha256 `8e7a6e25…` matches the round-4 row and `report.json`). The rejection comes from `unsupported_citations` (`reports.py:632`), first check `eid not in by_id`: **all 14 claims** cite ids of the form `<step_id>-tN` (e.g. `12225891-…-t0`), while the 14 delivered views are keyed `<step_id>-tN:<uuid>`. Round 4 was `final` (4/4 requests), so `_settle` (`loop.py:470–492`) returned `("failed", ("REPORT_INVALID",))`.

**Was the rejection correct per contract?** Yes. `REPORT_CONTRACT` (`reports.py:66–70`): "cite at least one complete evidence_id actually supplied in this Run … never abbreviate or invent IDs"; `EVIDENCE_DISCIPLINE` (`discipline.py:61`): "complete evidence_id values, never shortened aliases".

**Model vs product?** Every tool message the model received (`loop.py:720–726`, `canonical(visible_view(view))`) contains a field literally named `"evidence_id"` with the full form, plus a separate `"operation_id"` = `<step>-tN`. The model copied `operation_id`. That is a **MODEL defect**. Product contribution is limited to a face foot-gun: `operation_id` is a strict prefix of `evidence_id`, both appear in every result (`_VIEW_PROVENANCE_KEYS`, `context.py:83`), and no prompt text says `operation_id` is not citable (P3, product).

**Latent product contract gap (confirmed by counterfactual replay):** after rewriting every short id to its full form, `unsupported_citations` still returns True because claim 7 (`fact`: "email trace search returned no_data … unknown, not zero") cites view `62f5d83c-…-t3` whose status is `no_data`, and fact-like claims must cite `ok` views (`reports.py:670`). Neither `REPORT_CONTRACT` nor `discipline.py` tells the model that a `no_data`/non-ok view cannot anchor a fact. The model did exactly what criterion 3 asks (absence ≠ zero) and would still be handed off. **PRODUCT prompt/contract defect, P2** (an observable wrong rejection of a correct claim; it did not decide this Run's outcome, but it would decide a Run with correct ids).

### Substance vs independent observation

- Trace ids: the 8 ids in claim 0 are exactly the 8 `distinct_trace_ids` in both observe-pre and observe-post (set equality; no report-only or observation-only ids).
- Located failure: checkout `PlaceOrder` server spans ERROR (grpc 13), child client `PaymentService/Charge` ERROR (grpc 2, `parent_is_visible=true`), payment `grpc.oteldemo.PaymentService/Charge` ERROR with `charge.js:37` stacktrace in 8/8 spans. Observation: `increase(…[300s])` PlaceOrder ERROR 7.5, Charge ERROR 7.5, both UNSET 0; all other checkout client peers UNSET-only; `all_services_error_calls` non-zero only in load-generator/frontend/frontend-proxy/checkout/payment. The report's claims 4–5 cover frontend/frontend-proxy at their real scope. No hidden-flag guess: gap 7 says the token origin is unknown and no flag evidence was queried.
- Numbers: claims 2/3 rates (0.0167…0.0417; UNSET 0; Charge `rpc_grpc_status_code=0` series present and explicitly 0), claim 4 frontend-proxy 0.0208–0.0417, claim 8 p95/p50 values, claim 9 single cart/200 series, cart source window 04:17:34–04:20:01Z, product-catalog max 4204 µs — all match the views. `PlaceOrder` parents `parent_is_visible=false` correctly reported as unknown chain. Truncation/incomplete flags (t0/t1/t2 truncated, cart and product-catalog `incomplete=true`, 17 omitted rows) correctly disclosed.

### Criteria

1. **FAIL (as fault-positive capability); handoff itself accurate.** Final text parses, but no accepted report; handoff `REPORT_INVALID`, `execution=failed`, `published=false`, run `waiting_human`. Criterion 1 explicitly says an accurate handoff cannot count as a positive-capability pass. No budget/connection failure was passed off as completion (4/4 requests, 14/20 tools, `budget_unknown=0`).
2. **FAIL on binding / substance sound.** The located dependency, call, impact and trace ids are supported by delivered views and agree with observation; fact/hypothesis/counter-evidence/unknown are separated. But no claim is formally bound (all citations miss).
3. **FAIL.** Citations incomplete (all 14 claims). Plus claim 6 count/scope error (below). Cumulative/increment, missing-vs-zero, backend/raw/view/limit and same-trace associations are otherwise handled correctly.
4. **PASS.** No health/recovery certification, no repair; `next_steps` are advisory and explicitly "without executing changes here".
5. **FAIL** (open P2s below).

### State/rows vs ADR-0005 and PRODUCT-CONSTRAINTS (PASS)

PG: run `waiting_human`, epoch 1, owner/lease null, budget 4/4, 14 tool ops, deadline = submit+1800 s; 5 step rows; 14 evidence rows, all `run_id` = this Run, `sha256(raw)=raw_sha256` for all 14; last event seq 21 `run_handoff` with `parked=true`, `published=false`; incident `conclusion=null`, `lifecycle=open`, single Run. Consistent with "交接不发布结论、事故保持开放". Incident `state='queued'` after handoff: ADR-0005 does not specify the incident state field — 未确认 whether intended; not counted as a finding.

### Findings

- **P2 (MODEL)** — All 14 claims cite `operation_id` short forms instead of the delivered `evidence_id`; contract text and tool-result field naming made the correct form clear. Root cause of REPORT_INVALID.
- **P2 (PRODUCT, prompt/contract; latent)** — `unsupported_citations` rejects any fact citing a non-`ok` view, but the report contract never says so; a correct "no_data ⇒ unknown" fact (claim 7) is rejected even with full ids (counterfactual replay).
- **P2 (MODEL)** — Claim 6: "20 shipping spans carry rpc.grpc.status_code=0 or http.status_code=200". View `62f5d83c-…-t2` has 20 spans: 16 shipping (ok) + 4 load-generator/frontend/frontend-proxy spans with `error=true`/500 (which claim 5 itself describes). Visible-scope/number error; conclusion unaffected.
- **P3 (PRODUCT face)** — Every view exposes `operation_id` as a strict prefix of `evidence_id` with no note that only the latter is citable.
- **P3 (MODEL)** — Email reported only as "unknown" from `no_data` traces, while view `12225891-…-t2` delivered explicit-zero email series (`send_email`, `POST /send_order_confirmation` = 0 all steps); not mentioned. Conservative, meaning unchanged.
- **P3 (MODEL)** — "Convert p95 6.25 rising to 19.0 ms" glosses a dip to 4.94 at step 2.

`VERDICT: FAIL`
`P1=0 P2=3 P3=3`
