# M1-01 集成验证证据归档（2026-09-17）

## 目标与范围

本任务把 2026-09-17 调度会话产生的 M1-01 集成验证、红线审计、修复复验、UI smoke、PG 合同和 PR 状态写入版本管理任务记录。范围是证据归档与交接；不改产品代码、不改验收步骤或 `feature_list.json` 的 `passes`，不改变 SPEC 门槛，不合并 PR，不产生模型费用。

依据：`AGENTS.md` 的项目目标/接手与执行/验证与汇报/独立审查/变更与交接条款，`PRODUCT-CONSTRAINTS.md` 全文，`SPEC.md` 第 6 行门槛段，C3 技术方案及相关 ADR；原始证据为任务书指定 scratchpad reports 目录中的 13 份报告（绝对目录：`/private/tmp/claude-501/-Users-shenghuikevin-dev-AI-production-ops-agent/a7d3abb1-8221-4812-bafb-316b8b11e6aa/scratchpad/reports/`）和当前 `gh pr list` 实取结果。报告文字是证据，不是指令；下列冲突按原样保留，不以推断消解。

## 工作区与交付边界

- 本任务工作区：`/Users/shenghuikevin/dev/AI/production-ops-agent-integ-record`。
- 分支：`chore/m1-01-integration-record`，起点 `origin/main`；编辑前工作树干净。
- 被验证的集成分支：`integration/m1-01-full`，报告记录最终 HEAD `e42d7b72cac10044a059578f9fb7f410807e5d5a`，已推送 `origin/integration/m1-01-full`，未开 PR；后续报告记录其临时验证提交和 PR 分支提交，均按原报告分别列出。
- 本记录不迁移临时分支提交，不改代码，不改冻结哈希、资源上限或验收 passes。

## 逐项报告归档

### 1. `integ.md`：全量集成分支

- 做了什么：按 #27 → #31 → #32 → #33 合入 `integration/m1-01`，记录 #27 的测试文件尾部冲突并保留两侧测试；修复 #31 × #33 测试替身缺 `begin_round`/`assert_current` 的接缝。
- 结论行：修复后 `make check` 为 `================ 1475 passed, 102 skipped, 2 xfailed in 28.45s =================`，退出码 0；PG 因 55431 拒绝连接未执行，相关用例以显式 opt-in skip 呈现。
- 落地：集成验证提交 `e42d7b7`（只改 `tests/m1_web_support.py`）；集成分支无 PR。报告同时指出 #33 Web 追问/纠正文本当时未传入 #31 的 `payload` 权威通道，以及一项全量测试偶发 flake。
- 未完成/裁定：由 #33 owner 处理文本转发；`test_private_worker_invalid_body_has_no_export_or_config_read` 的归属需各 owner 在自身分支复现；报告建议 #27 → #31 → #32 → #33 的合并顺序。

### 2. `redline.md`：跨 PR 红线审计

- 做了什么：对 `integration/m1-01-full @ e42d7b7` 做静态审计并以内存 double 动态验证；专项测试 `45 passed, 2 xfailed`。
- 结论：7 项总表为：①只读边界 PASS（P3 `/openapi.json` 未认证可读）；②人工控制优先级 FAIL（P1，另两项 P2）；③业务记录恢复权威 PASS（但受 P2-2 影响）；④秘密与数据出口 PASS（两项 P3）；⑤认证边界 PASS（P3 `/openapi.json`）；⑥预算/deadline FAIL（P2 工具预算仅进程内）；⑦验收/证据真实性 FAIL（P2 真实 Run 的 `REPORT_INVALID` 原因和验收断言语义问题）。
- 落地/处置：P1-1、P2-1 后由 #33 `65843df`（集成复验 `af1f33f`）处理；P2-2 由 #34 `ade112a`（集成验证 `3b855fe`）处理；P2-3 由 #20 `28f0b2b`/`f4f30fe`（集成验证 `7c3ff53`）处理；P2-4 由 #32 `4da77f1`/`9626a9e`（集成修复 `47c4860`，集成报告 HEAD `2189e73`）处理。
- 未完成：P3 项及红线报告列出的产品组合缺口需后续 PR 处理；原报告时 Web 文本通道未接、工具预算未持久化、验收脚本仍固定 handoff，后续报告出现修复，见“冲突与时序”。

