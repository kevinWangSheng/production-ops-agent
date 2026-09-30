# normal-2 independent review (fresh-context Agent, model sonnet; verbatim, not edited by the executor)

Reviewer inputs: the Run directory, v4 packet criteria, PRODUCT-CONSTRAINTS, the A contract in docs/tasks/2026-09-29-m1-01-view-bytes-timeout.md, read-only SQL on the lab PostgreSQL. The reviewer had not seen other reviews or the executor's assessment.

Independent review: OpsPilot Run normal-2 (run cf6496e3-d545-5389-b71a-25585685ecb0), view-token batch

Verdict: FAIL, because the report has two unresolved P2 model-side errors: a missing-series/absence over-claim that misplaces the start of the error burst, and not_recorded spans written as "successful". The core conclusion (no checkout/payment errors or latency elevation in 11:36:22-11:41:22) is correct and rests on ok views. No P1 was found.

Criteria
1. Report parses and completes: PASS. m0-report-v2, assessment_status=completed, conclusion=partial, handoff=false, no handoff reasons. It has 13 claims (8 facts, 2 counter_evidence, 2 hypotheses, 1 rejected_hypothesis), 7 gaps and 4 next_steps.
2. Core conclusion rests on ok views, categories kept distinct: PASS on the core conclusion, with the P2 items below in the derived claims. No claim cites the refused view (55695580-t0 has no evidence_id, only operation_id) or the no_data view 5a3ab20b-t1. The no_data view appears only in gap 2, as a gap. Cart, currency, shipping, email and product-catalog are correctly kept as "unknown, not zero".
3. Numbers, times, sources and citations match the views: FAIL. Almost everything recomputes correctly, including:
   - checkout ERROR 0 / UNSET 65 and payment ERROR 0 / UNSET 10 in the last 5 minutes;
   - p95 21.142857 vs 19.837242 / 9.642857 / 16.060787 for the windows ending 11:26:22 / 11:31:22 / 11:36:22;
   - hour totals 11.862229, 5.931115, 17.793344, 14.489784, 6.993189, 1.135435, ad 0;
   - Charge status 2 = 0 in the window and 6.988716 in the hour;
   - trace ids and spans (98 spans over 11:40:39-11:40:59; 92 spans over 11:36:03-11:36:07).
   Two P2 remain (F1, F2).
4. Read-only, authorization, budget, state: PASS.
   - Only metrics_range_query and traces_search were used (15 calls); there are no remediation actions.
   - The run ended run_completed / published with execution=completed.
   - stats.json shows evidence_raw_hash_mismatch=[].
   - The model used 8 requests and about 106 s of model time.
   - All budget reservations are spent.
   - The fault inject/restore events in fault-timeline.jsonl were done by the lab operator and are not tool actions of the Run.
5. Overall no unresolved P1/P2: FAIL (F1, F2).

Findings

F1. P2, class b (missing buckets read as proof of absence), plus class e (burst-start time). Attribution: MODEL.
- The summary says "the only nonzero 5-minute ERROR bucket for checkout/payment is the one ending 11:36:22".
- Claim 10 says the burst was "inside the 5-minute bucket ending 11:36:22 (between 11:31:22 and 11:36:22)" and "cleared by 11:36:22-11:41:22".
- View 32cdbcdd-...-t0 (query start 10:41:22) returns for checkout PlaceOrder, checkout client Charge and payment grpc Charge only two points, at 11:36:22 (8.333) and 11:41:22 (0). The buckets ending 11:26:22 and 11:31:22 are absent, so they are unknown, not zero. The payment GET/dns.lookup/tcp.connect series do have points at 11:21:22 through 11:41:22.
- The hour view 9286ca84-...-t0 gives checkout ERROR 11.862 in the hour, more than the 8.333 in that single bucket. That is a visible hint of errors elsewhere in the hour that the report never reconciles.
- Lab ground truth in docs/evidence/m1-01-view-tokens-effect/fault-timeline.jsonl: a fault was injected at 11:29:33Z and restored at 11:33:54Z. The burst therefore started before 11:31:22, outside the stated bucket. The report's own claim 5 is correctly worded ("only nonzero bucket returned"), but the summary and claim 10 drop "returned" and state a start bound as fact. The gaps do not list the absent earlier buckets.
- The "burst is before the window, not in it" conclusion still holds.

F2. P2, class a (span status not_recorded written as a good status). Attribution: MODEL.
- Claim 7 says the two 11:36 traces "contain only successful checkout PlaceOrder, payment Charge, cart, currency, shipping, product-catalog, email and frontend spans". Gap 1 says "show only successful spans".
- Claim 12 says the shown spans of the other dependencies "carry rpc.grpc.status_code=0 or http status 200".
- In views 5a3ab20b-t0, 3fad8a7f-t0 and 3fad8a7f-t1, 34 of 92 spans have status_state=not_recorded, and 17 of 46 in 3fad8a7f-t1.
  - Cart service-side spans are 12 of 14 not_recorded (POST, EXPIRE, HGET, HMSET and the CartService server spans).
  - So are payment "charge", email send_email and render_template, shipping-quote spans, checkout "orders publish" and prepareOrderItems...
