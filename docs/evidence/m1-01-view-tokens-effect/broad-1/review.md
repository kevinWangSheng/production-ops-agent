# broad-1 independent review (fresh-context Agent, model sonnet; verbatim, not edited by the executor)

Reviewer inputs: the Run directory, v4 packet criteria, PRODUCT-CONSTRAINTS, the A contract in docs/tasks/2026-09-29-m1-01-view-bytes-timeout.md, read-only SQL on the lab PostgreSQL. The reviewer had not seen other reviews or the executor's assessment.

Independent review: OpsPilot Run 3af1ace2-bb99-58ec-9f04-9bdcfcbd3f8d (broad-1, view-token refusal batch)

Verdict: FAIL, because the report contains several P2 numeric, scope and citation errors (a false "payment non-zero at 18:25:29Z", a mixed-up flagd latency range, an overclaimed "per-service trace searches for all 17 services", and citations that do not support the claims they are attached to). No P1 was found and the RESULT_TOO_LARGE mechanism itself worked as intended.

Criteria
1. Report parses and completes: PASS. Final report (round 9) parses as m0-report-v2, assessment_status=completed, conclusion=partial, handoff=false. The round 8 output was invalid: it carries trailing junk after the JSON ("Extra data" at char 32056, DSML residue). Round 9 was the repair retry and was accepted.
2. Core conclusion rests on delivered ok views, with facts, hypotheses, counter-evidence and gaps kept distinct: PASS with caveats. All 28 evidence ids in the report are ok views, and none is a refused or no_data view. image-provider and kafka are stated as "unknown rather than zero". Hypotheses are labelled (claims 27-29), and the counter_evidence and rejected_hypothesis claims are sound. The caveats are the P2s below.
3. Numbers, times, sources and scope match the delivered views: FAIL. Almost all metric numbers recompute exactly (checked claims 1-9, 21 and 22 against the ledger views). The exceptions are the P2 findings below.
4. Read-only, authorization, budget and state: PASS. Only metrics_range_query and traces_search were used, with no remediation and no out-of-scope service values. The run ended completed. It used 9 of 100 model requests, and 35 tool operations were charged, including the 4 refusals. No budget_unknown, and evidence_raw_hash_mismatch is empty.
5. No unresolved P1/P2: FAIL, see the findings.

Findings
No P1.

P2-1 (class e, MODEL). Claim 10 (view fdb95655, 3600 s error-increase query) says frontend, frontend-proxy, load-generator "and payment" were non-zero at 18:25:29Z and 0 at 11:25:29Z.
- The view shows payment as [1790619929 (18:25:29Z), "0"] and [1790681129 (11:25:29Z), "0"], so it was 0 at both instants.
- The claim is wrong for payment. Frontend 52.39, frontend-proxy 31.43 and load-generator 15.71 at 18:25:29Z are correct.

P2-2 (class e, MODEL). Claim 26 says flagd ResolveBoolean spans with rpc.grpc.status_code=0 lasted "74 to 9836 microseconds".
- In the delivered views the flagd-service spans with status 0 last 321 to 399 us. Its unlabelled resolveBoolean spans last 60 to 305 us.
- 9836 us belongs to a cart-service span, and 74 comes from a span with no status tag. The range mixes attribution and status.

P2-3 (class e, MODEL). Claim 13 says cart Redis operations HGET/HMSET/EXPIRE lasted "120 to 1535 microseconds" (cites 8a2fb715).
- In that view the range is EXPIRE 234, HMSET 1152, HGET 310 to 1535, so the correct range is 234 to 1535.

P2-4 (visible-scope overclaim, MODEL). The summary says it "Surveyed all 17 authorized services ... using ... per-service trace searches".
- No traces_search was run for frontend-proxy, load-generator or product-catalog.
- image-provider and kafka returned no_data. This is disclosed in the gaps.
- Claims 19, 20 and 22 present those three services' spans as if from their own searches. The spans are actually taken from other services' traces (ad, cart, email, payment and others).
- Gap bullet 11 only says "services without a dedicated trace search" and does not name them. It does not say that the survey coverage for those three is incidental.
- These services were not refused; the model simply never searched them. It had 91 model requests left.

P2-5 (citation does not support the claim, MODEL). The claimed numbers and trace ids exist in other delivered views but not in the cited ones.
- Claim 15 (currency) lists trace c5a6d7d3... and "409 to 2157 us", citing 33f347f1. That view holds only trace d82cd... and 409 to 1108.
- Claim 23 (quote) lists c5a6... but cites 94ad324a (d82cd... only).
- Claim 25 (shipping) lists c5a6... and "6424 to 7299", citing 214a332d, which has 7299 only, with no c5a6... and no 6424.
- Claim 19 (frontend-proxy) lists trace ids d82cd, 752fb7aa and 43fff5ae with range "2413 to 72607", citing 8a2fb715. That view holds only 2 traces, of which only d82cd... is among the listed ids.
- Claim 20 (load-generator) lists traces 43ff... and 6157..., citing 2e4f5cbf, which contains neither.
- Claim 22 (product-catalog) lists 6157..., 43ff... and d82cd..., citing c8401c1a, which holds only 43ff... with 282 us.
- The facts are true, but a reader cannot verify them from the cited evidence_id. Cross-view sourcing should carry all the relevant ids.

