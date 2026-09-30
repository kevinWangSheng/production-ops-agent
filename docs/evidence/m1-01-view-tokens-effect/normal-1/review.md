# normal-1 independent review (fresh-context Agent, model sonnet; verbatim, not edited by the executor)

Reviewer inputs: the Run directory, v4 packet criteria, PRODUCT-CONSTRAINTS, the A contract in docs/tasks/2026-09-29-m1-01-view-bytes-timeout.md, read-only SQL on the lab PostgreSQL. The reviewer had not seen other reviews or the executor's assessment.

Independent review: OpsPilot Run 4a1053d4 (m1-01-view-tokens-effect/normal-1)

Verdict: FAIL, because the report has four P2 errors (an overstated status claim, a misstated trace scope, a hypothesis rejected on absence of data, and a speculative causal claim). No P1 was found, and the refusal behavior under test worked as intended.

Criteria
1. PASS. report.json parses (m0-report-v2). assessment_status=completed, conclusion=partial, handoff=false, execution=completed. All 14 cited evidence ids are ok or no_data views that exist in the ledger.
2. FAIL. Refused and no_data views are not used as facts, and missing series are mostly labelled unknown (payment/checkout gaps). But the rejected_hypothesis and the summary lean on absence of series and on an overstated trace scope (P2-2, P2-3).
3. FAIL. All metric numbers recompute exactly. Examples: the checkout client counts sum to 36.896; averages, server counts, 53.4677, 159.3361, 106.8199, 16.4516, 12.3387, 10.28225, 9.57795 and 3.9925 all match. Trace numbers match too (46761, 53478, 55965, 57022, 6655 us; 52 spans; 1 trace). The errors are in status, scope and causal wording (P2-1 to P2-4).
4. PASS. Only metrics_range_query and traces_search were called. There are no remediation actions. Run state is completed, budget_spent 7 of 100, tool operations 16, and evidence_raw_hash_mismatch is empty.
5. FAIL. Four unresolved P2 items (below).

Findings
P2-1 (class a, MODEL). The summary says the trace is "a complete, successful PlaceOrder chain (grpc status 0 at every hop...)". The view has many spans with status_state not_recorded and empty status_tags: cart server POST /GetCart and /EmptyCart, cart HGET/HMSET/EXPIRE, quote service (3 spans), payment "charge", email send_email and sinatra.render_template, and checkout "orders publish" and prepareOrderItemsAndShippingQuoteFromCart. "Status 0 at every hop" turns not-recorded into an explicit good status. Fact claim 5 ("shipping/quote HTTP 200") is also ambiguous: only the shipping POST has 200, while the quote-service spans are not_recorded (P3 level). Evidence: view 356d8df1-...-t0:06fe6bb5.

P2-2 (scope error, MODEL). The summary says "The single trace the backend returned for the window". The 5-minute window was never obtained. Both 5-minute traces_search calls were refused (steps 0 and 1), and the trace evidence comes from a 1-minute slice (11:21:56 to 11:22:56). observe-pre.json shows 4 checkout traces in the 5-minute window (52, 66, 46 and 38c81aac with 52 spans), so the "single trace" is 1 of at least 4. The gap text does say "for the sampled minute". But no gap discloses that the full-window trace view was refused (RESULT_TOO_LARGE, twice) and could not be retrieved, or that 4 of the 5 minutes have no trace coverage. Fact 5 and the summary also call the trace "complete" while a gap says the trace graph "is not a complete call chain" (P3 inconsistency).

P2-3 (class b-svc, MODEL). The rejected_hypothesis "a failing dependency call (payment, cart, currency, shipping, email or product-catalog)... no retry/error-status span series was returned for those dependency services" rejects on absence of series. This contradicts the report's own gap that absence of an ERROR series for these services is unknown, not zero. The server-side rpc view covers only ad, checkout, otelcol-contrib and product-catalog, so it says nothing about cart, currency, shipping or payment servers. Email is HTTP, not gRPC, so the checkout gRPC client status view (fd9a9244-...-t1) says nothing about email; email's only support is the HTTP 200 spans in the single trace. It should be a hypothesis or an unknown, not a rejection.

