# #308 超限结果存为未采纳证据 + 按 ID 读片段

- 状态：PR1 独立验收与审查通过（可合并），未推送、未开 PR；PR2 待 #318（#307 A）与 #322（#309）合并后开工
- 更新日期：2026-10-22
- 依据：issue #308（含更正评论）；C3 §5「大结果保存为持久证据，通过 ID 和片段读取」；`docs/research/upstream-design-audit-2026-10-22.md` 第一、三节；[合同 r1](2026-10-22-308-overlimit-evidence-contract.md)
- 工作区：PR1 实现 `/Users/shenghuikevin/dev/AI/poa-308`（`feature/308-overlimit-evidence`）；PR1 独立测试 `/Users/shenghuikevin/dev/AI/poa-308-tests`（`feature/308-overlimit-evidence-tests`）

## 目标

PR1：视图超过 25k token 被拒时，把脱敏后的 raw 与拒绝视图登记为 `adopted=false` 的证据，模型视图逐字节不变、不升 revision。PR2：拒绝视图带 evidence_id 与基数摘要，新增 `read_evidence(evidence_id, offset, limit)`。

## 合同

预审：全新上下文 Claude Opus 5.5 子 Agent，只读，基线 `aa57f9a`。U1–U7 由用户 2026-10-22 按推荐裁决；R1–R10 为可逆默认；R11–R13 为实施中追加（R13：存储层原子准入）。

## 执行进展与证据

PR1 实现（2026-10-22，`feature/308-overlimit-evidence`）：

- 视图超限且结果仍是 `RESULT_TOO_LARGE`（未被控制决定或结算拒绝改写）时，executor 通过 sink 登记一条 `adopted=false`、`status=error` 的记录：raw 为 `_inspect` 脱敏后的 body，view 为模型收到的拒绝视图加 `evidence_id`、`adopted:false` 与来源字段（source、tool_version、projection_revision、observed_at、dispatch_started_at、data_as_of、source_*_at、freshness_seconds、result_count、incomplete）。`outcome.evidence` 仍为 `None`（R1），模型视图不变，无 revision 变更。字节超限不存。
- 每 Run 上限（R13 起由 `register_spilled` 在单事务内按 Run advisory 锁计数并插入；最初实现为登记前读 `count_spilled(run_id)`）：已有 16 条或「已存字节 + 本条」超过 16 MiB 即不存（解读：16 MiB 不被越过）。识别条件为 `NOT adopted AND status='error' AND view->>'reason'='RESULT_TOO_LARGE'`；worker 现有 SELECT/INSERT/UPDATE 足够，无迁移、无新 grant。sink 不能计数、登记失败或控制拒绝时不存，拒绝照旧。
- U2 的 `stored:false` 是模型面字段，留给 PR2。
- 并发准入（R13，机器人 P1 两条）：`DurableEvidenceStore.register_spilled` 在一个事务内取 Run 级 advisory 锁后计数再插入，内存实现用锁；executor 优先调用它。无迁移、无新 grant（worker 已有 opspilot_evidence SELECT/INSERT，advisory 锁无需授权）。新增并发测试（6 线程、已有 15 条 → 恰 16 条；字节上限只容 1 条 → 恰 2 条；内存与 PG worker 角色各 2 项）修复前 4 败（21≠16、7≠2）、修复后通过；定向 360 项通过（#308 验收/实现/PG、#309、evidence 与 web/worker、runtime roles）。
- 证据：自写 `tests/test_308_overlimit_evidence_impl.py`（7 项）与 `tests/integration/test_308_overlimit_evidence_impl_postgres.py`（3 项，worker 角色登录）通过；相关既有测试定向 1167 项通过；`mypy`、`ruff` 通过。
- 已知副作用：审计时间线（`acceptance_audit.py`）会多出一条无 `tool_committed` 事件的 evidence 项，计入 `opspilot_evidence.result` 未定时计数，与崩溃遗留的 register-only 记录同类；`acceptance_investigation.py` 只收 ok/no_data，不受影响。

