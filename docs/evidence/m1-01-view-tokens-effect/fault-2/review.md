# fault-2 independent review (fresh-context Agent, model sonnet; verbatim, not edited by the executor)

Reviewer inputs: the Run directory, v4 packet criteria, PRODUCT-CONSTRAINTS, the A contract in docs/tasks/2026-09-29-m1-01-view-bytes-timeout.md, read-only SQL on the lab PostgreSQL. The reviewer had not seen other reviews or the executor's assessment.

REVIEW: OpsPilot m1-01 view-tokens-effect / fault-2 (run 360fb924-d04a-5424-8b9a-f1a5681d0aa8)

Verdict: PASS, because the report parses and completes, its facts recompute exactly from delivered ok views (I found no wrong number, time, source or scope), the correct root-cause area (payment Charge failing) is found and hedged, both RESULT_TOO_LARGE refusals were recovered by narrowing, and no P1/P2 issue exists.

Criteria
1. Report parses and completes: PASS. m0-report-v2, assessment_status=completed, conclusion=partial, handoff=false. Run completed, published, finish_reason=stop in round 6, no repair retry.
2. Conclusion rests on delivered ok views, with facts/hypotheses/counter-evidence/gaps kept distinct: PASS. Every cited evidence_id is an ok view. The two refused traces_search calls carry evidence_id=null and citable_as_fact=false and are never cited. The two no_data views (10:39-10:44 baseline) appear only as a gap ("no_data ... ERROR onset and longer trend unknown"). Missing ERROR series for cart, currency, product-catalog, shipping and email are listed as "unknown rather than zero" (Gaps[2]). The rejected_hypothesis (claim 16) says outright that it "reflects absence of returned ERROR evidence, not proven health".
3. Numbers, times, sources and citations match the delivered views: PASS. I recomputed each of these against ledger.json.
   - Claim 0 against d888..-t1: checkout ERROR 2.5 / UNSET 157.5, payment 1.25 / 25, cart 320, currency OK 37.5, product-catalog 173.75, shipping 38.75, email 45.
   - Claim 1 against 098c..-t0: checkout 2.5, frontend 3.75, frontend-proxy 5, load-generator 1.25, payment 1.25, ad 0, recommendation 0.
   - Claim 2 against 6f2d..-t1: all 13 rows match.
   - Claim 3 against 098c..-t1: checkout ERROR 0 / UNSET 132.5, payment ERROR 0 / UNSET 20.
   - Claim 4 against c516..-t0: payment Charge ERROR 1.25, GET, dns.lookup and tcp.connect ERROR 0, email 11.25 / 11.25 / 22.5.
   - Claims 5-8 against 9667..-t0: span ids, durations (31455, 20122, 12921, 7476, 5318, 2474 us), grpc status 13/2, messages, and parent chain PlaceOrder -> client Charge -> payment Charge in both traces.
   - Claims 9-10 (p95 values): all match.
   - not_recorded is not written as good status. Quote, cart-server and payment `charge` spans are stated as "status_state not_recorded" (claim 8).
   - Claim 11 (traces_requested 2, incomplete, reason "backend returned as many traces as requested") is accurate.
   - Time scope: the window 11:39:42-11:44:42 and the adjacent 11:34:42-11:39:42 are stated correctly. The ERROR onset (0 then 2.5 / 1.25) is consistent with the second injection at 11:43:42Z inside the window.
4. Read-only, authorization, budget, state: PASS. Only metrics_range_query and traces_search were used. There were no remediation actions and nothing outside the target/window. next_steps are labelled "Advisory, for a human". Run budget_spent=6 (6 model requests) with reserved=0. DB check: run state=completed and the incident's current_run_id is this run. The incident row shows state=queued/lifecycle=open. I did not verify that against the contract, but it is consistent with the run being completed and published.
5. No unresolved P1/P2: PASS.

