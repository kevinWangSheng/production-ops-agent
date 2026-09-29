# fault-2 独立审查（全新上下文 Agent，Opus；原文照录，未经执行者改动）

Reviewer inputs: v4 packet criteria, PRODUCT-CONSTRAINTS, ADR-0005，模型可见 REPORT_CONTRACT/RUN_COVERAGE_TEMPLATE/REPORT_RETRY_TEMPLATE 与工具描述，docs/tasks/2026-09-28-m1-01-upstream-alignment-b.md（B1-B6 背景），本目录，55431 只读 SQL。审查者未见执行者评估或其他审查者结论。本 Run 触发了 B3 格式修复重试（round 8 → round 9），审查者被特别要求核查该重试是否修对了问题。

**VERDICT: FAIL.** The report gets the root cause right, but it has 4 P2 errors, so criteria 3 and 5 fail. Round 9 fixed the one thing that got round 8 rejected, but it also brought in two new P2 errors (P2-1 and P2-3 below).

I made no edits. Evidence came from this directory's `{report.json,ledger.json,observe-pre.json,events.jsonl}`. I re-ran the product's own `parse_report`, `unsupported_citations` and `delivered_view` from the worktree `.venv` against the ledger rows. [本机路径已由执行者脱敏为仓库相对路径，审查结论原文未改]

Note: `ledger.json` has no `tool_results[].result.window` path. The windows below come from `evidence[].view.window` and `.query` (27 views) and from `steps[].tool_results`.

**Actual query windows**
- **Target:** 13:07:56–13:12:56Z. All 7 trace views and 5 metric views used it, so "last 5 minutes" was read correctly.
- **Comparison window:** 13:00–13:05Z (5 views).
- **Uncited views:** 13:05–13:10Z (checkout UNSET explicitly 0, i.e. no checkout traffic) and 12:12:56–13:12:56Z (1h view: checkout ERROR 2.651, the same as the 5-minute value).
- **no_data:** 12:07:56–12:12:56, 12:45–12:50 and 12:40–12:50.
- **The one error was not a 24h query.** It was a *future* window, 13:52:56–13:57:56, refused with `INVALID_PARAMS` (step round-3, ordinal 2). Its view shows the 24h outer frame as `window`.

### Criteria
1. **PASS.** The report parses (`m0-report-v2`, completed/partial), with `handoff=false` and `published=true`. It used 9 model requests and no compaction.
2. **PASS.** The core conclusion (checkout PlaceOrder fails with code 13 because payment Charge returns "Invalid token") is supported by trace views t0/t1 and metric view f6a7-t0/t1. Facts, hypotheses and gaps are kept separate.
3. **FAIL.** Two citations don't back what the claim says (P2-3, P2-4).
4. **PASS.** Nothing is declared healthy without evidence: email is written as "unknown rather than healthy", and cart's not_recorded status is not called OK. No remediation and no gate authority.
5. **FAIL.** 4 P2 findings are open.

### B3 retry (round 8 → round 9)
- **Round 8 deserved rejection.** Re-running `unsupported_citations` flags exactly one claim: claim 12, a `fact` about email. It cites `929c18b9-…-t6`, whose status is `no_data` and `citable_as_fact=False`, which breaks REPORT_CONTRACT. Every other round-8 claim passes.
- **Round 9 fixed that specific problem.** The email statement moved to gaps, and the re-check returns `unsupported_citations=False`. The larger estimated prompt (193,038 → 203,899) fits the round-8 reply plus feedback being sent. The raw request text is not stored, so this part is inferred.
- **Round 9 also introduced new errors.** The "not total failure" counter-evidence (P2-1) is new. The product-catalog counter-evidence changed its citation from t2 (right) to t0/t1 (wrong) (P2-3). A gap statement about the 1h view was dropped (part of P2-2).
- **The feedback didn't say what was wrong.** The message sent was only "(REPORT_INVALID) … citing only evidence_id values already delivered". The cited id *was* delivered; the actual fault was citing a no_data view as a fact. The B3 contract asks for "哪条 claim、哪个 evidence_id、何种错误". The task record shows the implementation chose to send only the reason code. See P3-3.