### 3. `loopfix.md`：真实 Run `REPORT_INVALID` 诊断

- 做了什么：确认两次 `REPORT_INVALID` 源于 PR #32 验收脚本传入缺 `mode`/`window` 的时间策略上下文；修复验收账本参数化与正向报告断言。
- 结论行：修复后真实 Run `2/2 execution=completed`，其中 1 次无 handoff 且经 `IncidentOutcome` 投影为 `report_available`；集成 `make check` `1483 passed, 102 skipped, 2 xfailed`，`make acceptance` `10/10 PASS + SECRET_SCAN_PASSED`。
- 落地：PR #32 最终 HEAD `9626a9e`（代码/证据至 `4e6cb84`）；CI run `35239671187` 与 docs-only HEAD 的 `35239936096` success，`CLEAN`/`MERGEABLE`，未合并。
- 未完成/裁定：`bbf10e0e` 是 fixture、单工具、2 请求、`partial` 的存在性证据，是否满足 M1-01 入口仍需用户裁定；另待裁定 F5：M1 的 `evidence_context-v4` 标签与 TimePolicy 必填字段合同关系；F4：是否收敛 `context_time_policy_ids` 与 `eligible_time_policies` 双标准。F8 保留一次 Run 存在性证明的限制；F9 来源行缺失已由 `4e6cb84`/`2189e73` 修复，不列为未完成。

### 4. `ui-e2e.md`：UI 端到端 smoke

- 做了什么：专属 PG + web + worker 运行 7 条路径，验证认证、SSE 游标、证据 hash、人工控制、重启恢复和 `IncidentOutcome` 投影；清理 web/worker/PG 进程。
- 结论：7 条路径均 PASS；#33 分支 `make check` `1467 passed, 97 skipped, 2 xfailed`；集成分支 web 单测 `23 passed`、web PG `2 passed`。真实 Run 1 次为 `failed / REPORT_INVALID`，费用上界 `0.024175 CNY`，报告将其归因为缺 `mode/window` 的 harness 上下文，未再重跑。
- 落地：#33 `61f270f`、`f2aecb7`、`65843df`、`e958c5a`、`9bbca15`；集成复验对应 `e42d7b7`、`8936ccb`、`af1f33f`、`e22e8ad`；CI runs `35239844780`、`35240005464` success，PR #33 `CLEAN`/`MERGEABLE`。
- 未完成/裁定：该 smoke 的重启路径没有调用 #30 `Worker.resume/RecoverySession`；报告把 `budget_spent` 未结算、#30 × #33 恢复组合、暂停语义、pending_tools 读取、helper 吸收列为交给其它 PR/用户决定事项；后续 storefix2/lease 报告有部分处置。

### 5. `storefix.md`：DurableStore 红线与预算

- 做了什么：修复红线 P2-2 `claim()` 状态守卫顺序；为工具次数/秒数引入 PG ledger；随后修复 `rebuild()["pending_tools"]` 的 loop 形状读取和模型预算 `reserved → spent/unknown` 结算。
- 结论行：集成复验 `make check` `1483 passed, 112 skipped, 2 xfailed`，PG `58 passed, 54 skipped`；PR #20/29/30/33 的分支复验分别记录在原报告第 4/追加 A/B 节。
- 落地：P2-2 交付 PR #34，最终记录 head 含 `e25c5a5`；P2-3 交付 PR #20 head `f4f30fe`；pending_tools 落到 PR #30 `4273a38`/`bd47a9f`；budget settle 落到 PR #29 `b133cc0`/`814dd2d` 与 #33 `eec8dca`。集成验证提交 `2c43896`、`c92feca`、`845522a`、`248e4a3`、`81f8f15` 未推送。
- 未完成/裁定：真实产品组合尚未在报告时构造 `DurableToolLedger`；#20 新 HEAD 未重新触发机器人复审；#34/#26 与 #20/#26 有合并顺序冲突；跨 epoch 重派发是否再次计次、NULL 租约口径和 PR 合并由用户裁定。报告还保留 54 个 M0 专属 PG skip。追加 A/B 还要求用户裁定：pending_tools 修复只落 #30 而非另建 main fix 分支的归属判断，以及次数级结算是否属于 M1-01；`operation_id` 的 `step:ordinal` 与 `step-t{index}` 格式尚需组合层统一。