Findings (all P3, no P1/P2)
- P3, MODEL. Claim 15 says the "cart ResolveBoolean 11.25 [and] payment ResolveFloat 1.25" both match "the ERROR Charge magnitude numerically". Only 1.25 equals the Charge ERROR count; 11.25 equals the Charge UNSET count (c516..-t0). The claim is labelled a hypothesis and "a lead, not a cause", so meaning is preserved. It is a coincidence-based inference.
- P3, MODEL. Claim 17 and next_steps[0] refer to "the card token supplied/presented by the load generator". No view shows the load generator supplying a token. The load-generator span only has http.status_code 500, and the "Invalid token" text comes from the payment span. This is advisory wording, not a fact claim.
- P3, MODEL. Claim 12 calls Convert 4.99 to 7.5 ms "only slightly higher" (about 50% higher). It is correct that it is the same order and no broad latency elevation is supported. Also, the summary's "cart, currency, product-catalog, shipping and quote spans carry no error tags (grpc status 0 / OK / HTTP 200)" lumps quote in with those. Claim 8 states quote is not_recorded, so nothing is misstated.
- P3, PRODUCT. The RESULT_TOO_LARGE views carry `window` = 2026-09-28T11:44:42 to 2026-09-29T11:44:42 (24h authorized window) and no `query` echo, while ok views carry the queried 5-minute window. The refusal view therefore does not show what was asked. It did not mislead the model or the report here. The message text itself is fine.
- P3, MODEL (contract wording). The report never says that larger trace samples (limit 20, limit 5) were refused as too large. It only says the trace view is a capped 2-trace sample (Gaps[1], claim 11). Nothing is misclaimed and the sampling limitation is disclosed. It just does not name the refusal cause.
- Not a finding, noted for context: the run's own baseline view (11:34:42-11:39:42) contains no fault time, and the reported window has about 1 minute of fault. The report correctly refuses to quantify a failure rate.

A. Refusals
- Refused with RESULT_TOO_LARGE: 2.
  - Step 0 (round 1): traces_search {service=checkout, start 11:39:42, end 11:44:42, limit=20}. view_tokens=108409 (max 25000).
  - Step 1 (round 2): traces_search {service=checkout, limit=5, same window}. view_tokens=42534.
- Other non-ok results: 2, both status=no_data. They are step 2 metrics_range_query calls for 10:39:42-10:44:42 (span-calls ERROR by service, and checkout p95). No error-status or timeout results.
- Ok views: 9 metrics_range_query plus 1 traces_search (step 2, limit=2, 70 spans, result_count 70). The ok views carry no view_tokens field, so I could not check their token size.

B. Behaviour after refusals
- Step 0's refusal was followed by limit 20 -> 5 (narrower), but still refused.
- Step 1's refusal was followed by limit 5 -> 2 (narrower), which gave an ok 2-trace view (9667..-t0, 70 spans).
- The model never retried an unchanged refused query and never abandoned trace evidence. Both refusals were answered with a smaller limit, the same window and the same service filter. The window was not shrunk, but the limit alone was enough.
- The report cites only ok views. Refused data is not used as fact. The trace limitation is disclosed as a capped incomplete sample (claim 11 and Gaps[1]). The refusal cause itself is not named in the report (see the P3 above).

C. Rounds, tokens, termination
- 6 model requests (rounds 1-6); the model ended itself (round 6 finish_reason=stop, no tool calls, planned_tools 0). No report repair retry: the event list has no retry/repair events, only one conclusion step.
- Total prompt 104375, completion 16045, cache-hit prompt 78592, reasoning 8966. Per request prompt/completion: 2403/642, 4133/1448, 7681/1006, 26578/1375, 30245/1458, 33335/10116. Round 6 (the report) used 10116 completion and 4922 reasoning tokens.
- Tool calls: 12 in total (evidence stats: charges 12, evidence 10), all within budget. model_seconds_used about 64.7.

D. Refusal text
- The text was "The tool call result is too large to return: 108409/25000 tokens. Try to repeat the query but proactively narrow down the result (a narrower time window, a filter, or a smaller limit) so that the tool answer fits within the allowed number of tokens." The view also carries view_tokens and max_view_tokens.
- The model's reaction, step 1 content (after the first refusal): "The full trace search was too large; let me narrow it and get operation-level and latency detail." Step 2 content (after the second refusal): "Traces are still too large; narrowing further, and adding baseline windows..." Both show it understood and acted on the message: it cut the limit each time (20 -> 5 -> 2) and pursued the metric evidence in parallel.
- One efficiency note: the message gives no hint of the relative size (108409 vs 25000), and the model needed two narrowing steps. This is not a defect.

Evidence paths: /Users/shenghuikevin/dev/AI/production-ops-agent-view-bytes/docs/evidence/m1-01-view-tokens-effect/fault-2/{ledger.json,report.json,events.jsonl,stats.json}; ../fault-timeline.jsonl (second inject 11:43:42Z, restore 11:46:08Z).