- The correct statement is "no error-tagged span; recorded statuses are grpc 0 / http 200 only on the named client/server spans". Claim 2 is worded correctly (only the named recorded operations); claims 7 and 12 are not. This is a wording-level overreach, but it is the class-a pattern.

P3 (do not affect the verdict)
- Claim 8 has the typo "GPRC".
- The "last 5 minutes" trace query actually ran with a 2-minute window (11:39:22-11:41:22) after the refusal. Gap 3 discloses the 2-trace, ~20 s sample, but no claim states the actual 2-minute query window.
- Latency evidence is only checkout's aggregate rpc-client p95. There is no per-dependency latency and no trace-duration comparison. The wording "no elevated latency is demonstrated" is hedged, and gap 6 only mentions the four points.
- Numerical inconsistency not flagged: the payment hour ERROR increase (5.93) is smaller than one 5-minute bucket (8.33). Gap 7 covers increase() extrapolation only generically.
- PRODUCT (minor): the RESULT_TOO_LARGE view reports window 2026-09-28T11:41:22..2026-09-29T11:41:22 (the 24 h policy window), not the requested 5-minute window, and does not echo the query. The model's next call was not misled, but the field is misleading for audit.

A. Refusals
- Refused with RESULT_TOO_LARGE: 1 of 15 calls.
  - Round 1, ordinal 0, traces_search {service:"checkout", start:2026-09-29T11:36:22+00:00, end:11:41:22+00:00, limit:20}.
  - view_tokens=57502 vs max_view_tokens=25000; status "error", citable_as_fact=false, content=null.
- Other non-ok results: 1 no_data. Round 5, ordinal 1, metrics_range_query with [60s] ERROR increase, step 60, window 11:26:22-11:41:22; content=[], citable_as_fact=false.
- 13 of 15 calls were ok. There were no other errors.

B. Behaviour after refusal
- Round 2 narrowed the query: the same service (checkout) with limit 5 and a 2-minute window (11:39:22-11:41:22), which returned an ok view (98 spans, 2 traces, 4ec485f2-t0).
- It never retried the refused query unchanged and never gave up on trace evidence. Later trace calls used narrower, targeted scopes (limits 2/2/1, a 5-minute window at 11:31:22-11:36:22), all ok.
- The report says honestly what was lost. Gap 3 states that the limit-20 trace request ended in RESULT_TOO_LARGE with no spans and that the trace picture therefore rests on a 2-trace ~20 s sample. Claim 9 repeats that the view cannot establish that no failure occurred elsewhere. The refused data is not cited as evidence anywhere.
- The no_data view is likewise a gap only, in the final report. The first draft, from round 7, cited it as evidence in claims 9 and 10, and that draft was replaced (see C).

C. Run economics
- 8 model requests (rounds 1-8): 6 tool rounds, then a report draft in round 7, then a replacement report in round 8.
- Tokens from ledger usage_totals and stats.json: prompt 340,021, completion 26,234, prompt cache hit 175,360, reasoning 15,176.
- The model ended itself with finish_reason=stop; there was no forced cut-off before the report.
- A report repair retry did occur:
  - The round-7 report was not accepted. Round 8 has context.final=true and prompt_cache_hit only 640.
  - The round-7 claims 9 and 10 cited the no_data view 5a3ab20b-t1; the round-8 claims cite only ok views.
  - The rejection reason is not recorded in the ledger. I infer (unverified) that the validator blocked the citation of a no_data view.
- Model seconds used: 105.7. DeepSeek balance went from 12.70 to 11.19 CNY across the whole 5-run batch (the balance files are batch-level, not per-run).

D. Refusal message quality
- The message was: "The tool call result is too large to return: 57502/25000 tokens. Try to repeat the query but proactively narrow down the result (a narrower time window, a filter, or a smaller limit) so that the tool answer fits within the allowed number of tokens."
- It was actionable. The model's round-2 text was "Trace view was too large; narrowing." and its next call narrowed both limit (20 to 5) and window (5 min to 2 min).
- The measured overshoot (2.3x) was large. The model narrowed by 4x in limit and 2.5x in window, and the result was ok. Later trace views are about 63 KB of JSON each and were accepted, so the per-view token metering behaved as specified in this Run.
- The only oddity is the window field on the error view (see the PRODUCT P3 above).

Paths
- Run directory: /Users/shenghuikevin/dev/AI/production-ops-agent-view-bytes/docs/evidence/m1-01-view-tokens-effect/normal-2 (ledger.json, report.json)
- Fault ground truth: /Users/shenghuikevin/dev/AI/production-ops-agent-view-bytes/docs/evidence/m1-01-view-tokens-effect/fault-timeline.jsonl