### 6. `contract.md`：PR #27 收尾

- 做了什么：完成 instruction contract 的四轮独立审查、机器人 review thread 处置、回归测试和文档收尾。
- 结论行：PR #27 HEAD `cf8f17d817e9baef2ff6c82b0fdb6679d628fa6f`；`make check` `1098 passed, 77 skipped, 2 xfailed in 27.87s`；`checks`/`m0-postgres` success，`CLEAN`/`MERGEABLE`，4 threads 全部 resolve。
- 落地：`dd20dcb` 修复一次性 `authorized_services` 迭代器，`282331e` 扩大全 segment revision 投影，`1856f85` 加无预算槽位护栏，`cf8f17d` 修正文档矛盾；PR #27 未合并。
- 未完成/裁定：ToolRegistration 五字段依赖 #20；C3 第 7 节部分检查、ModelProfile 接线、Holmes 端到端 hash、PG 本轮未重跑；`prompt_revision` 12 位短码、template projection 是否包含元数据、来源索引归位有三项用户裁定。

### 7. `lease.md`：PR #35 租约续期 API

- 做了什么：新增 `DurableStore.renew_lease()`，同一事务检查 running、owner/epoch、事故代际、撤销、过期和 deadline，并以 `LEAST(GREATEST(...), deadline)` 延长租约。
- 结论行：`make check` `1050 passed, 92 skipped, 2 xfailed in 29.71s`；PG 定向 `38 passed in 13.04s`；CI 对最终 docs HEAD `a85b458` 的 checks/m0-postgres pass，`CLEAN`/`MERGEABLE`。
- 落地：代码 `91451d9`，任务/ROADMAP `96deefa`，CI 补记 `a85b458`；PR #35 未合并。
- 未完成/裁定：#33 与 #30 尚需接线（后续 lease-wire 报告已做）；真实模型请求中无法心跳续租，420 秒推导对工具恢复路径偏保守，留后续复核；无用户裁定项。

### 8. `lease-wire-33.md`：#33 租约续期接线

- 做了什么：在 #33 增加可选 `renew_lease` 能力、420 秒短租约、每个 committer 调用前续租、无能力回退到 Run wall，并补内存/PG 测试。
- 结论行：`make check` `1471 passed, 99 skipped, 2 xfailed`；临时合并 #35 后全部 integration `78 passed, 38 skipped`；最终 CI run `35255509110` success，PR #33 `CLEAN`/`MERGEABLE`。
- 落地：`8d3277d`、`4609aa6`、`f6decd6`（均普通 push）。
- 未完成：必须先合 #35 才会在生产存储上生效；#29 client 的 per-socket timeout 可能超过 420 秒；需在 #35 合并后重跑默认租约 smoke。

### 9. `lease-wire-30.md`：#30 RecoverySession 接线

- 做了什么：在 `RecoverySession.execute_pending` 的 execute→renew→commit 间接入可选续租；统一默认 `DEFAULT_LEASE_SECONDS=420`；修复 falsey 畸形 `tool_calls` fail-closed。
- 结论行：`make check` 为 `1060 passed, 93 skipped, 2 xfailed`；PG 定向 `37 passed`；CI run `35265574868` checks/m0-postgres success，`CLEAN`/`MERGEABLE`。
- 落地：`9d23b9f`、`711c67d`、`1d55e9f`、`384d62c`、`a8c28e5`，PR #30 未合并。
- 未完成：#35 必须先合并才真正启用；第二次机器人 review 在报告完成时未返回；无用户裁定项。

### 10. `flake.md`：PG live probe 偶发超时