### Findings
**P1: none.** No permission, human-control, budget or state-recovery violation.

**P2-1 (e), model, new in round 9.** A counter_evidence claim says checkout shows "a recurring but not total failure pattern" because 51.35 UNSET spans coexist with 2.65 ERROR. That misreads span counts: the UNSET spans are child operations (GetCart, Convert, …). Per span name the evidence shows every order failed:
- PlaceOrder: ERROR 1.3256, UNSET 0 (f6a7-t0). The report's own claim 1 says this.
- Charge client call: code 2 = 1.324, code 0 = 0 (f6a7-t2).
- `app_payment_transactions_total` for payment = 0 (6a74-t2).
- All 6 of the 6 checkout traces the backend returned are errors (limit 30).
- observe-pre agrees: PlaceOrder UNSET 0, and 6 of 6 traces in its window.

**P2-2 (e), model.** The report calls 13:00–13:05 "the immediately preceding comparable 5-minute window" and concludes the errors "are new in the final 5 minutes rather than sustained". It leaves out its own 13:05–13:10 view, where checkout traffic was an explicit 0 (the environment's traffic dip). With no traffic in that stretch, onset between 13:05 and 13:10 cannot be observed. The fault was actually live from 13:03:39Z. The gap text "only 13:00–13:05 could serve… no longer-horizon error trend can be asserted" also ignores the 1h view d3cb-t3, which round 8 had mentioned. I rate this one plausible rather than confirmed: if "new" is read as "first observed", it drops to P3.

**P2-3 (e), model, citation regression in round 9.** The counter-evidence "product-catalog… observed durations are sub-10 ms" cites trace views t0/t1. Those contain zero product-catalog spans, and their checkout-side GetProduct client spans reach 12.57 ms. The view that supports the claim is t2 (max 9.575 ms), which round 8 cited and round 9 dropped.

**P2-4 (e), model, carried over from round 8.** Claim 13 says "the same regex over 13:00–13:05 likewise returned only checkout and product-catalog". The cited baseline view b7a4-t0 used the narrower regex `checkout|payment|product-catalog`, so it cannot show that cart, currency and shipping were absent. The statement is true only through b7a4-t2, which is not cited.

**P3-1, model.** Says span f1146bad "carr[ies] error=true". Its tags are only `http.status_code: 500` (the projection still sets `error_by_visible_tags=true`).

**P3-2, model.** The bucket difference is given as 6.334138365279; the actual value is 81.0769724507204 − 74.74283397800787 = 6.33413847271.

**P3-3, product.** The B3 retry feedback is generic and points at the wrong cause (details in the B3 section). It didn't make the final report wrong, but it doesn't meet the B3 contract text. Whether to accept the documented implementation choice is a contract decision for lead.

**Context (B4):** about 1.73M prompt tokens across 9 rounds, no compaction. The only self-contradiction is P2-1, which conflicts with claim 1 in the same report. I found no drift between early and late rounds otherwise.

**Traffic anomaly:** it thinned the evidence (13:05–13:10 had zero checkout spans, and the target window has fewer orders than 13:00–13:05). The report does not blame the payment fault for it. The product-catalog 2500–5000 ms tail, likely contention, is kept as an unconfirmed hypothesis with a recommendation to validate it, and is not linked to payment.

VERDICT: FAIL
P1=0 P2=4 P3=3
P2-by-class: a=0 b=0 b-svc=0 c=0 d=0 e=4
Window contamination: no contamination found. All 6 cited trace ids start between 13:10:00 and 13:12:48Z, inside the paymentFailure episode (13:03:39–13:16:48Z). The 13:00–13:05 comparison window overlaps only the first 81 s of the *same* episode (no errors seen there). No other fault had been injected. Pre-12:55:47Z windows returned no_data and were written up as gaps, not facts.
Upstream-style verdict: correct root cause. The report names payment Charge "Invalid token" failures behind checkout PlaceOrder code 13, in the same 6 trace ids (1cd2…, 40cb…, c414…, 60ad…, 4c12…, e9a1…) that observe-pre.json lists with `failed_dependency_services=["payment"]`.
