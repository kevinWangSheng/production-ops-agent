# PR16 控制代次时间校验修复

GitHub bot P1 3979613299，基线61850c1。按用户要求直接使用bot复审，没有重新调用本地eng:code-review或独立review Agent。原两条重标签反例先失败后修复；最终隔离代码全量636 passed/44 PG默认skip（21.16s），ruff/format通过。应用时核对基线和三个文件SHA，随后提交最新CI/Code/Security；不自动合并。

控制代次约束实际发送：本代事件下界、下一代事件的发送上界；不裁掉旧请求的迟到响应历史。gen0不补造开始时间，未发送prepared可缺时间，但不存在的代次仍unknown。原fixture缺钟/倒序另保负例，只把应为合法正例的合成数据改为一致时间；未改真实历史证据。数据库、schema、legacy及服务不变，0真实模型/trace/PG。

以下保留实现者的版本和命令记录，不标为独立审查；其中v1全量与v2定向均按版本区别，最终v2全量结果以上述root日志为准。

# Bot P1 3979613299 — strict control-generation chronology

Base: `61850c163b9f7e42551b24978ec61afb905285dc`.
Readonly original: `/Users/shenghuikevin/dev/AI/production-ops-agent-m0-01`.
Isolated tracked-code archive: `/tmp/m0-bot-control-time-code`.
No original files/index modified, no ignored .env/database/private run files copied, no schema or PG changes, model/network/container/service calls, commits or local review agents. Existing interpreter only; root owns applying and GitHub review.

## First frozen patch (preserved as v1)

`/tmp/m0-bot-control-time-fix-v1.patch`
SHA256 `214d4fcc69e8bcda67919aaa05c82a736a1defcc69f6f10371517526ce233820`

Changed file SHA256:

- scripts/m0/outcomes_v4.py: `d3fabd7d17f2d7f10fdc18ce7fafb3f1d9d8f4fce3f4cc06d4efae568e3400df`
- tests/test_m0_outcomes_v4.py: `804944f861c16ad6e87f00c7796f0e8dfeece86081c477f9d643b41de7891089`
- tests/test_m0_holmes_bridge_v4.py: `c0b10174378409f772363ba19ab5f208d41738ccfaacb853756ad142170f9bdd`

## Red evidence

`/tmp/m0-bot-control-time-red.txt`: 2 failed, 55 deselected in0.16s. Both cancel and correct followed by new_run at00:03:05 falsely accepted a previously initiated00:03:00 dispatch relabeled as generation2, with response00:03:10. Original checker returned[] in each case.

## Bounded change

A report-independent strict v4 chronology helper checks existing trusted control events and delivery/capture timestamps before any reportless return:

- Control event times must be nondecreasing in generation order; no fabricated event timestamps.
- A known actual dispatch in generation>=1 cannot precede that generation's event; generation0 has no invented intake start.
- Actual dispatch must precede the next generation event, including the generation0→1 boundary. Equality at the next event is not prior-generation authority.
- cancel/correct generations do not themselves grant permission to initiate another request; a new_run transition is required.
- Missing dispatch time for claimed dispatched/committed records, missing receipt for committed response, and missing capture time remain explicit UNKNOWN. A prepared record with no dispatch/receipt claim is still a legitimate unsent handoff.
- Known response/capture receipt cannot precede its own claimed generation event. There is **no next-generation upper bound on receipt/capture**, so late old responses remain auditable history. Existing current-run/generation/output-binding checks prevent adopting old captures as current final output.

The rule checks operation/control chronology only, not historical telemetry event or query windows; old evidence may legitimately predate the new Run. Existing PG audit timestamps already come from clock_timestamp after the subject lock (root-verified); no database change is included. Legacy v3 behavior, historical artifacts and schema snapshots are untouched.

## Verification

Commands in `/tmp/m0-bot-control-time-code`:

```sh
/Users/shenghuikevin/dev/AI/production-ops-agent/.venv/bin/python -m pytest tests/test_m0_outcomes_v4.py -k relabelled_dispatch -q --tb=short
/Users/shenghuikevin/dev/AI/production-ops-agent/.venv/bin/python -m pytest tests/test_m0_outcomes.py tests/test_m0_outcomes_v3.py tests/test_m0_outcomes_v4.py tests/test_m0_holmes_bridge.py tests/test_m0_holmes_bridge_v4.py tests/test_m0_initial_evidence.py -q --tb=short
/Users/shenghuikevin/dev/AI/production-ops-agent/.venv/bin/ruff check scripts/m0/outcomes_v4.py tests/test_m0_outcomes_v4.py tests/test_m0_holmes_bridge_v4.py
/Users/shenghuikevin/dev/AI/production-ops-agent/.venv/bin/ruff format --check scripts/m0/outcomes_v4.py tests/test_m0_outcomes_v4.py tests/test_m0_holmes_bridge_v4.py
```

Final `/tmp/m0-bot-control-time-green.txt`: **317 passed in5.48s**. Ruff and format checks pass.
Coverage includes original two relabeling negatives, valid exact lower boundary, next-event dispatch before/equal/after, missing times, control time reversal, generation0, prepared-unsent handoff, cancel/correct authority, late old response history and old capture rejection without receipt-time clipping.

Intermediate `/tmp/m0-bot-control-time-initial-green.txt` exposed three inconsistent old synthetic positive fixtures: a committed response without receipt time and cancel/correct events later than a subsequent new_run. Only intended-valid synthetic fixtures were given consistent recorded times; explicit negative regressions preserve the original missing-clock/reversed-order shapes as UNKNOWN/error. No real historical source timestamps or acceptance requirement was rewritten.

This is implementer self-test, not independent certification. Per user instruction, no local eng:code-review or other review agent was invoked. Root will run the full suite, approve original-worktree application and close the GitHub bot review.

## Final v2 after root's bounded review

Root found that the prepared-unsent exemption skipped generation existence as well as time checks. The generation lookup now precedes the exemption: an unsent known generation (including zero) still requires no fabricated clock, but future/nonexistent generation999 is CONTROL_GENERATION_UNKNOWN. One regression preserves this distinction; no other mechanism changed.

Current `/tmp/m0-bot-control-time-fix.patch` SHA256: `4a088c7dbdfa2d6dd439250841bc510521f627490079af54c20c7f9a5c2ba008`.

Final file hashes:
- outcomes_v4.py: `9d6b2efbcf7f2de9cc44dd2383e0dfc8bf0812d523c1a2b8aadb404be05b9fe2`
- tests/test_m0_outcomes_v4.py: `ea96fe90b7bef64c9da99412f0ad727e036460b21a663a9dd642619a444b0552`
- tests/test_m0_holmes_bridge_v4.py unchanged: `c0b10174378409f772363ba19ab5f208d41738ccfaacb853756ad142170f9bdd`

Final affected v4 module regression:76 passed in0.15s, log `/tmp/m0-bot-control-time-v2-targeted.txt`; ruff passes. The earlier317 suite is evidence for v1, not claimed as a fresh full run of v2; root is running the final full suite. Original worktree remains untouched and no review agent was invoked.