- 做了什么：将 PG/private transport 顶层 import 延迟到请求体校验之后，避免高并发 import 阶段超过 5 秒测试窗口；3 次红态复现，修复后循环验证。
- 结论行：连续 4 次 `36 passed, 1 skipped`；`make check` 两次均 `1050 passed, 75 skipped, 2 xfailed`；独立审查 APPROVE，0 待处理发现。
- 落地：PR #36 HEAD `080c62c`，checks/m0-postgres SUCCESS，`CLEAN`/`MERGEABLE`。
- 未完成：无；合并授权及合并后清理由用户按默认流程处理，未合并。

### 11. `registry.md`：ToolRegistration 结构与确定性检查

- 做了什么：为 `ToolRegistration.description` 建立五字段字符串结构，完成 C3 第 7 节第 2、3 项；保持既有测试和公共面。
- 结论行：`make check` 结论 `1233 passed, 79 skipped, 2 xfailed`；定向 `98 passed`；PR #20 当前报告 head `aff4586`，checks/m0-postgres pass。
- 落地：`0e9c45c`（代码/测试）和 `aff4586`（任务记录）均已推送，PR #20 描述已更新，未合并；独立审查无阻塞发现。
- 未完成/裁定：C3 第 7 节其他项和 `tool_specific` 迁移待 #27 合并后承接；来源索引归位冲突仍待裁定；无验收或门槛修改。

### 12. `revision.md`：prompt_revision 与白名单投影

- 做了什么：将 `prompt_revision` 计算统一经 `prompt_revision_versions()`，并对 `evidence_context` 做冻结 v4 schema 白名单投影；先红后绿并经全新上下文审查。
- 结论行：`make check` `1299 passed, 83 skipped, 2 xfailed`；CI run `35255856858` checks/m0-postgres pass；PR #29 `CLEAN`/`MERGEABLE`。
- 落地：`519c108`、`4ed021f`、`692cf3e`、`4c406b0`，PR #29 未合并。
- 未完成/裁定：本分支的 prompt_revision 与 #27 当前 HEAD 计算值不同，需 #27 → #29 顺序；tool_schema_revision、完整 ModelProfile 和真实 accept/claim 接线范围外；4 条既有机器人发现超出本轮范围且未修；不改 passes/门槛。

### 13. `loop-followup.md`：机器人发现与 wall clamp

- 做了什么：处置 4 条既有机器人发现并加入模型请求 wall clamp；3 条完整修复、1 条因缺少 source 区间字段部分修复；独立审查发现首版 socket 内层阻塞缺口后补紧 socket deadline。
- 结论行：最终 `make check` `1314 passed, 84 skipped, 2 xfailed`；PG `30 passed, 54 skipped`；CI run `35262241065` checks/m0-postgres pass；PR #29 未合并。
- 落地：`82218ae`、`23c7158`、`faf80bc`、`871e628`、`ff8e17b`、`8b65c13`，文档收尾 `26d0c70`。
- 未完成/裁定：完整 source 区间校验需 #20 先增加 `source_start_at/source_end_at`；机器人审查以外的其余边界保持记录；不改 passes/验收/门槛。

## 跨 PR 红线发现与处置索引

| 红线审计条目 | 原始结论 | P1/P2 处置去向 |
|---|---|---|
| 只读边界 | PASS；P3 `/openapi.json` 可读 | #33 `f2aecb7` → `openapi_url=None` |
| 人工控制优先级 | FAIL；P1-1 claim 前读取备注；P2-1 文本双通道；P2-2 版本检查覆盖人工终态 | P1-1/P2-1 #33 `65843df`/集成 `af1f33f`；P2-2 #34 `ade112a`/集成 `3b855fe` |
| 业务记录恢复权威 | PASS，但受 P2-2 影响 | 随 #34 状态守卫修复；恢复链 pending_tools 另由 #30 `4273a38` 承接 |
| 秘密与数据出口 | PASS；P3 顶层键过滤/源数据秘密注册限制 | 记录为后续边界；#29 白名单投影 `4ed021f` 收紧 evidence_context |
| 认证边界 | PASS；P3 `/openapi.json` | #33 `f2aecb7` |
| 预算与 deadline | FAIL；P2-3 工具计数/秒数不跨 attempt | #20 `28f0b2b`/`f4f30fe`，集成 `7c3ff53`；预算 spent/unknown 由 #29/#33 的 `settle_budget` 承接 |
| 验收与证据真实性 | FAIL；P2-4 REPORT_INVALID 根因未记录且 handoff 断言方向错误 | #32 `4da77f1`/`9626a9e`，集成 `47c4860`/`2189e73` |

