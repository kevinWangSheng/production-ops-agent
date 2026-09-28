# fault-2 独立审查（全新上下文 Agent，Opus；原文照录，未经执行者改动）

Reviewer inputs: v4 packet criteria, PRODUCT-CONSTRAINTS, ADR-0005，模型可见 REPORT_CONTRACT/RUN_COVERAGE_TEMPLATE 与工具描述，docs/tasks/2026-09-28-m1-01-alignment-c.md（C1-C3 背景），本目录，55431 只读 SQL。审查者未见执行者评估或其他审查者结论。本 Run 未触发 C2 重试。

**VERDICT: FAIL.** The report gets the root cause right, and every number and trace id I checked matches the stored evidence. It fails on one P2: a scope mix in claims 4 and 11 that makes payment Charge look partly successful. That P2 is borderline. If you rule it P3, the Run passes.

Scope: Run `bff4fc27`, fault-2, commit `ce6972e`. I made no edits. I checked everything against the stored views in `ledger.json` and did not need PostgreSQL.

**Run facts**
- The Run completed and published: 4 model requests, 9 tools, no handoff, prompt revision `prompt-replay-candidate-bd28790117a0`.
- It stopped on its own. Tool calls ran in rounds 1–3 and round 4 returned `stop` with the final report.
- The C2 repair retry did not trigger.
- All 9 queries used the window 17:45:16–17:50:16Z.
- Every metric view is a single instant point at 1790617816 (17:50:16Z), so each `increase[5m]` value covers exactly that window.

**Criteria 1–5**
1. **PASS.** The report parses as `assessment_status=completed`, `conclusion=partial`, with 7 gaps. Nothing is presented as complete that isn't.
2. **PASS.** The core conclusion rests on delivered evidence, and the root-cause statement is labelled a hypothesis with its limits spelled out.
3. **FAIL.** One scope mix, described under P2 below. Everything else here is correct:
   - The 8 trace ids are exactly the trace set in both trace views.
   - The report keeps "8 traces returned" separate from the limits of 30 and 20.
   - Shown and omitted spans are stated correctly (133/111 of 244 and 124/120 of 244).
   - `no_data` is described as an absence, not zero. The explicit `0` series for email, PlaceOrder code 0 and Charge code 0 do exist in the views.
   - The parent links hold: all 8 payment server Charge spans are children of the checkout client Charge spans (e.g. f39b005f→13e90872, c2127f0f→ba0aa7ba). `charge.js:37:13` appears in the raw rows.
4. **PASS.** Healthy dependencies are scoped to "in the sampled traces" and backed by a rejected hypothesis. Email is left unresolved. There is no recovery claim and no fix action.
5. **FAIL.** One P2 remains open.

**Findings**
- **P2(e), MODEL defect.** Claim 4 puts payment's service-level `STATUS_CODE_UNSET` 10.0 (from `47d94fd4-t0`) next to the Charge-specific `STATUS_CODE_ERROR` 8.33. Claim 11 (counter_evidence) then uses that 10.0, and checkout's UNSET 65.0, to argue "part of the observed span volume is non-error… not a complete failure rate."
  - The Run's own views contradict this reading. In `47d94fd4-t2`, grpc Charge UNSET is 0; the 10.0 is the internal `charge` span (8.75) plus flagd `ResolveFloat` (1.25).
  - `a9e09977-t1` shows PlaceOrder code 0 = 0. `7778d952-t2` shows client Charge code 0 = 0.
  - So the checkout and payment UNSET volume is child and internal spans, not successful Charge or PlaceOrder calls, and the counter-evidence is wrong.
  - observe-pre agrees: Charge UNSET 0.0 and PlaceOrder UNSET 0.0.
- **PRODUCT defects:** none. span_groups matches the rows exactly (next section).

**span_groups (C3)**
- (a) **Correct.** I recomputed span_groups from the raw shown rows of both trace views and they match field for field (rows, status keys, error_rows, min/max duration). Group row sums equal `spans_shown` (133 and 124), groups are sorted by (service, operation), and `span_groups_note` is present.
- (b) **Used.** The report's trace numbers follow span_groups: "8 rows… status {error=True; otel.status_code=ERROR; rpc.grpc.status_code=13}, durations 27017-57056 us", and the 22/14/8/8 row counts for Convert, GetProduct, GetCart and GetQuote.
- (c) **Used correctly.** All the duration ranges in claim 7 match. For payment server Charge the report took 8 rows (3361–22223 us) from the payment view, not the truncated 1-row group in the checkout view. The two `not_recorded` groups (`prepareOrderItems…`, payment `charge`) were never written up as a good status.

**Window contamination:** none found.
- The earliest shown span starts at 17:45:23.86Z, after the 17:45:17Z injection. The latest is 17:49:45Z, before submission at 17:50:16Z.
- All 8 trace ids are the same 8 that observe-pre independently found in its 17:45:11–17:50:11 window.
- No query reached back into the 24h frame, and no other fault episode was mixed in.

**Upstream-style verdict: root cause correctly identified.** The report pins the failure on payment Charge ("Invalid token", gRPC 2, surfaced as checkout gRPC 13 and HTTP 500). observe-pre independently shows `traces_with_checkout_error_and_failed_dependency_child: 8` and `failed_dependency_services: ["payment"]` over the same 8 trace ids.

VERDICT: FAIL
P1=0 P2=1 P3=0
P2 by class: a=0 b=0 b-svc=0 c=0 d=0 e=1
Window contamination: no contamination found
C3 span_groups: present and exactly matching a recomputation from the shown rows; the report quotes span_groups rows, status and duration min/max for its trace claims, all correctly; the one P2 comes from metric series, not span_groups
Upstream-style: root cause correct (payment Charge "Invalid token" failure, matches observe-pre's 8/8 traces with a failed payment child span)
