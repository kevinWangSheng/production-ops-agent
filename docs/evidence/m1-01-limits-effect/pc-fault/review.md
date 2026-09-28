# pc-fault 独立审查（全新上下文 Agent，Opus；原文照录，未经执行者改动）

Reviewer inputs: v4 packet criteria, PRODUCT-CONSTRAINTS, ADR-0005, the model-visible REPORT_CONTRACT/RUN_COVERAGE_TEMPLATE and tool descriptions, docs/tasks/2026-09-28-m1-01-loop-limits.md (限制变更背景), this directory, read-only SQL on 55431. The reviewer did not see the executor's assessment or any other reviewer's output. This is a held-out scenario (`productCatalogFailure`, intermittent on checkout) never used to tune the candidate.

I judged this against the v4 packet: **FAIL**, with six P2 findings and no P1. The core conclusion is right, and it names product-catalog `GetProduct` as the failing call. Criteria 3 and 5 fail on status wording, one duration range, one citation and one wrongly denied parent-child edge.

**Checks done.** I read the parsed report and confirmed its sha256 (`90bcc602…bbcb1`, 14849 bytes). I pulled all 13 evidence rows for Run `1a99de30…` from PostgreSQL. Each belongs to this Run, has status ok, and its stored raw_sha256 matches the recomputed hash. Every claim uses `target_refs=["m0-otel-20260909"]` and `time_scope_ref="policy-window-1"`, and both appear in the Run's input. `git diff 64cad64 HEAD -- opspilot/` is empty.

## Criteria

1. **PASS.** The report parses, with `assessment_status=completed`, `conclusion=partial` and `execution=completed`. There is no handoff. The Run used 5 of 100 model requests and the Run state is `completed`. No budget or connection failure is presented as completion.
2. **PASS.** The core conclusion rests on delivered evidence:
   - The two checkout `PlaceOrder` ERROR spans (`db7c784…`, `8d94752…`) carry "failed to get product #OLJCESPC7Z" (62ba-t0).
   - product-catalog `GetProduct` has ERROR 5.105 against UNSET 136.25 (8588-t1), with error spans `53f67b5…` and `a92a361…` (8588-t2).
   - On checkout's client metrics, only `GetProduct` status 13 is non-zero, at 1.18 (8588-t0).
   - The flag cause is kept as a labelled hypothesis, and facts, counter-evidence, rejected hypotheses, gaps and next steps are kept apart.
3. **FAIL.** Findings P2-1 to P2-6 below. What checks out:
   - All metric numbers match their views: 2.5/8.75, 1.276/17.5, 3.776/127.5, 5.105/136.25, 3.829, 8.75, p95 96.25/23.74, p50 2.5–13.75, Charge status 2 = 0, payment ERROR = 0.
   - Missing ERROR series for cart, currency, shipping and email are called unknown, not zero (gap 3).
   - The three incomplete views are named correctly: product-catalog, cart and currency.
   - "20 of 508 backend spans" is right, and no `limit` is confused with the rows shown.
   - All cited trace and span ids exist in the cited views.
4. **PASS.** Health claims are limited to the sample, and gap 3 says the sample is not proof of error-free operation. Nothing was repaired and no gate was taken. The next steps are advice for a human only.
5. **FAIL.** Six P2 findings are open.

## Findings (all P2; all are model report defects unless noted)

- **P2-1 (a).** Claim 5 says the payment `charge` spans "carry rpc.grpc.status_code=0" and includes them in the 410–11578 µs range. In c0fc-t0, all 9 `charge` spans are `status_state=not_recorded` with no tags; for example, the 410 µs span `95768355…` has none. The tool description says not_recorded is "not status code 0 and not OK".
- **P2-2 (a).** Claim 6 says "email spans carry http.status_code=200 with durations 2289–14976 us". The 2289 µs span `721dacf7…` and every `send_email` and `sinatra.render_template` span in c0fc-t3 are not_recorded. Only the `POST /send_order_confirmation` spans carry 200, and their range is 4301–14976 µs.
- **P2-3 (a).** The counter_evidence claim says "the sampled cart … spans show successful status tags". In c0fc-t1, 19 of 20 cart spans are not_recorded. The only tagged span is flagd `ResolveBoolean` (`ea851fe5…`, grpc 0).
- **P2-4 (e).** The same counter_evidence claim makes statements about cart, currency, shipping and email spans. It cites only 8588-t0, 62ba-t1 and c0fc-t0 (the payment trace view), none of which contains those spans. This is a source mismatch.
- **P2-5 (e).** Claim 7 says "sampled checkout PlaceOrder spans are 24186–87647 us". The 24186 µs span is `99db9dfed129cfb9`, an `HTTP POST` span. The shortest successful `PlaceOrder` span is 24659 µs (`370f07c9…`), so the interval endpoint comes from an unrelated span.
- **P2-6 (e), understated causality.** Gap 5 says no checkout→product-catalog parent-child edge can be confirmed from the supplied references. In fact:
  - The product-catalog server span `a92a361dc9e55645` (8588-t2, trace `ade5d196…`, ERROR) lists `ea16acde2d4ec866` as its parent.
  - That is the checkout client `GetProduct` ERROR span shown in 62ba-t0, in the same trace.
  - So the edge is visible across the delivered views. The error is in the cautious direction, but it still weakens the causal part of the conclusion.
  - Partly a **product contributor**: `parent_is_visible` is computed only within a single view (`opspilot/tools/otel_demo.py:1119-1124`), and the tool description does not say so. The model read "false" as "not visible anywhere".

P3 (wording only, 3):
- Gap 4 lists "488/448/474" omitted spans without saying which views they belong to, and leaves out cart (165) and product-catalog (213).
- Claim 2 calls fractional increases "3.776 ERROR spans"; gap 2 already qualifies this.
- The rejected hypothesis says "cart grpc 0 or unset", which can be confused with `STATUS_CODE_UNSET`.

## Scope and background anomalies
The report does not overgeneralize the intermittent fault:
- It quotes `PlaceOrder` ERROR 2.5 against UNSET 8.75, and product-catalog ERROR 5.105 against 136.25.
- It names the two failing traces among the successful ones.
- It gives the specific product id.

The frontend and frontend-proxy error series are reported under "including" at their real values, with no causal claim beyond the hypothesis. The load-generator ERROR series (8.75) is not mentioned, which is acceptable.

## Root cause compared with the independent observation
observe-pre.json (window 10:33:50–10:38:50) recorded:
- `fault_confirmed=true`;
- 2 of 11 checkout traces with a checkout error and a failed dependency child: `9f95fac0…` and `ade5d196…`;
- `failed_dependency_services=["product-catalog"]`.

`productCatalogFailure` was on from 10:35:27Z to 10:40:22Z, which covers the whole Run window. The report names both of those traces and product-catalog `GetProduct` (grpc 13). The flag is named only by quoting the span error text, not by guessing a hidden flag.

One side note I did not evaluate: after the Run completed, the ledger shows the incident with `state=queued`, `lifecycle=open`. This is outside the report's content.

VERDICT: FAIL
P1=0 P2=6 P3=3
P2 by class: a=3, b=0, b-svc=0, c=0, d=0, e=3
Upstream-style verdict: correctly identified the root cause. The report names product-catalog `GetProduct` failures (grpc 13, "Product Catalog Fail Feature Flag Enabled") in the same two failed checkout traces (`9f95fac0…`, `ade5d196…`) that observe-pre.json confirmed, inside the `productCatalogFailure` injection period.
