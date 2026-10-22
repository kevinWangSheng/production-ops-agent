# #308 超限结果存为未采纳证据 + 按 ID 读片段

- 状态：PR1 独立验收与审查通过（可合并），未推送、未开 PR；PR2 待 #318（#307 A）与 #322（#309）合并后开工
- 更新日期：2026-10-22
- 依据：issue #308（含更正评论）；C3 §5「大结果保存为持久证据，通过 ID 和片段读取」；`docs/research/upstream-design-audit-2026-10-22.md` 第一、三节；[合同 r1](2026-10-22-308-overlimit-evidence-contract.md)
- 工作区：PR1 实现 `/Users/shenghuikevin/dev/AI/poa-308`（`feature/308-overlimit-evidence`）；PR1 独立测试 `/Users/shenghuikevin/dev/AI/poa-308-tests`（`feature/308-overlimit-evidence-tests`）

## 目标

PR1：视图超过 25k token 被拒时，把脱敏后的 raw 与拒绝视图登记为 `adopted=false` 的证据，模型视图逐字节不变、不升 revision。PR2：拒绝视图带 evidence_id 与基数摘要，新增 `read_evidence(evidence_id, offset, limit)`。

## 合同

预审：全新上下文 Claude Opus 5.5 子 Agent，只读，基线 `aa57f9a`。U1–U7 由用户 2026-10-22 按推荐裁决；R1–R10 为可逆默认。

## 执行进展与证据

PR1 实现（2026-10-22，`feature/308-overlimit-evidence`）：

- 视图超限且结果仍是 `RESULT_TOO_LARGE`（未被控制决定或结算拒绝改写）时，executor 通过 sink 登记一条 `adopted=false`、`status=error` 的记录：raw 为 `_inspect` 脱敏后的 body，view 为模型收到的拒绝视图加 `evidence_id`、`adopted:false` 与来源字段（source、tool_version、projection_revision、observed_at、dispatch_started_at、data_as_of、source_*_at、freshness_seconds、result_count、incomplete）。`outcome.evidence` 仍为 `None`（R1），模型视图不变，无 revision 变更。字节超限不存。
- 每 Run 上限：登记前读 `count_spilled(run_id)`，已有 16 条或「已存字节 + 本条」超过 16 MiB 即不存（解读：16 MiB 不被越过）。识别条件为 `NOT adopted AND status='error' AND view->>'reason'='RESULT_TOO_LARGE'`；worker 现有 SELECT/INSERT/UPDATE 足够，无迁移、无新 grant。sink 不能计数、登记失败或控制拒绝时不存，拒绝照旧。
- U2 的 `stored:false` 是模型面字段，留给 PR2。
- 证据：自写 `tests/test_308_overlimit_evidence_impl.py`（7 项）与 `tests/integration/test_308_overlimit_evidence_impl_postgres.py`（3 项，worker 角色登录）通过；相关既有测试定向 1167 项通过；`mypy`、`ruff` 通过。
- 已知副作用：审计时间线（`acceptance_audit.py`）会多出一条无 `tool_committed` 事件的 evidence 项，计入 `opspilot_evidence.result` 未定时计数，与崩溃遗留的 register-only 记录同类；`acceptance_investigation.py` 只收 ok/no_data，不受影响。

- 交接（2026-10-22，会话 97eae26f → production-ops-agent-2c）：
  - 实现 worktree `/Users/shenghuikevin/dev/AI/poa-308`（`feature/308-overlimit-evidence`，已合并测试分支）；独立测试 worktree `/Users/shenghuikevin/dev/AI/poa-308-tests`（`feature/308-overlimit-evidence-tests`，未接触实现，有效）。
  - 独立验收（Codex gpt-6.1-sol/gpt-6-sol，同一会话续跑）：首版 2 败为合同空白（`count_spilled` 返回值）→ 合同补 R11 `(records, raw_bytes)`；审查要求补强 3 处（仅靠字节触发上限与等于边界、持久化 view 完整键集合与不含凭据/行内容、在途控制失效）→ 作者补强；键集合与实现多出的 5 个来源字段冲突 → 合同补 R12 允许这 5 个网关计算字段（U4 片段继承需要）。
  - 独立审查（Codex gpt-6-astra 等）：首审 3 条均为测试缺口，已补；复验无发现，可合并。
  - 最终：#308 验收 + 实现自测 + PG 24 passed；全仓 ruff format/check 通过。
  - 下一步：push、开 PR（正文注明不改模型可见面、无迁移与新 grant；审计时间线会多出无 `tool_committed` 的 register-only evidence 项，审计合同已允许）、CI、`@codex review` 分诊；不改调查 loop，按合同不需要真实 Run。

## 待决

无。
