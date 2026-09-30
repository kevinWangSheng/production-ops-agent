# fault-1 independent review (fresh-context Agent, model sonnet; verbatim, not edited by the executor)

Reviewer inputs: the Run directory, v4 packet criteria, PRODUCT-CONSTRAINTS, the A contract in docs/tasks/2026-09-29-m1-01-view-bytes-timeout.md, read-only SQL on the lab PostgreSQL. The reviewer had not seen other reviews or the executor's assessment.

Independent review: OpsPilot fault-1 Run 53c3a9ae-9bf0-5da0-95ff-4188c1056fc5 (view-token batch)

Verdict: PASS, because the report parses, finds the payment Charge failure (checkout->PaymentService/Charge, status 2; PlaceOrder status 13) from delivered ok views, stays hypothesis-level on causation, and states the refusal-caused trace gaps honestly. There is no P1, and the P2 below does not change the core conclusion.

Criteria
1. Report parses and completes: PASS. m0-report-v2, assessment_status=completed, conclusion=partial, handoff=false, handoff_reasons=[], 6 rounds, no repair.
2. Conclusion rests on ok views; facts, hypotheses, counter-evidence and gaps are kept distinct: PASS. The RESULT_TOO_LARGE results were never cited, and the no_data results are cited only as gaps. One overreach in the summary is F1.
3. Numbers, times, sources and scope match the views: PASS with P3 wording issues. I recomputed these from ledger.json tool_results:
   - PlaceOrder status 13 = 3.6996 and status 0 = 1.2500, so about 4.95 calls (2ba83904-t0).
   - Charge status 2 = 3.6996; Convert 13.7504, GetCart 5.0001, GetProduct 8.7503 (06f484b2-t1).
   - Client average latencies 6.197, 3.328, 15.664, 3.215, 0.567, 10.551, 0.253 ms (9f321d2c-t1). product-catalog server 0.5227 ms (56ee0ff8-t3).
   - Error series: frontend-proxy 3.75 and 3.7129, load-generator POST 3.7129 (9a0dc735-t0).
   - Failing trace 89173024335d704d873506c914d5c42f: PlaceOrder 11113 us and root POST 16094 us. Successful trace dfe13ecfb7292e5d1d5a97113c150540: 53597 us and 64935 us, product-catalog GetProduct x8, email POST 200.
   - not_recorded spans (cart, quote, payment "charge", email send_email) are never written as 0/200/OK.
   - The successful trace at 11:29:27 predates the injection at 11:29:33 (fault-timeline). This does not break "same window", since 11:29:27 is inside the 5-minute window, but the report never says the fault began mid-window. It lists onset as a gap.
4. Read-only, authorization, budget, state: PASS. Only metrics_range_query and traces_search were used. All calls stayed inside the policy window. No actions. run completed, 27 events, 18 tool_charges (refused calls are charged), reservations spent.
5. No unresolved P1/P2: PASS with the caveat that F1 is a borderline P2 that does not affect the core conclusion.

Findings
- F1 (P2, class b-svc, MODEL; borderline): the summary says "Cart, currency, shipping, product-catalog, quote and email show no error signals in either the metrics or the two sampled traces". Several of the supporting checks are missing series, not zeros:
  - The service-scoped ERROR span-metric queries for checkout and for cart, currency, shipping, email and product-catalog (56ee0ff8-t0, t1) and the non-zero rpc_server query (t2) are no_data.
  - Claim 4 words the same absence as a fact: "returned no ERROR series at all for checkout, cart...".
  - The report's own gap 3 says absence there is "not evidence of zero". Its counter-evidence claim also shows the span-metric ERROR dimension is unreliable: the payment Charge trace is ERROR while the payment metrics show only UNSET, in 9f321d2c-t2.
  - Positive support exists for cart, currency, shipping and product-catalog (checkout client status 0 in 06f484b2-t1, none non-zero in 9a0dc735-t1, and OK/200 spans in the traces). Email and quote have no positive metric evidence.
  - Suggested fix: restrict the sentence to checkout-client-edge status 0 and sampled spans.