P3
- Claims 9 and 28 and gap 10 read the 900 s-rate series as the error interval ("non-zero from 18:04:29Z to 18:18:14Z"; "the only fine-grained error timing").
  - rate[900s] stays non-zero for about 15 minutes after an increase, so the actual error events were brief and near 18:03-18:04Z.
  - The values are literally correct, but the wording overstates the episode length.
  - The EmptyCart series starts at 18:13:29Z, which is the series start and not a proven onset.
- Gap 3 says "Every per-service trace search that delivered spans is flagged incomplete".
  - The accounting view 05cd630c has incomplete=False (9 traces returned for limit 10), and its id is correctly not in the list.
  - The word "Every" is wrong, and the id list itself is right.
- Claim 27 ties gRPC status 9 = 3.30 to the EmptyCart operation.
  - The status-code view has no operation label, so this is inference. It is labelled as a hypothesis, so it is acceptable.
- The report calls the increase[86400s] values "calls" (for example "2.095 calls"). These are extrapolated Prometheus increases. This is a wording issue only.

A. RESULT_TOO_LARGE refusals
4 refusals in total and no other errors. There were 2 further non-ok results, both no_data.

| Step (round) | Tool | Arguments | view_tokens |
|---|---|---|---|
| 0 (r1) | traces_search | service=email, limit=50, 24 h window | 88680 |
| 2 (r3) | traces_search | service=cart, limit=10, 24 h window | 36184 |
| 2 (r3) | traces_search | service=checkout, limit=10, 24 h window | 88682 |
| 2 (r3) | traces_search | service=currency, limit=10, 24 h window | 88680 |

- Every refusal carries max_view_tokens=25000, status=error, citable_as_fact=false, content=None and no evidence_id.
- The other non-ok results were traces_search no_data for image-provider and kafka (step 4, limit 2, 0 traces).
- The 25 ok views include 2 that are no_data. The evidence table holds 31 rows (29 ok plus the 2 no_data).

B. Behaviour after refusal
- Each refused service subsequently obtained an ok view with a smaller limit, and none was retried unchanged.
  - email: limit 2 at step 3, 92 spans.
  - cart: limit 2 at step 3, 55 spans.
  - checkout: limit 1 at step 5, 46 spans.
  - currency: limit 1 at step 6, 46 spans.
- The model narrowed only by reducing limit and never shortened the 24 h window. It stated that traces "only return the newest samples regardless of window width".
- It tried limit 10 for cart, checkout and currency in the same round after the email limit-50 refusal, and 3 of those 3 were refused again. It then dropped to limits 1 and 2. This is a wasted round but not a repeat of an unchanged query.
- Refused data was not used as evidence. No refused view is cited, and claims cite only ok views.
- The report states the gap: "High-limit trace searches were refused as too large and returned no spans, so the request for at least 50 traces per service could not be satisfied". It also states the resulting sample sizes (mostly 1 or 2 traces, at most 10 for ad).
- It does not list which services were refused (email, cart, checkout, currency), but those services all ended with delivered trace evidence.
- Services with no delivered own-service trace evidence:
  - image-provider and kafka: no_data. This is not caused by refusals and is honestly reported as unknown.
  - frontend-proxy, load-generator and product-catalog: never searched, see P2-4.
  - No service lost its evidence because of a refusal.

C. Run resources
- Model requests: 9 (rounds 1-9). The model ended itself at round 8, with finish_reason=stop and planned_tools=0.
- Usage: prompt 756590 tokens and completion 55674 tokens (stats.json equals ledger usage_totals), with prompt_cache_hit 433280 and reasoning 27475.
- Report repair retry: yes, one (round 9), because the round 8 report had trailing junk and was not parseable.
- Wall time: model_seconds_used 203.6, and tool_seconds_used 0.38 over 35 tool operations.

D. Refusal message clarity
- The message reads: "The tool call result is too large to return: 88680/25000 tokens. Try to repeat the query but proactively narrow down the result (a narrower time window, a filter, or a smaller limit)". It also gives the numeric view_tokens and max_view_tokens.
- It was understood and acted on. The model's step 1 text says "the 24h/limit-50 trace search was refused as too large".
- Step 3 says "High-limit trace search is refused for dense services. Let me ... retry those services with a smaller sample".
- One observation: the message suggests narrowing the time window. In this run the window was not what changed the result, because the backend returns the newest traces. The model correctly worked this out from the source_start_at and source_end_at fields. This is a minor prompt-wording caveat, not a defect that hurt the Run.

Key file paths
- /Users/shenghuikevin/dev/AI/production-ops-agent-view-bytes/docs/evidence/m1-01-view-tokens-effect/broad-1/ledger.json
- /Users/shenghuikevin/dev/AI/production-ops-agent-view-bytes/docs/evidence/m1-01-view-tokens-effect/broad-1/report.json

