# M1-01 暂停栅栏核查：全局/目标 suspension 落在租约快照之后

- 状态：进行中
- 更新日期：2026-09-26
- 依据：ROADMAP「M1-01 剩余工作」第 4 项的 suspension 半部；[工具执行器记录 §46/§47](2026-09-14-m1-01-tool-executor.md) 交接项「持久化暂停栅栏」；[人工控制记录](2026-09-16-m1-01-human-control.md)「全局/目标 suspension 持久化并接入 claim / reserve_budget / 结果采纳」；PRODUCT-CONSTRAINTS「人工控制决定在模型权限之外」；C3 §6/§7（续租与提交同样校验租约）。功能 ID M1-01。
- 工作区：`feature/m1-01-suspension-fence`，`../production-ops-agent-suspension-fence`

## 目标与范围

核对 main（`1ece309`）上每条耐久写路径：租约发放后落地的全局或目标暂停，是否在事务内拦住后续
的工具预算扣费、工具结果采纳、步骤提交、续租、发布与交接。只关闭真实缺口；已闭合则补 PG
测试钉住，不改产品代码。不含 DurableToolLedger 接线、`control()` 语义、UI 与重构。

## 前提与完成条件

- 前提：本 worktree 自有 PG 实验实例（55431，`tmp/m0-b/postgres`）。
- 完成条件：每条路径有核查结论与对应测试；`make check` 与 PG 集成全过；独立审查无未处置 P1/P2。

## 核查结论（main `1ece309`，`opspilot/persistence.py`）

`claim()` 把 `global_generation` / `target_generation` 快照进 `Lease`；`_lease_revoked()`
（`:153`）比较 `global_suspended` / `target_suspended` 标志与两个代际。`set_global_suspension()`
/ `set_target_suspension()` 暂停时对 `opspilot_scope_controls` / `opspilot_target_suspensions`
取 `FOR UPDATE`，锁全部（或该目标的）事故行，并把 running Run 改写为 `owner=NULL,
lease_until=NULL, state='paused'`。

| 写路径 | 事务内读 scope 行 | 栅栏实现 | 结论 |
|---|---|---|---|
| `claim` `:706` | `_lock_scope` FOR SHARE + JOIN | 标志直接拒 `CONTROL_DENIED`；事故 `paused` 也拒 | 已闭合 |
| `reserve_budget` `:827` | 同上 | `_lease_revoked` | 已闭合 |
| `charge_tool` `:961` | 同上 | `_lease_revoked`；**例外**：结算本租约已预留的 dispatch 放行（只把预留秒数改为实际秒数，不新建预留、不采纳结果，代码注释已说明） | 已闭合，例外已钉住 |
| `commit_step` `:1286` | 同上 | `_lease_revoked` → 响应落 `late_result` | 已闭合 |
| `begin_round` `:1589` | `_lock_scope` FOR SHARE（结果并入行，不 JOIN） | `_lease_revoked` | 已闭合 |
| `commit_tool` `:1349` | 同上 | `_lease_revoked` → 结果落 `late_result`，不采纳 | 已闭合 |
| `publish` `:1659` | 同上 | `_lease_revoked` + `run_state != running` + 事故 `paused` → 结论落 `late_result`，返回 False | 已闭合 |
| `hand_off` `:1183` / `block` `:1159` | 同上 | `_lease_revoked` + `run_state` | 已闭合 |
| `renew_lease` `:777` | **否**（不读 scope 行） | 自带判定：`run_state='running'`、owner/epoch/事故代际、`lease_until` 非空 | 可观察行为已闭合，靠暂停写回 Run 行 |
| `settle_budget` `:890` | **否** | 同上（无 `run_state`） | 同上 |
| `abandon` `:1278` | 否 | 按 owner/epoch/代际只清 `owner`/`lease_until`，不写业务记录 | 无需栅栏 |

并发：读 scope 行的写路径先 `FOR SHARE` scope 行，暂停对同一行 `FOR UPDATE`；`renew_lease` /
`settle_budget` 不读 scope 行，靠事故行 `FOR UPDATE` 与暂停的事故行锁串行。两种情况下暂停都要等
在途写事务提交后才能落地，落地后同一租约的下一次写被拒；没有「暂停已提交、读到未暂停的写也
提交」的窗口。测试 ③ 用真实写路径（`reserve_budget` 与 `renew_lease` 各一）在取锁后延迟
`clock_timestamp()` 读取来钉住这一点。

变异核对（临时补丁，已还原，不入库）：
- 去掉 `_lease_revoked` 的 4 个 scope 判定：6 个新用例仍全过 → 目前承重的是暂停对 Run 行的改写
  （`lease_until=NULL`/`state='paused'`）加行锁；代际列是纵深防御。
- 去掉暂停对 Run 行的 `UPDATE`：`renew_lease` 首个未拒（`settle_budget` 同理未读 scope 列），
  其余路径由代际栅栏拦下。两条路径与 `_lease_revoked` 不一致属纵深防御缺口，不是可观察缺口，
  按「已闭合则不改产品代码」未改；是否对齐留给 owner。

## 执行进展与证据

- 新增 `tests/integration/test_m1_suspension_fence_postgres.py`（global/target 各参数化）：
  ① 租约后暂停 → 上表全部路径被拒/落 late_result，预算与工具用量不变、结论为空，释放暂停后旧租约
  在全部路径仍被拒；② 已预留 dispatch 的结算放行、新 dispatch 仍拒；③ 暂停对在途写阻塞，
  在途 `reserve_budget` / `renew_lease`（真实写路径，取锁后延迟时钟读取）提交后暂停才落地，
  下一次写被拒。
- 首次运行即绿（产品代码未改），证明栅栏已在；变异核对见上。修订后 8 例连跑 3 次全过。
- `make check`：ruff 通过，1936 passed / 216 skipped / 2 xfailed。
- `M1_DURABLE_POSTGRES=1 M0_B_POSTGRES=1 pytest tests/integration -q`：178 passed / 38 skipped
  （含新增 6 例）。
- 未跑真实 Run：只加测试，恢复/状态路径零改动。

## 下一步与交接

- 独立审查（全新上下文，2026-09-26）：无 P1。P2「锁测试用裸连接持锁、并发段落以偏概全」→ 已改为
  真实写路径持锁并改写段落；P3 采纳：`holder` 泄漏（随重写消失）、结算 docstring「只降低」改为
  「替换为实际秒数」、`begin_round` 行措辞、补 `abandon` 行、释放后复验全部路径与 `late_result:step:`。
  审查者用探针确认变异 2 下除 `renew_lease` / `settle_budget` 外各路径均由代际栅栏拦下。
- PR、`@codex review` 一次分诊；不合并（功能 ID 下的测试 PR，由 lead 决定）。
- 已考虑、不做（lead 决定 2026-09-26）：把 `renew_lease` / `settle_budget` 对齐到 `_lease_revoked`
  补读 scope 列。理由：暂停对 Run 行的改写（`owner=NULL, lease_until=NULL, state='paused'`）与事故行锁
  已承担栅栏，变异核对证明这不是可观察缺口；本项按速度优先、只改真实缺口处理。
- 审查者提出、本项未核实：持续重叠的写流量下，新的 `FOR SHARE` 可插到排队中的暂停 `FOR UPDATE`
  之前，暂停可能撞 `lock_timeout=4000ms` 报错而非落地；超出本项范围，记给 owner。
- PG 实例：任务结束后 `postgres_lab stop`，数据保留。
