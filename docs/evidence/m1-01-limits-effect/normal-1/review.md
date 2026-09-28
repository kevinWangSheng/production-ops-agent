# normal-1 独立审查（全新上下文 Agent，Opus；原文照录，未经执行者改动）

Reviewer inputs: v4 packet criteria, PRODUCT-CONSTRAINTS, ADR-0005, the model-visible REPORT_CONTRACT/RUN_COVERAGE_TEMPLATE and tool descriptions, docs/tasks/2026-09-28-m1-01-loop-limits.md (limits-change background), this directory, read-only SQL on 55431. The reviewer did not see the executor's assessment or any other reviewer's output.

**Verdict: normal-1 fails v4.** Criteria 1, 2 and 4 pass; 3 and 5 fail because of four P2 errors, all in how the model wrote the report. It still correctly judged that there was no fault.

**How I checked.** The worktree HEAD is `5e45009`, not `64cad64`, but `git diff 64cad64 HEAD -- opspilot/` is empty, so the contract and tool descriptions I read are the frozen ones. The Run finished normally: `state=completed`, `handoff=false`, 5 model requests out of 100, 21 tool operations.

The Run's plumbing checks out:
- Every evidence row has `run_id=1224763b…`.
- `raw_sha256` matches `raw_sha256_recomputed` for all 21 rows.
- The report's sha256 matches, and the raw text parses to the stored parsed object.
- All 19 claims cite ids this Run delivered, with status `ok` and `citable_as_fact=true`, `target_refs=[m0-otel-20260909]` and `time_scope_ref=policy-window-1`. Both refs appear in the run input.
- I spot-checked the raw rows in PostgreSQL for `896b…-t0` and `07ed…-t0`. The backend counts show 0 checkout error spans (checkout `span_count` 62), which matches the views.

## Criteria

**1. PASS.** The report parses (`m0-report-v2`, `assessment_status=completed`, `conclusion=partial`). "Partial" fits the contract's meaning of "useful findings but unresolved conclusions": elevated latency is left unknown. There was no budget or connection failure. The t7 query returned `no_data`, and the report puts it in gaps (gap 4), not in a fact.

**2. PASS.** The core conclusion is "evidence does not corroborate elevated checkout errors; latency elevation unassessable". The delivered evidence supports it:
- Checkout series are UNSET only (56.73).
- 5 PlaceOrder spans all have `rpc.grpc.status_code` 0.
- Frontend and frontend-proxy ERROR series read 0.

Facts, hypothesis, counter-evidence, rejected hypothesis and recommendation are kept apart. The payment theory is labelled as an unconfirmed hypothesis.

**3. FAIL.** Findings 1–4 below. Everything else I checked is correct:
- Every number in claims 1, 2, 5, 6, 7, 8, 9, 12 and 13 matches the cited view.
- Durations 247008/74056/69860/60837/55949 match.
- The span counts in the counter-evidence and in gap 3 ("5 traces / 241 spans, 20 shown / 221 omitted"; payment 230/250, product-catalog 260/280, cart 354/374, 224/244) match.
- Exactly 3 views are incomplete (product-catalog, cart, frontend), as the report says.
- Missing ERROR series are called unknown in gap 2.
- Counters are read as `increase[300s]`.

**4. PASS.** The report says "absence of checkout errors is not proof of a zero error rate". It certifies no health or recovery, takes no action, and every next step is marked advisory.

**5. FAIL.** Four P2 findings remain unresolved.

## Findings

**P1: none.**

**P2-1, class (a).** Unset status is written as success.
- The summary says cart, currency, email, product-catalog and shipping "show only success statuses in the returned series and in the sampled spans". Claim 11 opens with "Sampled dependency spans … indicate successful handling".
- The cited series say otherwise. In `07ed…-t6`, cart, email, product-catalog and shipping are `STATUS_CODE_UNSET`; only currency is `STATUS_CODE_OK`.
- Almost every cart span in `07ed…-t2` is `status_state: not_recorded`, as are the email `send_email` spans in `-t5`.
- The `traces_search` description says not_recorded "is Unset in OTel terms, not status code 0 and not OK".

**P2-2, class (b).** A missing series is read as "no non-zero code".
- Claim 7 says "no non-zero gRPC status recorded on checkout's outbound dependency calls". The rejected hypothesis says those calls "all carry rpc_grpc_status_code 0".
- `896b…-t3` returned only `rpc_grpc_status_code="0"` series. Non-zero codes are simply absent.
- The metrics tool says "A series that is not returned is unknown, not zero". The report's own gap 2 applies that rule to ERROR span series but not here.
- This Run shows why it matters. Payment ERROR spans exist at 10:09:51, yet the payment ERROR series reads 0, so a new or absent series proves nothing.
- The dependencies are not singled out by name, so this is plain (b).

**P2-3, class (e).** The parent links are stated wrongly.
- Claim 4 and the summary say "the error-tagged payment spans carry empty parent_references (parent_is_visible false / no visible parent)".
- In `07ed…-t0`, only the 2 GET root spans have empty `parent_references`.
- The other 3 error spans (94cf `tcp.connect` and `dns.lookup`, 598a `tcp.connect`) have `parent_is_visible: true`, pointing at the in-trace GET (e.g. `e67e8f0f5caad188`, `83e47bef70ba14b3`).
- No error span has `parent_is_visible: false`.
- The inference "not attached to checkout" still holds, because the chains are rooted at parentless GETs.

**P2-4, class (e).** One claim cites views that don't contain its trace.
- Claim 14 cites `2a04…-t0` (frontend traces) and `69ef…-t5` (metrics) for "POST /api/checkout (263505us) plus grpc.oteldemo.CheckoutService/PlaceOrder rpc.grpc.status_code 0 in trace a316d3abe7eb…".
- The frontend view has no a316 rows. Its only checkout-path rows are trace 2c7487 (69565us and PlaceOrder 59915us).
- The a316 rows appear only in `07ed…-t3` and `-t4` (currency and shipping), which claim 14 doesn't cite.

**P3-1.** Claim 12's "payment transactions were being recorded as successful activity" goes further than a counter increase of 4.408. The meaning is unchanged.

## Product vs model
All four P2s and the P3 are model report defects. I found no product defect: evidence binding, run ownership, raw/view hashes and the report hash are consistent. The projection matches raw for the two rows I spot-checked. Not scored: the ledger still shows the incident row as `state='queued'` with the conclusion set. `persistence.py:1833` writes only `conclusion`, so this may be by design. I did not check.

## Upstream-style verdict
Correctly judged no fault. The report finds no checkout or dependency failure and attributes the payment metadata-probe errors to payment alone. This matches observe-pre.json: `precondition.fault_confirmed=false` and `jaeger.traces_with_checkout_error_and_failed_dependency_child=0`.

VERDICT: FAIL
P1=0 P2=4 P3=1
P2 by class: a=1, b=1, b-svc=0, c=0, d=0, e=2
Upstream-style: correctly judged no fault
