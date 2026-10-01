# M1-01 后续项：事故列表页显示当前 Run 状态

- 状态：进行中（合同写定，待独立测试作者）
- 更新日期：2026-10-01
- 依据：[#61 任务记录](2026-09-29-m1-01-incident-state.md) 第 91 行待决（用户 2026-10-01 决定做）；C3 §4
- 工作区：`feature/m1-01-list-run-state`，`../production-ops-agent-list-run-state`

## 状态

- 2026-10-01：新增 `tests/integration/test_m1_list_run_state_postgres.py`，提交 `75a4f35`；真实 PG 结果 1 红（合同 1 的 `Run` 列尚未实现）、3 绿（合同 2 只读、合同 3 查询次数、合同 4 Control 语义）；ruff check/format check 通过。无 Run 场景由 SQL 将 `current_run_id` 置空，因为现有公开受理入口始终创建首个 Run。
- 2026-10-01：列表查询以一次 `LEFT JOIN opspilot_runs` 投影当前 Run 的 `state`，模板新增第二列 `Run`，无当前 Run 显示为空；既有列表断言同步加入该合同列（直接合同后果：Run 状态现在合法出现在行值中，不能再把 `queued/running` 作为整行禁词）。定向既有测试通过；PG 独立/指定 integration 矩阵已重跑并完成 lab 停止释放。

实现细节：`IncidentSummary.run_state` 置于末尾并默认 `None`，保持其他内存/测试构造调用兼容；Durable 查询用单次左联接读取权威 Run 行。

## 问题

#61 之后列表页只在 paused/cancelled 时显示 Control，分不出「交接等待人工」和「进行中」。事故页已有 `run-state`，列表页没有。

## 合同

1. 列表页（`GET /`）每行新增一列 `Run`，显示该事故当前 Run（`current_run_id` 指向的 Run 行）的状态，取值与事故页 `run-state` 同一词表（queued、running、waiting_human、completed、cancelled 等存储值原样）；没有当前 Run 时为空。
2. 值来自权威 Run 行，读页面不改变任何行。
3. 列表查询不因行数增加而对每个事故单独查询 Run（一次查询或一次联表取得）。
4. 其他列与 Control 语义不变（#61 合同不变）。

## 验收口径

真实 PG 上用公开入口制造：刚受理（queued）、执行中（running）、交接停放（waiting_human）、发布（completed）、取消，以及无 Run 的事故；只读 `GET /` 的 HTML，断言每行 Run 列的值。

## 不做

不改事故页；不加筛选/排序；不显示 Run 的原因或报告。

## 审查后补充（2026-10-01）

- 已知行为：列表页不执行超时清扫（合同 2 读页面不写行），已过期但未被清扫的 Run 在列表上仍显示 `running`，直到 worker 清扫或有人打开事故页。
- 独立审查（Opus）：产品代码符合合同；两条测试缺口（多 Run 时取 current_run_id 而非最新 Run；合同 3 统计整个请求的查询而非最后一个事务）交独立测试作者补；可简化项：撤回 `find_incident` 的联表（合同未要求），`_summary` 改用 `row["run_state"]`。