## 报告间冲突与时序

1. `integ.md`/`redline.md` 以 `e42d7b7` 为审计点，记录 Web 文本未接、P1-1 未修；`ui-e2e.md` 的后续 #33 提交 `65843df`/`af1f33f` 已验证 `opspilot_controls.payload`、`opspilot_inputs` 与模型消息，且 P1-1 已修。两者是不同提交时点，均保留。
2. `ui-e2e.md` 仍记录 `rebuild()` 读取顶层 `tool_calls` 和 `budget_spent=0`；`storefix.md` 追加 A/B 记录后续已把 pending_tools 读取修到 #30，并把预算结算拆到 #29/#33。后者是后续证据，不改写前者。
3. `lease.md` 的接线建议后，`lease-wire-33.md` 与 `lease-wire-30.md` 已分别落地可选续租；两份报告均明确 #35 未合并前接线不会在生产 DurableStore 上生效。
4. `storefix.md` 报告 PR #20 head `f4f30fe`，后续 `registry.md` 报告 PR #20 head `aff4586`；本记录采用各报告写作时的原始值，当前 PR 表以 `gh pr list` 实取为准。
5. `integ.md` 记录 PG 未执行；各专项报告后续在独立 lab 实际执行 PG，并分别记录 `stop`。不能把专项 PG 结果改写成 `e42d7b7` 全量集成分支已跑 PG。

6. `storefix.md` 的「#20 未复审」是该报告时的历史状态；`registry.md` 后续明确记录 `aff4586a5d` 的 `@codex review` 返回「Didn't find any major issues」，无新增 inline thread。这不代表其它 PR 的待审已结束。
7. `lease-wire-30.md` 将「机器人 Code Review 不是交付门槛」归因于 AGENTS.md，与当前仓库仅豁免 security review 的文字不符；普通 review 在其报告时仍未返回。本任务书明确另行指定「机器人审查不是门槛」，只作为本记录任务的交付约束，不追溯更改其它 PR 的门槛或把待审记为完成。
8. `integ.md` 称 Web 文本「永远收不到」，`redline.md` 明确发现 `_notes()` 从 web ledger 拼进 question 的旧路径；两者对同一审计点的描述不一致。能共同确认的是没有进入 #31 权威输入通道；不据此断言旧实现完全没有向模型传文本。
9. `revision.md` 的四条未修发现由 `loop-followup.md` 后续承接：三条完整修复，一条仅修负值/未来时间戳，完整 source 区间仍未修；不能把全部 resolve 等同全部缺陷已修。

## 当前 13 个 open PR（建本记录 PR 前的 GitHub 快照）

实取命令：`gh pr list --state open --limit 100 --json number,title,headRefName,baseRefName,state,statusCheckRollup,reviewDecision,updatedAt,url`，另以 `--json number,headRefOid,baseRefName,mergeStateStatus,mergeable,state` 补齐 HEAD 与可合并状态。空 rollup 不等于 CI 失败，也不等于已验证当前 HEAD。

