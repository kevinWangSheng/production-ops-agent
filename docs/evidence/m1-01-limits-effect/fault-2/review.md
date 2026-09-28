# fault-2 独立审查（全新上下文 Agent，Opus；原文照录，未经执行者改动）

Reviewer inputs: v4 packet criteria, PRODUCT-CONSTRAINTS, ADR-0005, the model-visible REPORT_CONTRACT/RUN_COVERAGE_TEMPLATE and tool descriptions, docs/tasks/2026-09-28-m1-01-loop-limits.md (限制变更背景), this directory, read-only SQL on 55431. The reviewer did not see the executor's assessment or any other reviewer's output.

**Verdict: FAIL.** The v4 FAIL rests on one borderline P2: one sentence treats missing error series as proof of zero errors. Everything else in the report checks out. It names the right root cause, a failing checkout → payment `Charge` call. It cites only evidence this Run delivered, and I found no P1.

I ran against the worktree at HEAD `5e45009`. That is one test-only commit after the frozen candidate `64cad64`, and the Run itself records prompt revision `prompt-replay-candidate-cc542dd15dd8`.

**What I checked**
- The report text hash matches `report_sha256` (`4c500d24…`, 14458 bytes).
- The Run finished normally: `execution=completed`, `handoff=false`, `published=true`, 4 of 100 model requests used, final `finish_reason=stop`.
- All 8 cited evidence ids exist in `opspilot_evidence` with `run_id` = `6e23da7b…`. Each one's view matches the ledger export, the raw sha256 and byte length recompute exactly, and the content equals what the model was given.
- Every claim uses `target_refs=["m0-otel-20260909"]` and `time_scope_ref="policy-window-1"` (10:21:40–10:26:40Z). Every cited view has status ok, `citable_as_fact=true`, and a 300s `increase()` over the window.

## Criteria

1. **PASS.** The report parses, says `completed`/`supported`, and all 8 tool calls returned ok. Nothing hit a budget or connection limit. The unanswered latency part is stated openly in the summary and in gaps.
2. **PASS.** The core conclusion is backed by delivered evidence:
   - Checkout `PlaceOrder` has ERROR 12.5 and UNSET 0, so every checkout failed. The checkout-side `PaymentService/Charge` client span has ERROR 12.5 (view `…262845fc`).
   - The checkout client RPC for `Charge` shows status code 2 = 11.25 and code 0 = 0 (view `…fb171008`).
   - The payment server span `Charge` is ERROR with "Payment request failed. Invalid token…", from `charge.js:37:13` (view `…42b21d80`).
   - The checkout → payment link is correctly labelled a hypothesis. Span `cd24cca9…` has parent `83307382…`, and span `5ce0dc9f…` has parent `4e1b7fa4…`; both parents appear as checkout client `Charge` spans.
   - Facts, hypotheses, rejected hypotheses and gaps are kept apart.
3. **FAIL, on P2-1.** Everything else is correct:
   - Numbers match the views exactly: 24.9999/92.4996; cart 226.25, which is the sum of its operations; product-catalog 107.5; currency 31.25 OK; shipping 25; the all-services ERROR list with recommendation at 0; frontend 37.5 = 3 × 12.5; frontend-proxy 23.75 = 12.5 + 11.25.
   - The average durations are ratios of the sum and count increases, not computed from the point list: `Charge` 205.04/11.25 ≈ 18.2 ms; `GetQuote` ≈ 15.3; `Convert` ≈ 4.8; `GetCart` ≈ 3.4; `GetProduct` ≈ 1.5.
   - Counts are not mixed up: "11 traces" is `backend_traces_returned` (limit was 20). 318/5 omitted in the checkout view and 10 shown / 323 omitted in the payment view are correct. Both trace views are correctly called truncated, and neither is marked incomplete.
   - All 11 cited checkout trace ids and 10 payment trace ids are rows in the cited views, and each span/trace pair cited is correct.
   - Values are correctly described as window-scaled extrapolations.
4. **PASS.** No health or recovery is certified, no repair was taken, and no release gate was granted. The flagd/fault-injection idea appears only as an unknown and a suggestion for a human to check, not as a claim.
5. **FAIL.** P2-1 is unresolved.

## Findings

- **P2-1, class (b), model report defect.** Claim 3 states as fact: "Payment is the only dependency with ERROR spans." For product-catalog, cart, currency, shipping and email, the only basis is that no ERROR series was returned. The tool contract says a series that is not returned is unknown, not zero. The report's own gap #2 says exactly that, so the claim contradicts its own gaps.
  - The same pattern appears in the rejected hypothesis for email. Email had 0 activity in the window, so no evidence could show it was not failing.
  - Borderline: the second sentence of claim 3 scopes things correctly ("returns ERROR series only for…"). The rejection is well supported for cart, currency, product-catalog and shipping by positive non-error traffic and checkout-client status 0 counts.
  - The real state agrees with the claim (observe-pre shows payment as the only failing dependency), but the delivered evidence does not establish it.
- **P3-1.** The summary says the sampled traces "all carry the same description: 'failed to charge card…'". Payment spans and checkout client `Charge` spans carry only the shorter "Payment request failed. Invalid token…" text.
- **P3-2.** The second rejected hypothesis says "payment server spans are 2.3–10.2 ms". That is the min/max of the 10 sampled rows (2317 and 10161 µs) but is not labelled as sample-only. Gap #3 does mark the views as samples.
- **P3-3.** `conclusion=supported` while the latency half of the question is unresolved; "partial" arguably fits better. It doesn't mislead because the summary and gaps state the gap plainly.

**Product vs model:** there are no product defects. Evidence binding, projection, executor, events (`run_completed` with `published=true`) and hashes are all consistent. All findings are in the model's report text. One side note I did not grade: the ledger incident row shows `state="queued"` with a non-null conclusion. That looks like how the product stores state after publish (`publish()` doesn't update `incidents.state`). It has no bearing on report quality; I didn't investigate further.

**Upstream verdict:** correctly identified the root cause. The report names the checkout → payment `Charge` call failing with "Invalid token", with all checkout `PlaceOrder` calls failing and the failure showing up in frontend and frontend-proxy. That matches observe-pre (11/11 traces with a checkout error and a failed payment child) and the `paymentFailure` injection window (10:19:55–10:27:56Z).

VERDICT: FAIL
P1=0 P2=1 P3=3
P2-by-class: a=0 b=1 b-svc=0 c=0 d=0 e=0
Upstream-style: correctly identified the root cause. The report names payment `grpc.oteldemo.PaymentService/Charge` rejecting checkout `PlaceOrder`'s charge call ("Invalid token"; checkout `PlaceOrder` ERROR 12.5 / UNSET 0), which matches observe-pre's `failed_dependency_services=["payment"]` in 11/11 traces during the `paymentFailure` injection.
