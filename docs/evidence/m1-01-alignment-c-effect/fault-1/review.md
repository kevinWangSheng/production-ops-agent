# fault-1 独立审查（全新上下文 Agent，Opus；原文照录，未经执行者改动）

Reviewer inputs: v4 packet criteria, PRODUCT-CONSTRAINTS, ADR-0005，模型可见 REPORT_CONTRACT/RUN_COVERAGE_TEMPLATE 与 C2 修复反馈构造器、工具描述，docs/tasks/2026-09-28-m1-01-alignment-c.md（C1-C3 背景），本目录，55431 只读 SQL。审查者未见执行者评估或其他审查者结论。本 Run 触发了 C2 修复反馈重试（round4→round5）。

**VERDICT: FAIL.** No P1, but four P2 errors in the numbers and their cited sources remain unresolved, so criteria 3 and 5 fail. The root cause is correct, the C2 retry worked, and `span_groups` is computed correctly.

Everything below comes from `docs/evidence/m1-01-alignment-c-effect/fault-1` (ledger.json, report.json, observe-pre.json). I checked each claim against the stored evidence rows and recomputed values in the scratchpad. I did not re-query PG or the lab backends. Nothing was edited.

## Criteria 1–5

1. **PASS.** The report parses and was published (`execution=completed`, `conclusion=partial`, 5 model requests, no handoff). Round 4 was rejected and round 5, the one allowed retry, was accepted. The latency question is openly marked unresolved; nothing is passed off as complete.
2. **PASS.** The core conclusion (checkout fails at payment Charge) rests on views the run actually delivered. Facts, counter-evidence, rejected hypotheses, hypotheses and recommendations are kept apart. The claimed call chain holds: for all 6 failing traces I walked the parent references in the checkout trace view (`c457…-t0`) from payment Charge up to the load-generator POST, 10 hops each.
3. **FAIL.** Findings P2-1 to P2-4 below.
   - Handled correctly: missing metric series are called "unknown rather than a measured zero"; the `no_data` baseline view is not read as zero; truncated and incomplete views are listed correctly (6 truncated: `c457-t0`, `fac8-t0/t1/t2/t4/t5`; 2 incomplete: `t1`, `t2`).
   - Every metric value in claims 1, 3 and 8 matches the view content exactly.
4. **PASS.** No health or recovery is certified, the latency question stays unresolved, nothing was remediated, and the recommendation is advisory only.
5. **FAIL.** Four P2 findings are unresolved.

## Findings

**P2-1, class e, MODEL.** Claim 9 gives failing frontend `POST /api/checkout` durations as "30261 to 94302" µs.
- In the cited view, that group's minimum is 30624 (`span_groups`: 6 rows, 30624–94302).
- 30261 is the minimum of a different operation, `executing api route (pages) /api/checkout`.

**P2-2, class e, MODEL.** Claim 9 gives successful frontend `POST /api/checkout` durations of 128935 and 148885 µs, citing only `c457-t0`.
- That view has no successful `POST /api/checkout` row: its group has 6 rows, all `http.status_code=500`, and the successful ones were truncated out.
- The values come from views the claim doesn't cite (`fac8-t0/t3/t4/t5`).

**P2-3, class e, MODEL.** Counter-evidence claim 10 gives the successful payment Charge durations as 9662 and 5386 µs, citing `c457-t0` and `c457-t1`.
- In `c457-t0` the payment-side Charge group holds only the 6 ERROR rows.
- The successful Charge rows it does contain are checkout's client-side spans, at 23073 and 12470 µs.
- 9662 and 5386 come from the payment view `fac8-t0`, which this claim doesn't cite.

**P2-4, class e, MODEL.** Claim 10 argues "failures are not total" because the checkout `STATUS_CODE_UNSET` count (67.5) exceeds `STATUS_CODE_ERROR` (9.94).
- UNSET covers every checkout span name (GetCart, GetProduct, Convert, and so on), not just successful PlaceOrder calls. observe-pre.json shows this breakdown by span name.
- So the comparison could not show non-total failure even if every PlaceOrder failed: a scope and causality error.
- The same claim's trace-based point (2 successful PlaceOrder spans, both before injection) is valid.

**P3-1, MODEL.** The summary and claim 11 say the five other dependencies show "only success/no-error status tags" or "success/OK or unset status tags".
- Every cart span in view `fac8-t2` is `not_recorded`: it has no status tags at all.
- The precise fact claims (5 and 12) correctly say "carry no error status tags". A stricter reviewer could count the summary wording as class a.