- F2 (P3, MODEL): the rejected_hypothesis ("no measured latency increase accompanies the failures") compares one failing trace (11.1 ms) with one successful trace (53.6 ms). The failing request aborts early, so this is not a latency comparison, and there is no baseline. The summary and gap 1 correctly say latency elevation cannot be asserted. Soften or drop the rejected claim.
- F3 (P3, MODEL): the trace claim says the frontend spans error "with http.status_code 500 and rpc.grpc.status_code 13". Those are two different spans: the api-route span has http 500, and the grpc PlaceOrder span has rpc 13.
- F4 (P3, MODEL): gap 4 says "both attempts to use the 5-minute window returned RESULT_TOO_LARGE". The second refused call was 11:29:44 to 11:32:44 (3 minutes), limit 5, not the 5-minute window.
- F5 (P3, PRODUCT): the refusal payload echoes window.start=2026-09-28T11:32:44 (the 24h policy window) and has no query echo, although the request was 5 minutes. The model read it as "the trace view keeps returning the full window" (step 2 assistant text). It recovered anyway. Echoing the requested window, or the arguments, would remove the misleading signal.

A. Refusals and other non-ok results
- RESULT_TOO_LARGE, 2 calls, both traces_search, both refused as a whole with content=null and citable_as_fact=false:
  - Step 0 (ordinal 2, op 06f484b2-t2): service=checkout, 11:27:44 to 11:32:44, limit=30, view_tokens=46549 (max_view_tokens 25000).
  - Step 1 (ordinal 3, op 9a0dc735-t3): service=checkout, 11:29:44 to 11:32:44, limit=5, view_tokens=33739.
- Other non-ok: 3 metrics no_data results, all in step 3 (56ee0ff8-t0, t1, t2: calls_total ERROR for checkout, calls_total ERROR for the five dependency services, and rpc_server status!=0 for those five services). No error status other than the two refusals. The other 13 calls returned ok, with 18 calls in total.

B. Behaviour after refusals
- Each refusal was followed by a narrower call, and the narrower call succeeded.
  - Step 2 used a 1-minute window (11:31:44 to 11:32:44) with limit=1 and got an ok view of 27 spans (incomplete=true, 9f321d2c-t0).
  - Step 4 used a different 1-minute window (11:28:44 to 11:29:44) with limit=1 and got an ok view of 64 spans (incomplete=true, 2ba83904-t3).
- The model never retried the same refused query unchanged. The step 1 retry narrowed the window and the limit but was still refused (33739 tokens), so it took a second narrowing.
- It did not give up on trace evidence.
- The report states the gap in gap 4: the trace coverage is partial and biased (one trace per minute), the views are incomplete, and the RESULT_TOO_LARGE calls returned no spans. It never uses refused data as evidence. The only inaccuracy is the "5-minute window" wording in F4.

C. Rounds and tokens
- 6 model requests (rounds). Rounds 1-5 finished with tool_calls; round 6 finished with finish_reason=stop and emitted the JSON report itself.
- No repair retry: there is no repair marker, and the single conclusion step is committed at round 6.
- Prompt tokens 110033 and completion tokens 12889 (ledger usage_totals and stats.json agree). Cache hit 73728, reasoning 6252.
- Per-round usage (prompt/completion): 2405/630, 6415/924, 10796/1045, 21720/1413, 25311/1403, 43386/7474.
- Model seconds 52.4.

D. Refusal message quality
- The message is "The tool call result is too large to return: 46549/25000 tokens. Try to repeat the query but proactively narrow down the result (a narrower time window, a filter, or a smaller limit)...". It also carries the structured view_tokens and max_view_tokens fields. It was actionable: the model acted on it both times.
- Step 1 assistant text: "The trace view was too large; I'll narrow it and pull dependency error/latency metrics." It narrowed the window and the limit.
- Step 2 assistant text: "Now the trace view keeps returning the full window. Let me narrow aggressively..." The model misread the echoed 24h window (F5), but the next call was correct: 1 minute, limit 1.
- The refusal did not say how much to narrow. The 3-minute, limit-5 attempt still exceeded 25000 tokens, so it cost one extra round.

Files reviewed (read-only): /Users/shenghuikevin/dev/AI/production-ops-agent-view-bytes/docs/evidence/m1-01-view-tokens-effect/fault-1/{ledger.json,report.json,stats.json,request.json}, ../fault-timeline.jsonl.