- 交接（2026-10-22，会话 97eae26f → production-ops-agent-2c）：
  - 实现 worktree `/Users/shenghuikevin/dev/AI/poa-308`（`feature/308-overlimit-evidence`，已合并测试分支）；独立测试 worktree `/Users/shenghuikevin/dev/AI/poa-308-tests`（`feature/308-overlimit-evidence-tests`，未接触实现，有效）。
  - 独立验收（Codex gpt-6.1-sol/gpt-6-sol，同一会话续跑）：首版 2 败为合同空白（`count_spilled` 返回值）→ 合同补 R11 `(records, raw_bytes)`；审查要求补强 3 处（仅靠字节触发上限与等于边界、持久化 view 完整键集合与不含凭据/行内容、在途控制失效）→ 作者补强；键集合与实现多出的 5 个来源字段冲突 → 合同补 R12 允许这 5 个网关计算字段（U4 片段继承需要）。
  - 独立审查（Codex gpt-6-astra 等）：首审 3 条均为测试缺口，已补；复验无发现，可合并。
  - 最终：#308 验收 + 实现自测 + PG 24 passed；全仓 ruff format/check 通过。
  - 下一步：push、开 PR（正文注明不改模型可见面、无迁移与新 grant；审计时间线会多出无 `tool_committed` 的 register-only evidence 项，审计合同已允许）、CI、`@codex review` 分诊；不改调查 loop，按合同不需要真实 Run。

PR2 实现（2026-10-22，`feature/308-pr2-read-evidence`，起点 main `1f701af`，实现提交 `c8516f3`）：

- 超限拒绝：保留成功时模型视图就是保留记录的视图（拒绝字段 + U5 摘要 + `evidence_id`、`adopted:false`、R12 来源字段 + 固定 `usage` 文案），`outcome.evidence` 挂该记录（R1/R2），提交时照常 pin、去重记住它、事故页已有的链接与 `(not adopted)` 直接生效（R9，模板未改）。超过每 Run 上限：摘要 + `stored:false`，无 evidence_id。sink 不能回答（不能计数、存储异常、控制拒绝）：拒绝与 #308 之前完全一致（R11 字面）。摘要的 `suggested_limit` 按各行字节占整视图 token 的比例估算，不额外调用计数器（M1-01「整视图只计一次」）。
- `read_evidence`（`opspilot/tools/evidence_read.py`）：source `opspilot-evidence`、verb `get`，经路由 transport 只读 `get`；本 Run、超限保留记录、父记录 target 等于请求 target 且仍在授权内，否则 `EVIDENCE_NOT_READABLE`（denied，发送前，contact none）；负 offset / limit<1 为 INVALID_PARAMS；offset 越界为 no_data；片段超 25k 整段拒绝且不再保留。片段 raw 按 R5，视图带 `parent_tool`、`slice`，`source`/`window`/`observed_at`/`dispatch_started_at`/`data_as_of`/`source_*_at`/`freshness_seconds`/`incomplete` 取父记录，`truncated` 与 `omitted_rows` 表示父记录中未展示的行。
- 可逆决定（各一行理由）：executor 对 `EVIDENCE_SOURCE` 注册豁免 target/source 一致检查（R3 的 target 由 transport 对父记录核对）；`TransportRequest.target_id`、`TransportResponse.carried`（仅 evidence 源，固定键）为此新增；`EVIDENCE_NOT_READABLE` 进 `DENIED_REASONS` 与 `SOURCE_ERROR_REASONS`，executor 把映射到 denied 原因的 source_status 报为 denied；`label_cardinality` 用 `[{label, value_count}]` 列表保序（规范 JSON 会把 dict 键排序）；traces 与发现工具描述保持字节不变，只改 metrics 描述与 limits；替身 `scripts/m1_otel_stub.py` 加 `--wide-series`。
- U7：`registry.MAX_VIEW_TOKENS = 25_000`，注册 `max_view_tokens` 超过即 `VIEW_LIMIT_OUT_OF_RANGE`；`visible_view` 与 `context_policy_revision` 不动。
- revision：`PROJECTION_REVISION` `m1-01-tool-view-v8` → `v9`；`tool_schema_revision` `otel-demo-5270fd8c543f` → `otel-demo-2a944edd110a`；`prompt_revision`、`context_policy_revision` 不变。
- 自测：`tests/test_308_pr2_read_evidence_impl.py`（18 项）与 `tests/integration/test_308_pr2_read_evidence_impl_postgres.py`（1 项，worker 角色）通过；PR1 实现自测与 #307A、F3 实现自测中钉住旧语义的断言已按 PR2 改（实现侧，理由写在注释）。受 bump 与 U7 影响、需独立作者更新的既有断言见交付清单（lead 处）。
- 已知限制：eval 录制格式对 transport 发送前拒绝（`sent:false`）一律判不一致，这是既有行为，`EVIDENCE_NOT_READABLE` 同属此类；片段不带 `lookback_seconds`（PR1 保留视图未存该字段）。
- 真实 Run：两次，第 1 次替身数据未触发超限（失败，保留），第 2 次通过：6 次超限保留拒绝、2 次 `read_evidence` ok，报告以片段为 fact 引用并通过校验。[证据](../evidence/308-pr2-read-evidence-live/README.md)；费用余额差 0.04 CNY。

