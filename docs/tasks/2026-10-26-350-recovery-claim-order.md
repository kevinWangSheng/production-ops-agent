# #350 Observer 领取顺序：恢复会话有界提前

- 状态：进行中（PR 待审查）
- 更新日期：2026-10-26
- 依据：[#350](https://github.com/kevinWangSheng/production-ops-agent/issues/350) Agent Brief（用户 2026-10-26 裁决 U1 = D、U2 = A）；C3 §10「采样与提交」；F6、F11
- 工作区：`/Users/shenghuikevin/dev/AI/poa-350-impl`，分支 `feature/F11-350-recovery-claim-order`

## 目标与范围

`claim_due_samples` 原先把两类已到期会话只按 `active_sample_due_at` 排序，发布观察多时可能延误事故恢复确认。本项让恢复会话在已到期会话之间最多领先一个自身采样间隔；资格条件、领取/提交事务、`ObserverLoop`、迁移与 grants 不变。

## 合同（r1，抄自 brief）

| 编号 | 裁决要点 | 出处 |
| --- | --- | --- |
| U1 | D：有效到期 = 恢复 `due − sample_interval_seconds`、发布 `due`；同值按 `session_id`；资格仍为真实 `due ≤ now` 等原条件；不做绝对优先或份额 | brief |
| U1a | keyset 游标与 SELECT、ORDER BY 用同一有效到期表达式；`probe_budget = 8 × limit` 不变 | brief |
| U1b | 不加常量、环境变量、列、索引或迁移 | brief |
| U2 | A：不跑 kind、不附 trace（Observer 无模型调用、无 Run）；AGENTS.md「恢复路径」只指 C3 §7 故障/重启恢复 | brief |

可逆技术决定（实现者）：

- 有效到期表达式是 `claim_due_samples` 内的局部变量 `claim_order`，三处拼接同一字符串，避免手写漂移（独立审查建议：不用模块常量，免与「不加新常量」字面冲突）。
- SELECT 仍以 `SELECT session_id,purpose` 开头，既有分页测试按此前缀识别候选查询。
- 实现测试把到期时刻放在 1800 年，避免共享库里其他测试的到期会话插队；fixture 取消自己额外登记的发布。

## 证据

- 实现测试 `tests/integration/test_350_claim_order_impl_postgres.py`（临时 PG 17）：7 passed。红绿对照：`git apply -R` 实现补丁后同时到期、早到期 10 s（< 15 s 间隔）及两组混合加锁分页共 4 例红；`git apply` 后全绿。
- 回归（临时 PG 17）：F6 验收与 F6 测试、F11 四步验收、F11 pr2a/pr2b/pr5b（含 #349）PG 套件、`test_f11_release_observation_postgres`、`test_m1_observer`：487 passed；m1_02 observation store/takeover/streak、F7 redaction、F11 pr4a、pr2b 单元、architecture：215 passed、2 xfailed（既有标记）。
- 未做真实运行：Observer 领取顺序无模型调用、无 Run，按 U2-A 不跑 kind、不附 trace。

## 待决

无。
