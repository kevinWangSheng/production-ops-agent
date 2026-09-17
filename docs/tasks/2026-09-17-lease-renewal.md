# DurableStore 租约续期（renew_lease）

- 状态：实现中（PR 待 CI）
- 更新日期：2026-09-17
- 依据：`docs/design/technical-proposal-2026-09-07.md` 第 6 节「续租和提交均校验执行身份与租约」与第 7 节提交一致性；`PRODUCT-CONSTRAINTS.md`「Runtime and human control requirements」（worker 重启/迟到完成不得抹掉更新的人工决定）；PR #33 描述与 UI 端到端 smoke 报告路径 6 指出的缺口。
- 工作区：worktree `/Users/shenghuikevin/dev/AI/production-ops-agent-lease`，分支 `fix/lease-renewal`（自 `origin/main` `b483a12`）。

## 目标与范围

`DurableStore` 没有租约续期 API。PR #33 的 `Workbench.run_once` 因此把租约默认取整个 Run wall（冻结上限 1800 s）：worker 被硬杀后 `LEASE_ACTIVE` 持续整个 wall，其它 worker 无法接手；smoke 只能把租约压到 20 s 演示恢复，而 20 s 短于一次模型请求上限（360 s），产品路径不能这样用。

本任务只在 main 侧交付：

- `DurableStore.renew_lease(lease, extend_seconds) -> datetime`；
- 对应 PG 语义测试；
- 两处使用方（PR #33 `run_once`、PR #30 `Worker`）的接线建议写在报告，不直接改那两个分支。

不改 `claim()` 与其它写路径（PR #26/#34 都在改 `claim()`，避免冲突），不改验收步骤、`feature_list.json` passes、冻结上限，不新增依赖。

## 语义

- 栅栏与写路径一致：run 为 `running`、owner/epoch 相符、**事故行** `control_generation` 相符、`lease_until` 非空且未过期、未过 `deadline`；任一不满足抛 `CONTROL_DENIED`，且 `lease_until` 不变。
- 过期即失效：只能重新 `claim()` 拿新 epoch；旧凭据此后永远续不动。
- 新值 `LEAST(GREATEST(lease_until, now + extend), deadline)`：不缩短更长的剩余租约，永不越过 deadline（`claim()` 本身不封顶，续期后会收回到 deadline 内）。
- `extend_seconds <= 0` 抛 `INVALID_INPUT`（与 `reserve_budget` 的 amount 校验同风格）。
- 续期不换身份，返回数据库时钟下的新 `lease_until` 供调用方安排下一次续期。
- 加锁顺序 incidents -> runs，与其它写路径同序。

## 前提与完成条件

- 前提：本地 PostgreSQL lab（`scripts/m0/postgres_lab.py start`），不调用模型或付费服务，费用为 0。
- 完成条件：确定性测试先红后绿；变异测试确认每条栅栏都有测试承重；`make check` 通过；`M1_DURABLE_POSTGRES=1` 定向测试通过；全新上下文独立审查完成且发现已处置；PR 已开且 CI 通过。

## 当前进展与验证证据

| 步骤 | 结果 |
|---|---|
| 基线 `test_m1_durable_state_postgres.py` | `21 passed` |
| 新测试红 | `13 failed, 1 passed`（方法不存在；锁序测试因线程先抛错而空过） |
| 实现后 | `35 passed`（14 新 + 21 旧），补两条测试后新文件 `17 passed` |
| 变异测试（9 项：去掉 expiry / generation / owner / epoch / running / deadline 检查、去掉 deadline 封顶、去掉 GREATEST、先锁 run） | 初次 7 红 2 存活；补 `test_deadline_passed_is_refused_even_when_the_lease_itself_is_still_unexpired` 与 `test_a_run_that_is_not_running_is_not_renewed_even_with_a_live_lease` 后 9/9 转红 |
| `make check`（采纳审查意见后） | `1050 passed, 92 skipped, 2 xfailed`；ruff/format/mypy 全部通过 |
| PG 定向测试（采纳审查意见后） | `M1_DURABLE_POSTGRES=1` 两文件 `38 passed` |
| 独立审查（全新上下文只读子代理） | 无阻塞发现，4 条 P3 全部采纳：docstring 措辞（封顶到 deadline 时会缩短）、检查与 UPDATE 共用同一 `now`、模块级 `install()` fixture、放宽 deadline/租约秒数的时间余量。审查者另行做了三方 merge-file 核对：与 #26、#34 均 0 冲突 |

## 接线建议（不在本 PR 实施）

见报告；摘要：`run_once` 用短租约（建议 420 s = 模型单请求上限 360 s + 60 s 余量）claim，并在 `_EmittingCommitter` 每次 `begin_round`/`commit_step`/`commit_tool`/`reserve_budget` 前 `renew_lease(lease, 420)`；`Worker.resume` 同样在 `execute_pending` 每个工具前续期。续期 `CONTROL_DENIED` 即按现有 handoff 路径停止。

## 下一步

- PR 合并后由 #33/#30 分支按建议接线，并把 smoke 路径 6 的 20 s 租约改为默认值复验恢复延迟。
