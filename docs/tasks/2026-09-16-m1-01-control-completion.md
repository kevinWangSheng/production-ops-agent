# M1-01 人工控制补全

状态：实现中，尚未完成测试与审查。

## 目标、范围与依据

按用户 2026-09-16 授权接续 PR #28 待决中的三项：全局/登记目标暂停持久化及领取、预算、请求、采纳栅栏；追问/纠正内容持久回读；每轮固定输入边界及已处理水位。依据 SPEC 有界 M1-01、PRODUCT-CONSTRAINTS、C3 第 4/5 节及 PRD/feature_list 的 F2/F12。close/reopen、目标重绑定、合并/拆分仍不实施；不改 passes 或验收步骤。

## 工作区与前提

- `/Users/shenghuikevin/dev/AI/production-ops-agent-m1-control-completion`，`feature/m1-01-control-completion`，基于 `integration/m1-01`。
- 依赖 PR #20/#21/#26/#28/#29/#30（#27 经 #29 集成）。PR base 使用 integration/m1-01。
- 接手时仅 persistence.py 有前会话未提交修改（79 insertions / 11 deletions），已保留并接续；未移动用户 WIP。
- 用户授权 make setup、本地独占 PG、必要的有账本真实调用；本任务当前无需付费调用。
- 与并行 UI/acceptance 工作隔离，主要编辑 persistence.py；若 loop/adapter 需要接入，只增加输入快照边界。

## 完成条件

领域/持久化确定性检查、M1_DURABLE_POSTGRES=1 的暂停竞争集成、关键断言变异验证、make check、全新上下文独立审查并修复；提交推送 stacked PR，workflow_dispatch CI success，当前 HEAD code review 与 inline thread 闭环；不自动合并。

## 当前进展与证据

- 已核查 pwd/git status/git diff；make setup 成功，锁定 43 packages，39 packages audited。
- 半成品包含 scope/输入表与 Lease 代际，尚缺完整事务/输入快照与测试。
- 首次 make check：ruff check 通过，format check 因 persistence.py 格式失败；尚未运行后续检查，不计通过。
- 独立设计审查已启动；重点为锁序、暂停解除后显式恢复、输入快照与处理状态的分离。

## 下一步

完成 scope 锁与输入轮次合同，补领域/PG/loop 回归，运行变异与全套检查，审查后交付 PR。当前无需要用户裁决的新范围事项。

## 验证证据（2026-09-16）

- `.venv/bin/python -m scripts.m0.postgres_lab start` 成功启动本 worktree 专属 PG 17.9；完成后已 stop，数据保留。
- `M1_DURABLE_POSTGRES=1 .venv/bin/python -m pytest tests/integration/test_m1_control_completion_postgres.py -q`：3 passed；覆盖目标暂停 claim、解除不自动恢复、follow-up payload 回读、全局暂停预算栅栏。
- `M1_DURABLE_POSTGRES=1 .venv/bin/python -m pytest tests/integration/test_m1_durable_state_postgres.py -q`：41 passed。
- `make check`：1444 passed, 98 skipped, 2 xfailed；ruff check/format、mypy 均通过。
- 设计独立审查（全新上下文）完成：指出统一 scope 锁序、冻结 round 输入边界、每次模型/工具请求发送前复核 lease、输入原文与模型投影边界；已采纳 scope lock、begin_round、assert_current 与 loop 接入。审查未运行测试，PG/确定性命令证据以上述实际输出为准。

## 未完成与交接

- 当前代码尚未提交、推送或创建 PR；需完成最终 diff 审核后提交 `feat: complete durable human control [M1-01]`，推送并创建 base=`integration/m1-01` 的 stacked PR。
- 需要 workflow_dispatch CI 覆盖当前 HEAD，等待 code review 并处理线程；不自动合并。
- close/reopen、目标重绑定、事故合并/拆分仍是用户待决，未实现；11 个 feature passes 未改。

### 最终 PG 复验（2026-09-17）

- `M1_DURABLE_POSTGRES=1 .venv/bin/python -m pytest tests/integration/test_m1_control_completion_postgres.py -q`：3 passed。
- `M1_DURABLE_POSTGRES=1 .venv/bin/python -m pytest tests/integration/test_m1_durable_state_postgres.py -q`：41 passed。
- 前述专属 PostgreSQL 已由 `postgres_lab start/stop` 启停，输出为 server started / server stopped；实例数据保留。

## 最终交付（2026-09-17）

- HEAD：`445020f`，后续提交未改写历史；提交标题中的 `[M1-01]` 保留为用户要求的已推送历史信息，新增提交已去掉该伪 feature id。
- PR：[ #31 ](https://github.com/kevinWangSheng/production-ops-agent/pull/31)，base=`integration/m1-01`。
- 最终 workflow_dispatch：[35175606617](https://github.com/kevinWangSheng/production-ops-agent/actions/runs/35175606617)：`checks` 与 `m0-postgres` 均 success。
- 最新机器人审查覆盖最终 HEAD；6 条及后续 4/3 条 inline thread 均已逐条回复并 resolve，当前未解决 thread 数为 0。采纳项包含 mandatory expected generation、actor 审计、输入 allowlist、暂停优先级与 new_run 重放。
- 用户审核/合并仍是最后一道门；本任务不自动合并。close/reopen、目标重绑定、合并/拆分仍待决。
