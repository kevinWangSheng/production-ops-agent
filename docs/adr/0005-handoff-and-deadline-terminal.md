# ADR-0005：交接不发布结论；过期 Run 由清扫收尾

状态：**已决定**（用户 2026-09-24）。

## 背景

- 调查 loop 每次结束都提交结论步骤，交接（预算耗尽、报告不合格、配对失败等）也不例外（`opspilot/investigation/loop.py` `_finish`）。后台 runner 拿到结论步骤就发布（`runner.py` `_publish`），`DurableStore.publish()` 写入事故 `conclusion` 并把 Run 标为 `completed`；之后 `control()` 因 `conclusion` 非空拒绝一切人工动作（`ILLEGAL_TRANSITION`）。交接后值班人员无法追问、纠正、取消或重新调查。
- 工作台路径（#33，`opspilot/web/service.py`）已改为交接不发布，只记录 `run_handoff` 事件，但真实驱动器是 runner，两条路径矛盾。
- Run 超过 deadline 后，租约栅栏拒绝 worker 的一切写入，没有写入者能把 Run 落为终态，Run 行停留在 `running`（#29 审查 P3 / 第八轮 P1）。

上游对照（HolmesGPT 固定 `5e983c17`）：只有给出回答的会话记 `COMPLETED`，异常记 `FAILED` 并写错误事件，不当作回答；失败后用户仍可继续发消息（`holmes/core/conversations_worker/worker.py` `_terminal_to_status`、`_fail_conversation`、事件历史重建）。卡住的会话由 pg_cron 清扫与停机钩子写 `TIMEOUT`（`conversations_worker/models.py` `ConversationStatus`）。

## 决定

1. **交接不发布结论。** 仅当 loop 得到合格报告且无 handoff 时 publish。交接时 Run 记为交接终态（不是 `completed`），事故保持开放，最后一份不完整报告与原因可在页面查看；追问、纠正、取消、`new_run` 保持可用。runner 与工作台统一为此行为。
2. **过期 Run 由清扫收尾。** 按数据库时钟定期找出 `deadline` 已过且仍为 `running` 的 Run，写为超时交接（`DEADLINE_EXCEEDED`），释放租约；事故按第 1 条保持开放。清扫写路径不经 worker 租约，但须在同一事务内确认 Run 仍为 `running` 且已过期，不覆盖期间落下的人工决定。补充（2026-09-26，PR #52 审查采纳）：从未被领取就过了 wall 的 `queued` Run（worker 停机或积压）同样无人能领，按同一规则清扫为超时交接。

## 后果

- 正面：与 v4 验收包「预算/连接失败不能冒充完成」、PRODUCT-CONSTRAINTS 人工控制优先一致；与上游行为对齐，实现量小。
- 负面：合格结论发布后仍会锁住人工控制，「出结论后再追问」要等 close/reopen（已移出 M1-01）。
- 反转条件：若需要把交接报告作为对外正式结论（例如通知外部系统），另立合同区分「结论」与「交接报告」。