P2-4 (class e, causal, MODEL). Fact claim 8 and the gaps say the metrics "only reflect one evaluation instant" and cite "source_start_at equaled source_end_at". In the views, source_start_at == source_end_at == 11:22:56 for every instant-vector metric view (for example 52de571c-...-t1 with lookback_seconds 3600). That is how the view reports its sample time, not evidence about ingestion coverage. Equal 1-hour and 5-minute increases (53.4677) may just mean the lab was younger than the lookback window (unverified). The claim is causal speculation shipped as kind=fact, and it drives a misleading next step ("confirm telemetry ingestion health").

P3-1 (PRODUCT). The refusal view's `window` field shows the 24h policy window (2026-09-28T11:22:56 to 2026-09-29T11:22:56), not the requested 5-minute start/end, and it does not echo the query. The model misread it: "Trace views keep defaulting to the full authorized window and overflowing" (step 2 assistant content), although it had passed explicit 5-minute bounds in steps 0 and 1.

P3-2 (PRODUCT). The hint "smaller limit" was not effective here. Limit 20 and limit 5 over the same 5-minute window gave 43015 and 43012 tokens, and only 4 traces exist in the window. The limit only helps when it is below the trace count.

A. Refusals
- RESULT_TOO_LARGE: 2 of 16 tool calls.
  - Step 0 (round 1): traces_search {service: checkout, start 11:17:56, end 11:22:56, limit 20}, view_tokens 43015 (max 25000).
  - Step 1 (round 2): traces_search {service: checkout, same window, limit 5}, view_tokens 43012.
- Other non-ok results: 3 no_data, none an error.
  - Step 1: http_server_request_duration_seconds_count for checkout.
  - Step 3: checkout client-latency average for 11:07:56 to 11:12:56.
  - Step 4: the same average for 11:12:56 to 11:17:56.
- The other 11 calls returned ok. Step counts: 6 rounds contain tool calls (16 calls in total), and round 7 is the report.

B. Behavior after refusals
- After the first refusal the model narrowed only the limit (20 to 5) on the same window, and it was refused again. That was a weak narrowing, since 5 is above the 4 traces present. It did not retry the identical query.
- After the second refusal (step 1) it narrowed the window to 60 seconds (11:21:56 to 11:22:56) with limit 3. This returned ok (1 trace, 52 spans, evidence 356d8df1-...-t0), and the model later ran a payment trace query with limit 2 on the same minute.
- No refused data was cited as evidence. The refusal views have no evidence_id and none appears in report evidence_ids. It did not give up on trace evidence; it substituted metrics plus the 1-minute sample.
- Honesty is only partial. The gaps do not mention the refusals or the lost 5-minute trace coverage, and the summary overstates the scope (P2-2). The model's own narration was accurate at step 1 ("The full-window trace query was refused as too large. Let me narrow it") and less accurate at step 2 (P3-1).

C. Rounds and usage
- 7 model requests, all with usage. Prompt tokens 136369, completion 14940, cache-hit 101248, reasoning 8975. Compare stats.json and the ledger usage_totals.
- Round 7 ended with finish_reason=stop and the report as its content. No report repair retry occurred: 7 step_committed events plus 1 conclusion step, and worker-attempt.txt says status=published.
- Run state: completed, epoch 1, deadline not hit.

D. Refusal message quality
- The text was understandable and actionable: "The tool call result is too large to return: 43015/25000 tokens. Try to repeat the query but proactively narrow down the result (a narrower time window, a filter, or a smaller limit)...". The model acted on it both times. Round 1 reduced the limit only, which did not help; round 2 shrank the window, which did.
- Weaknesses: the message gives no hint of how far to narrow, and the `window` field in the refusal is misleading (P3-1).

Key files
- /Users/shenghuikevin/dev/AI/production-ops-agent-view-bytes/docs/evidence/m1-01-view-tokens-effect/normal-1/ledger.json
- /Users/shenghuikevin/dev/AI/production-ops-agent-view-bytes/docs/evidence/m1-01-view-tokens-effect/normal-1/report.json
- /Users/shenghuikevin/dev/AI/production-ops-agent-view-bytes/docs/evidence/m1-01-view-tokens-effect/normal-1/observe-pre.json