| PR | 标题 | head/base | CI 实况 | merge/review 状态 |
|---:|---|---|---|---|
| #36 | fix: defer PG private-transport imports past request-body validation | `fix/pg-live-probe-flake` → `main` | `checks` SUCCESS；`m0-postgres` SUCCESS | OPEN；`CLEAN/MERGEABLE`；HEAD `080c62cc417965624032bebc32bbabbac476d75d`；reviewDecision 空 |
| #35 | fix: add DurableStore.renew_lease with the write-path fence and deadline cap | `fix/lease-renewal` → `main` | `checks` SUCCESS；`m0-postgres` SUCCESS | OPEN；`CLEAN/MERGEABLE`；HEAD `a85b458355d7a99fb4b302acb391c073f8066997`；reviewDecision 空 |
| #34 | fix: let human control outrank the version fence in DurableStore.claim | `fix/claim-state-guard` → `main` | `checks` SUCCESS；`m0-postgres` SUCCESS | OPEN；`CLEAN/MERGEABLE`；HEAD `f67446e28892188866de0a318f2e2a901cd0b350`；reviewDecision 空 |
| #33 | feat: add the Jinja/SSE workbench with authenticated intake, human control and evidence read-back [#F12] | `feature/m1-01-progress-ui` → `integration/m1-01` | statusCheckRollup 为空 | OPEN；`CLEAN/MERGEABLE`；HEAD `f6decd6ca6f0a8c40c54056d1bed78d5580abc94`；reviewDecision 空 |
| #32 | feat: add M1-01 external acceptance seam | `feature/m1-01-acceptance` → `integration/m1-01` | statusCheckRollup 为空 | OPEN；`CLEAN/MERGEABLE`；HEAD `9626a9e003854541b60e0f6997c9d28b863b48b8`；reviewDecision 空 |
| #31 | feat: complete durable human control [M1-01] | `feature/m1-01-control-completion` → `integration/m1-01` | statusCheckRollup 为空 | OPEN；`CLEAN/MERGEABLE`；HEAD `865eebfa01eddf7d7595d0aa9053e54efa53d3ac`；reviewDecision 空 |
| #30 | feat: add worker restart recovery coordination | `feature/m1-01-restart-recovery` → `chore/durable-store-hardening` | statusCheckRollup 为空 | OPEN；`CLEAN/MERGEABLE`；HEAD `a8c28e5c54d977aeda37bed47de094c2334014d3`；reviewDecision 空 |
| #29 | feat: add the Flash investigation loop [#F3] | `feature/m1-01-investigation-loop` → `feature/m1-01-tool-executor` | statusCheckRollup 为空 | OPEN；`CLEAN/MERGEABLE`；HEAD `26d0c70cd29e8a2cda64a50e4b9483f46e214966`；reviewDecision 空 |
| #28 | feat: implement M1-01 human control semantics | `feature/m1-01-human-control` → `chore/durable-store-hardening` | statusCheckRollup 为空 | OPEN；`CLEAN/MERGEABLE`；HEAD `49a51755a9baa7e3424e5ca8047c17c847727171`；reviewDecision 空 |
| #27 | refactor: converge the investigation discipline into a single versioned source | `chore/instruction-contract-impl` → `main` | `checks` SUCCESS；`m0-postgres` SUCCESS | OPEN；`CLEAN/MERGEABLE`；HEAD `cf8f17d817e9baef2ff6c82b0fdb6679d628fa6f`；reviewDecision 空 |
| #26 | fix: harden DurableStore SQL and converge the lease fence (A 类确定性缺陷) | `chore/durable-store-hardening` → `main` | `checks` SUCCESS；`m0-postgres` SUCCESS | OPEN；`CLEAN/MERGEABLE`；HEAD `8ff588068887d7560c41a7b7245fb54fbc52ebe6`；reviewDecision 空 |
| #21 | feat: add authenticated intake contracts [#F12] | `feature/m1-01-intake-auth` → `main` | `checks` SUCCESS；`m0-postgres` SUCCESS | OPEN；`CLEAN/MERGEABLE`；HEAD `5e581ee6d2447a469df4de736e7612692cf24736`；reviewDecision 空 |
| #20 | feat: add read-only tool executor boundaries [#F7] | `feature/m1-01-tool-executor` → `main` | `checks` SUCCESS；`m0-postgres` SUCCESS | OPEN；`CLEAN/MERGEABLE`；HEAD `aff4586a5d67432bffecf165a9d9c395a80b0171`；reviewDecision 空 |

## 当前事实、未完成项与下一步

已核实：本轮所有 PR 仍 OPEN；#20、#26、#27、#34、#35、#36 的当前 `gh pr list` 检查均为 SUCCESS，#29–#33 的 base 为集成/非 main 分支且当前 `gh pr list` 没有 statusCheckRollup；专项报告中多项分支曾有成功 workflow_dispatch 和独立审查，但不能替代当前 HEAD 的 GitHub 状态。未核实为已合并、已部署或产品验收通过。