**P3-2, MODEL.** Hypothesis 15 says the failures come from "requests carrying the attribute app.loyalty.level=gold".
- No delivered row has a loyalty attribute; the text appears only inside the error-message strings.
- The claim is labelled "not established", so it stays P3.

**P3-3, PRODUCT, optional.** The parser rejects valid JSON wrapped in a Markdown code fence.
- This is contract-conformant, but it used the Run's only retry.
- Round 5 missed the prompt cache almost completely: 224,375 of 225,015 prompt tokens uncached.

**Tally:** P1=0, P2=4, P3=3. P2 by class: a=0 b=0 b-svc=0 c=0 d=0 e=4.

## C2 retry

- **(a) What was wrong with round 4:** only the wrapper. Round 4's content begins with a Markdown code fence (` ```json `), so `json.loads` fails and the result is `REPORT_INVALID`. Unwrapped, the same JSON parses cleanly: all 17 claims cite evidence IDs that exist among the 11 delivered, with target `m0-otel-20260909` and `policy-window-1`.
- **Feedback text:** running `report_retry_feedback` on the stored round-4 content produces "…(REPORT_INVALID). JSON parse error: Expecting value: line 1 column 1 (char 0)…". That is a C2-conformant parse-position message, though it doesn't mention the fence. The text actually sent is not stored anywhere in the evidence; this is a deterministic reconstruction.
- **(b) Did round 5 fix it:** yes. Round 5 is bare JSON and passes both the parse and citation checks.
- **(c) New errors:** none. Round 5 rewords the text and names the truncated, incomplete and `no_data` views explicitly in `gaps`. P2-1 to P2-4 were already in round 4 and carried over unchanged; the retry only addresses the wrapper.

## C3 span_groups

- **(a) Correctness:** I recomputed `span_groups` independently for all 7 `traces_search` views, and all 7 match exactly. Group row totals equal the rows shown (134/133/158/148/92/133/133), and `span_groups_note` is present.
- **(b) Did the report use them:** probably, but that is an inference (the model's reasoning is withheld). Six whole-group ranges match `span_groups` min/max exactly (3361–22223, 29–2852, 5313–18675, 292–7577, 304–1460, and PlaceOrder 6-of-8 with `error_rows`).
- **(c) Where it went wrong:** subset ranges (error-only or success-only) aren't in `span_groups`, so the model computed those itself. Every P2 comes from that self-computed or cross-view work, not from `span_groups`.

## Window contamination

No contamination found.
- All 10 cited views use 17:43:19–17:48:19Z.
- All 6 error traces start 17:45:23–17:47:44Z, after the 17:45:17 injection.
- The 2 successful traces (a15e6f57 at 17:43:31, f37767a4 at 17:43:40) are pre-injection traffic inside the requested window. The report treats them as successes, not as part of the fault.
- The 24h-ago baseline query came back `no_data` and appears only in `gaps`.
- The round-2 query that reached outside the authorized frame (start 2026-09-27T17:43:19) was rejected by the gateway (`INVALID_PARAMS`, `source_contact: none`), so the scope boundary held.
- No earlier fault episode was mixed in.

## Upstream-style verdict

Correct. observe-pre.json records 8 checkout traces, 6 of them with a checkout error and a failed payment Charge child, and `failed_dependency_services: ["payment"]`. The report names the same 6 trace IDs, 6 of 8 PlaceOrder spans in ERROR, and the "Payment request failed. Invalid token" Charge failure.

VERDICT: FAIL
P1=0 P2=4 P3=3
P2 by class: a=0 b=0 b-svc=0 c=0 d=0 e=4
Window contamination: no contamination found (pre-injection successes are in the asked window; the out-of-frame query was rejected; the 24h baseline was `no_data`, used only in `gaps`)
C2 retry: round 4 failed only on the Markdown code fence (verified by reproduction); feedback matched C2; round 5 fixed it with no new errors; the 4 P2s carried over from round 4
C3 span_groups: correct in all 7 views; the report very likely used them for 6 whole-group ranges, correctly; every P2 came from self-computed subset ranges or values pulled from uncited views
Upstream-style verdict: correct root cause (payment Charge failure) — report and observe-pre.json agree on 6 of 8 traces, same 6 IDs, payment the only failed dependency
