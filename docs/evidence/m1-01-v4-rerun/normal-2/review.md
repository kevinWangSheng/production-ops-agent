# normal-2 独立审查（全新上下文 Agent，Fable 5.1；原文照录，未经执行者改动）

Reviewer inputs: v4 packet criteria, PRODUCT-CONSTRAINTS, ADR-0005, the model-visible REPORT_CONTRACT and tool descriptions, this directory, read-only SQL on 55431. The reviewer did not see the executor's assessment.

## Run identity and record consistency

- Run `44f23fdb-712b-5826-bd2d-31890f1f5d8d`, incident `b05f5b9a-afab-5f79-afa9-07c7e88cc684`, window 07:11:44–07:16:44Z (`window.json` = every view's `window`). normal-1 (run `9b68c913…`) is in the same DB and was excluded.
- DB `opspilot_runs`: `state=completed`, `budget_limit=4`, `budget_spent=4`, `budget_reserved=0`, `budget_unknown=0`, `tool_operations_used=15`, owner/lease NULL. Matches `ledger.json` and `report.json` (`model_requests_used=4`, `rounds=4`). 4 reservations all `spent`, seconds sum 43.33 = `model_seconds_used`. 15 tool charges ≤ 20.
- 15 evidence rows, all `status=ok`, `adopted=true`, `citable_as_fact=true`; `sha256(raw)` recomputed in SQL equals `raw_sha256` for all 15. `sha256(report_text)` = `report_sha256` = `opspilot_incidents.conclusion.report_content_sha256` (`4f68737a…`, 15768 bytes).
- 22 subject events in DB = `events.jsonl` = `sse.txt`; `run_completed` has `handoff=false, execution=completed, published=true`. `opspilot_controls` is empty (no human control). ADR-0005 satisfied: publish happened only with a qualifying, non-handoff report.
- Tool plans (steps 1–3) are 15 read-only `metrics_range_query`/`traces_search` calls only; no action or repair.
- Product observation (not a report defect): `opspilot_incidents.state` stays `queued` after publish (`persistence.publish()` `opspilot/persistence.py:1798–1861` updates `conclusion` and the run only); `incident.html` line 34 shows "State queued" beside run "completed". Same in normal-1. DB and export agree with each other.

## Criteria

1. **PASS** — parseable `m0-report-v2`; `assessment_status=completed`, `conclusion=partial`, 7 gaps, 4 next_steps; `finish_reason=stop` on round 4 (final request), 4/4 requests and 15/20 tools, no budget/connection failure disguised.
2. **PASS** — core conclusion ("no evidence of checkout failures in window; latency elevation not establishable without baseline") is supported by t0/t1/t2/fb-t3 views, and the engineer-side `observe-post.json` independently agrees (11 checkout series all `STATUS_CODE_UNSET`, `all_services_error_calls` all 0, 7 checkout traces with no error spans). Fact/hypothesis/counter-evidence/rejected/recommendation kinds are used and separated; no engineering answer visible to the model.
3. **FAIL** — all 20 claims cite complete evidence_ids that exist in this Run (no operation_id citations), single target `m0-otel-20260909`, `policy-window-1`; all cited trace ids exist in the cited views; `incomplete` on product-catalog/cart correctly reported with the exact narrower source ranges; missing series treated as unknown (gaps 2–3); model claims only "20 returned/sampled", never backend counts. But four observable number/time errors (P2 below).
4. **PASS** — no health/recovery certification ("neither confirmed nor fully excluded"), no repair, no gate; only read-only queries.
5. **FAIL** — unresolved P2 findings exist.

## Findings

**P2 (all MODEL report defects; none changes the core conclusion)**

1. Claim 6: "PlaceOrder … durations 49385–90342 µs". View `3c549698…-t2`: PlaceOrder spans are 47955–90342 (span `9226aefe1f7df679`, trace `5dfb9c37…`, 47955 µs); 49385 does not occur (nearest is 49585). Summary's "48–90 ms" is correct.
2. Claim 7: "PaymentService/Charge 2950–4937 µs". View `c903358f…-t3`: Charge spans are 2611–4937 (span `b9724ac8b39f2d6b`, trace `fbdf0fb5…`, 2611 µs); 2950 does not occur.
3. Claim 11: "GetQuote server spans 2862–11734 µs". View `fb8bc668…-t1`: GetQuote spans are 3057–11734; 2862 is the child `POST` span `43572440d435dcfe`, which the same claim already lists in the POST group.
4. Claims 2, 4, 5, 13, 14 (time scope / lookback): every metric view has `lookback_seconds=300`, `lookback_start_at=07:06:44`, so the first point (07:11:44) is entirely pre-window, yet range endpoints from that point are reported as "over/within the window". Concrete: claim 5 "increased by 6.25–11.25 per 5m step within the window" — 6.25 is only the 07:11:44 point (`fb8bc668…-t4`); in-window points are 7.5–11.25. Claim 4 cart 151.25 and currency 16.25 are first-point-only values; claim 2 Charge min 8.46, EmptyCart max 11.81, GetCart max 2.51, ShipOrder max 0.55 likewise. The report never mentions lookback and has no gap for it.

**P3**

5. Claim 5: "per 5m step" — step is 60 s with a 5 m lookback; and "successful" is not a label in the view (`app_payment_transactions_total` carries no status dimension), an inference inside a fact claim.
6. Claim 3: recommendation `/flagd.evaluation.v1.Service/EventStream` ERROR series has only 3 points (from 07:14:44) in `c903358f…-t0`; "0 across the window" overstates (absent points = unknown). Background service, correctly listed at its own scope.
7. Claim 9: "CartService GetCart/EmptyCart/AddItem server spans 3048–11432 µs" — 3048 is a plain `POST` child span (`adbbf8eafbbf89fd`); CartService POST spans are 3136–11432; span kind is not in the view.
8. Claim 18 (rejected_hypothesis): text covers six services plus an ERROR-counter statement but cites only the payment/currency/shipping/email trace views; `c903358f…-t4/-t5` and `fb8bc668…-t3` support it and are cited in other claims.
9. Claim 16 (hypothesis): "first five steps / last two steps" — at 30 s step the 0.70–0.78 plateau is the first 7 points and 3.99–4.88 the last 4 (pairs repeat).
10. PRODUCT (state presentation): incident `state` remains `queued` after a published conclusion (see above); DB and export consistent; not a report defect, same in normal-1.

VERDICT: FAIL
P1=0 P2=4 P3=6
