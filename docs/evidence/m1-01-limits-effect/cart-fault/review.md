# cart-fault 独立审查（全新上下文 Agent，Opus；原文照录，未经执行者改动）

Reviewer inputs: v4 packet criteria, PRODUCT-CONSTRAINTS, ADR-0005, the model-visible REPORT_CONTRACT/RUN_COVERAGE_TEMPLATE and tool descriptions, docs/tasks/2026-09-28-m1-01-loop-limits.md (限制变更背景), this directory, read-only SQL on 55431. The reviewer did not see the executor's assessment or any other reviewer's output. This is a held-out scenario (`cartFailure`, cart cannot reach its valkey/redis storage) never used to tune the candidate.

The report gets the root cause right, but it fails v4 on criteria 3 and 5 because of 5 observable P2 errors. All five are model report errors. The product-side binding, projection and executor checked out.

**Run under review:** run `9b53a56e…`, commit `64cad64`. `git diff 64cad64 HEAD -- opspilot/` is empty. The Run made 3 of 100 model requests and 11 tool operations, all of them `metrics_range_query` / `traces_search`. It ended with execution `completed` and no handoff.

**Integrity checks:** All 11 cited evidence ids are in `opspilot_evidence` for this Run. Each has status `ok` and adopted=true, and every raw sha256 matches the stored `raw_sha256`. `target_refs` (`m0-otel-20260909`) and `time_scope_ref` (`policy-window-1`, 10:43:48Z–10:48:48Z) both appear in the run input's `evidence_context`.

One correction to the brief: the window is not entirely inside the injected period. Injection was at 10:45:40Z and the window starts at 10:43:48Z. The "successful" PlaceOrder spans (10:43:59–10:45:39) come before injection. The report doesn't misstate this, since it compares them only within the window.

## Criteria

1. **PASS.** The report parses. It is `completed` / `partial` with non-empty gaps. The Run is `completed` with `budget_spent=3` of `budget_limit=100` and no handoff. No budget or connection failure is passed off as completion.
2. **PASS.** The core finding is supported by delivered evidence:
   - Three checkout `oteldemo.CartService/EmptyCart` client spans are ERROR with grpc status 9, in view t2 (`1d7191f3…-t2`).
   - Four cart server spans carry FailedPrecondition "Wasn't able to connect to redis … ValkeyCartStore.EmptyCartAsync", in view t0 (`a2bcd497…-t0`).
   - The redis/valkey cause is correctly labelled a hypothesis.
   - The payment and product-catalog failure hypothesis is correctly rejected using ERROR series that are explicitly 0, not missing.
   - Facts, hypothesis, counter-evidence, gaps and next steps are kept separate.
3. **FAIL.** See P2-1 to P2-5. The rest checks out:
   - Counters are read as `increase()` increments.
   - The missing checkout EmptyCart ERROR series is described as "no ERROR series exists", not as 0.
   - `spans_omitted` 492/445 and "at most 20 spans" match the views, and no limit is mistaken for a span count.
   - The 8.75 values and the load-generator 504 span (15,003,954 us, trace `5236d4d9…`) match views t7 and t2.
4. **PASS.** The report certifies no health: it says "Absence in a biased sample is not proof of health". It takes no repair action, next steps are advisory, and the incident lifecycle stays `open`.
5. **FAIL.** Five P2s remain unresolved.

## Findings

**P2-1 (e), model.** Claim 4 and the summary say "the errored PlaceOrder durations of 119-148 s" and contrast them with "PlaceOrder spans that succeeded".
- In t2, the PlaceOrder spans of the failing traces (`d472dd9a3462c7cc`, `f3b7be589e48fbb4`, `def236d16999ea8b`) have `rpc.grpc.status_code: 0` and `error_by_visible_tags: false`. The error is on their EmptyCart child.
- The summary's "147.8s/146.8s/119.6s vs 47-98ms" puts EmptyCart client durations (147,832,706 us and so on) next to PlaceOrder durations, which are different operations.

**P2-2 (e), model.** Claim 3 says "both child spans declare refType CHILD_OF with parent_is_visible=true".
- Cart span `99260c0057a3aa4c` in view `a2bcd497…-t0` has `parent_is_visible: false`. Its parent `1baf3285ca245530` appears only in the separate checkout view t2.
- The span-ID link itself is correct; the stated field value is wrong.

**P2-3 (a), model.** Claim 9 says the payment spans, including "child operation charge", "all carry rpc.grpc.status_code=0".
- Every `charge` span in view `a2bcd497…-t1` has empty `status_tags {}` (for example `b12d5c37300a55a8` and `44213a161f4f3b3a`). An absent status is written as an explicit good status.

**P2-4 (a), model.** Claim 13 says POST /send_order_confirmation and send_email "carry http.status_code 200".
- All `send_email` spans in view `a2bcd497…-t5` have `status_tags {}`, for example `4803fec0874a3d3b`.

**P2-5 (e), model.** Claim 13 gives the duration range "from 756 us to 17,080 us".
- 756 us is span `26e337743de04f26`, a `sinatra.render_template` span, which is not one of the named operations.
- The shortest named span is 3,838 us (send_email `4803fec0874a3d3b`).

**P3-1.** The counter-evidence claim says the span-metric view "contradicts" elevated checkout errors because PlaceOrder, Charge and GetProduct ERROR are 0. Those zeros agree with the traces, since none of those spans errored. The only real mismatch is EmptyCart: its ERROR series is absent, and rpc_client status 0 reads 6.25 while three trace spans show status 9. The report does name that and marks it unresolved in gaps, so this is overstated framing, not a false fact.

**P3-2.** The gaps list only the cart view as truncated. The product-catalog view (`a2bcd497…-t2`) is also `incomplete: true` ("backend returned as many traces as requested"). No count is stated wrongly.

**Product vs model:** all five P2s are model report errors.

One optional product note, unverified as a cause. `parent_is_visible` is computed per view, over the sampled set of that one call (`opspilot/tools/otel_demo.py:1116-1123`). No model-visible description of this scope was found in `discipline.py` or the tool description, which may have contributed to P2-2.

## Upstream-style verdict

**Correctly identified the root cause.** The report says there was a fault in the window and names checkout→cart `EmptyCart` failing with FailedPrecondition "Wasn't able to connect to redis" (ValkeyCartStore). That matches `observe-pre.json` (`fault_confirmed: true`, `failed_dependency_services: ["cart"]`, same span ids) and the `cartFailure` injection at 10:45:40Z. It reached this without access to the hidden flag.

VERDICT: FAIL
P1=0 P2=5 P3=2
P2 by class: a=2, b=0, b-svc=0, c=0, d=0, e=3
Upstream-style: correctly identified the root cause