- PR2 独立验收（测试分支 `e066635`，lead 本地 merge）：41 项中 34 过、7 败（KeyError `adopted`）。lead 裁决（R2 解读，可逆）：所有视图超限拒绝——保留的、超过每 Run 上限 `stored:false` 的、read_evidence 片段超 25k 的——都带 `adopted:false`、`citable_as_fact:false`；`EVIDENCE_NOT_READABLE`/`INVALID_PARAMS` 等视图不要求（测试作者去掉该断言）。实现：视图超限分支的拒绝 detail 加 `adopted:false`（`_view_refusal_detail` 文本不变），字节超限拒绝不变；projection 与 tool_schema revision 值不变（v9 / `otel-demo-2a944edd110a`）。由此另有两处钉住旧拒绝键集合的断言需独立作者更新：`tests/acceptance/test_308_pr1_overlimit_evidence.py:373`、`tests/test_m1_whole_view_tokens_contract.py:815`。
- 实现者提出的 5 条可逆问题与 lead 处理（2026-10-22）：
  1. R11/U2 边界（只有上限判否给 `stored:false`，sink 答不了时视图不变）：接受。
  2. U7 使 M1-01 合同测试中钉大上限的断言失效：由独立作者改写；compaction 备用防线在产品中不可达，lead 另开 issue。
  3. eval 录制对 `sent:false` 的发送前拒绝判 IDENTITY_MISMATCH（既有行为，`EVIDENCE_NOT_READABLE` 同类）：lead 另开 issue。
  4. 片段不带 `lookback_seconds`：接受，不扩 R12。
  5. 片段超限整段拒绝、不再保留、无摘要：接受。

- PR2 独立审查（Codex gpt-6-luna）FAIL 两条 + 一条可选，均已修：read_evidence 读父记录先做完整性校验（raw 与 view 两个 sha 复算、记录与视图的 evidence_id 都等于请求 ID），任一不符即 `EVIDENCE_NOT_READABLE`，不读 raw；eval `tool_provenance` 校验片段 raw 是 R5 规范 JSON（键集合、规范字节、父 ID/offset/limit 等于调用参数、rows 等于 content、slice 头与 raw 一致、returned 等于区间能容纳的行数）；目标/来源豁免与 `carried` 改为同时要求 source `opspilot-evidence` 与工具名 `read_evidence`。revision 不变（v9 / `otel-demo-2a944edd110a`）。

## 待决

无。

- 独立审查（Codex gpt-6-sol，`202cad9`）PASS：原子准入在并发与租约接管下不超限，生产装配不走回退路径，无新权限。