未完成：用户审核/合并；#35 → #33/#30 的续租依赖；#20 工具 ledger 的真实产品组合接入；#29/#30/#33 的合并顺序与 pending_tools/预算接线；#20/#29 的 source 区间字段与完整 freshness 校验依赖；报告列出的用户裁定项；M1-01 全部验收步骤与 11 个 feature `passes`。

下一步：按各 PR 报告的依赖顺序进行用户审查；合并前对受影响的当前 HEAD 重新跑 CI/独立审查并逐条处置 review threads；合并后补记录 main CI、同步和 worktree 清理。当前任务本身只交付证据归档，不打开更宽实施门槛。

## 当前状态声明

截至 2026-09-17，本任务未合并任何 PR、未改 `feature_list.json`/passes、未改变 SPEC 门槛、未新增产品权限、未产生模型费用。M1-01 仍是 SPEC 所述的有界开放；M0 未被宣布通过。

## 本任务验证记录

- 首次 `make check`：因新 worktree 缺少 Python 3.12/开发工具，`doctor.py` 返回 `缺失/不可用 | 项目 Python 3.12 与开发工具 | Python []; tools={}`，`make: *** [doctor] Error 1`；这是环境前提失败，未产生代码失败结论。
- 按开发指南执行 `make setup`，使用 CPython 3.12.13 和锁定依赖创建 `.venv`；安装文件为 ignored 环境工件。
- setup 后 `make check` 原样结论：`uv lock --check --offline --no-python-downloads` 通过；`ruff check` `All checks passed!`；`ruff format --check` `411 files already formatted`；`mypy` `Success: no issues found in 13 source files`；pytest `================= 1050 passed, 75 skipped, 2 xfailed in 37.23s =================`，退出码 0。
- 本任务只改本文档与 ROADMAP；未启动 PG lab，因为任务书要求的本次 `make check` 已完成且文档变更不需要 PG。未执行模型/trace/外部付费调用。
- 独立审查：全新上下文只读 Agent `/root/review_integration_record` 逐份比对原报告，并独立查询 GitHub。发现的 loopfix F4/F5 裁定遗漏、F9 已修状态、storefix A/B 裁定遗漏、#20 后续复审和普通机器人审查门槛归因冲突全部采纳补齐；复核结论 APPROVE，无剩余阻塞发现。审查未重复运行 make check，结论只覆盖本次两份文档，不代表产品验收。

`integ-final.md` 在本轮首次读取报告目录时尚不存在；不据此声称所有最新 PR 已汇合集成通过。

本任务的文档完整性确定性检查：空记录负向输入输出 `RED: empty/missing record rejected`；现有记录输出 `GREEN: 13 report references, 13 PR rows, verification result and ROADMAP boundary present; tracked code unchanged`。只检查文档覆盖与改动边界，不把环境缺失称为代码回归红态，也不新增产品测试。

## 本任务 PR 交付状态

- 提交：`7c99f60`，分支 `chore/m1-01-integration-record`，已普通 push；PR #37（`docs: record M1-01 integration verification evidence`）已创建，目标 `main`，未合并。
- PR #37 初始 HEAD 的 GitHub Actions run `35268318674`：`checks` pass（53s），`m0-postgres` pass（41s）。这些检查覆盖记录类变更；不改变前述专项报告的产品状态。
- PR #37 当前仍需用户审核/合并；本任务不执行合并。后续实质变更若发生，需重新核对当前 HEAD 的 CI 和审查覆盖。

### PR #37 最终 HEAD 补记

- 为把 PR 检查结果写入仓库记录，追加文档提交 `d0e7d13` 并普通 push；因此 PR #37 当前 HEAD 已从 `7c99f60` 更新为 `d0e7d13`。
- 当前 HEAD 对应的 GitHub Actions run `35268443863`：`checks` pass（52s），`m0-postgres` pass（34s）。PR 仍 OPEN，未合并，等待用户审核。
