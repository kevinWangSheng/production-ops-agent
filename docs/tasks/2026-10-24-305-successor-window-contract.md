# #305 合同预审：附加之后新开的 Run 取什么授权窗口（r1）

- 预审者：全新上下文 Claude（Opus 5.5），只读，基于 main `200172c`，2026-10-22。未跑测试、服务、模型或 kind。
- 依据：issue #305；F2 任务记录 PR-C 节与 r1；M1-04 J1–J5（`docs/tasks/2026-10-10-m1-04-alert-intake.md:87-94`）；C3 §4（:94-106）、§5 四类变化表（:194-206）、§6；PR-C 证据 `docs/evidence/f2-alert-correlation-live/run.md:35-47,108-111`；F11 PR-4 合同 R3、R6、P4-05、§5。

## 1. 现状

已核对：
- 告警 Run 的授权框按 J1 计算：`[received_at − 24h, received_at]`，`received_at` 是事故**首次**投递的时刻。锚点和默认查询窗按 J2/J3 从 `startsAt` 推出（`opspilot/investigation/inputs.py:37,72-95,107-133`），写进输入快照的 `time_policies[0]` 与 `scope_facts.alert_starts_at`。
- 每个 attempt 的网关授权框都从该 Run 自己的输入快照读出，不读时钟（`opspilot/tools/otel_demo.py:1177-1192,2205-2206`；fixture `opspilot/tools/fixture.py:213-230`）。查询窗不在框内时拒绝为 `WINDOW_OUT_OF_SCOPE`（`opspilot/tools/executor.py:791-792`）。框本身还受 `max_window_seconds=24h` 约束（:794；`otel_demo.py:765`），因为 loop 每次都把整框作为 `ToolRequest.window` 发出（`otel_demo.py:736-739,759-765`），而这个上限属于工具注册合同（`registry.py:686`）。
- 后继 Run 的输入由 `_successor_input` 生成（`opspilot/web/service.py:1131-1194`）→ `continuation_input`。它原样沿用前一 Run 的 `time_policies` 与 `alert_starts_at`（`inputs.py:192-227`，即 J5「不用续开时刻」）。fresh 回退走 `_alert_rebuild`，只取 `deliveries[0]`，也就是首次投递（`service.py:1203-1240`）。
- 用到 `_successor_input` 的路径：`new_run`（`service.py:935-946`）；追问/纠正在 `overdue or concluded` 时续开（`opspilot/persistence/controls.py:453`、`service.py:903-915`）。对未结论、未超时的 `waiting_human` Run 做 follow_up，只把**同一个 Run**重新排队，输入不变（`controls.py:453`；证据 run.md:42）。
- 附加投递带 `received_at`、`starts_at`、`outcome='attached'`，`incident_id` 指向原事故（`opspilot/persistence/alerts.py:96-158`）。`alert_intake_records` 会按 incident 读出全部投递（`opspilot/acceptance_alert.py:160-163`），所以新窗口可以从库行确定性地算出来。
- 继承证据时，绑定的 `time_scope_refs` 按原值带过来，后继 Run 不会按新框重判（`opspilot/investigation/context.py:1478-1501`；`reports.py:1103-1115`）。
- 告警 Run 已经有输入 v3/v4，版本取「最高的事实」（`context.py:516-527`）。F11 PR-4b 会占用 v6，并在同一组函数里加 `_release_rebuild` 与 `ReleaseAnchor`（框结束于**消费时的 DB 时刻**，F11 PR-4 合同 R3、R6）。worktree `poa-f11-pr4b` 正在实现。
- web 的 `now()` 是 DB 时钟（`opspilot/web/store.py:518,533,870`）。

推断：
- 两种情况都不需要改 `tool_schema_revision`：新规则只改后继 Run 自己快照里的框位置、框长仍为 24h；在途 Run 的输入也不变。C3 §5 把这类变化归为「同一授权范围内的窗口推进 → 实例层」。框若超过 24h，就会碰到 `max_window_seconds`，要 bump 版本，在途 Run 会 blocked。
- 同一 Run 重新排队时，改输入会让快照哈希对不上，和 #304 的 `INCOMPATIBLE_STATE` 同类，所以同 Run 路径不能换窗。

## 2. 决定表

### 需用户裁决（改了已批准的 J5，涉及授权窗口语义）

| 编号 | 问题 | 选项与代价 | 推荐 | 影响面 |
|---|---|---|---|---|
| W1 | 后继 Run 什么时候换框、换成什么 | **A** 框 24h，结束于最新附加 firing 的 `received_at`；锚点取该 episode 的 `startsAt`（按 J2 截断）。代价：完全由告警行确定，但只覆盖到收到告警那一刻（实测只有 08:05:54–08:06:48，约 1 分钟），#305 里 08:00–08:10 的查询仍会被拒。**B** 存在 `received_at` 晚于前一 Run 框终点的附加 firing 时，框 24h，结束于后继 Run 创建时的 DB 时刻；锚点取最新附加 episode 的 `startsAt`；默认查询窗为 `[锚点−1h, 框终点]`；没有附加时 J5 不变。代价：这一情况下改写 J5「不用续开时刻」，新框靠快照记录、不能从告警行重算。有先例：F11 R3 的发布框结束于消费时刻，提交 Run 的框结束于提交时刻。**C** 把框延长到覆盖首尾两次 episode，长度超过 24h。代价：要改 `max_window_seconds` 并 bump 工具合同版本，在途 Run blocked，单次查询体量也会变大。 | **B**：只有它能覆盖 #305 实测里被拒的那段查询，而且不碰工具合同 | inputs、web service、J5 文档、`docs/development.md:139` |
| W2 | 同一 Run 重新排队的路径（对 `waiting_human` 做 follow_up、resume）怎么办 | **A** 窗口照旧，模型被拒后照实写缺口；要新窗口就由人走 cancel+new_run，或在结论发布后追问续开。**B** 有新的附加时，follow_up 改为开后继 Run。代价：改动 F12 U1 与 #47 的控制语义，而且会和 #304 的修复、PR-E 的 C4-A 路径交叉。 | **A**。PR-E 要证明的是事件进入输入，不需要新窗口 | 控制语义、PR-E |
| W3 | 触发条件包不包括 F11 发布附加 | **A** 只看告警 `attached` 投递，发布附加仍按 J5，另开 issue。**B** 两种附加都触发。代价：依赖 PR-4b 的 link 表与 v6，两条线同时改同一组函数。 | **A** | 和 PR-4b 的边界 |

### 可逆默认（Agent 自定，理由一行）

| 编号 | 内容与理由 |
|---|---|
| R1 | 「最新附加 episode」= 该事故 `outcome='attached' AND status='firing'` 的投递里 `received_at` 最大的那条，锚点取它的 `starts_at`/`starts_at_raw`。理由：只用已入库的事实，和 R-C3 一致。 |
| R2 | 只替换 `time_policies[0]` 的 `window`、`anchor`、`default_query_window`，以及 `scope_facts.alert_starts_at` 三个键，不加新键。理由：输入版本不变（仍为 v3/v4），不和 PR-4b 的 v6 冲突，旧 worker 也能读。 |
| R3 | `alert_context` 仍取首次投递（K6），附加 episode 的注解不进上下文。理由：R-C4 白名单。 |
| R4 | 继承证据的 `time_scope_refs` 按现状原值带过来，不按新框重判。理由：代码已把继承证据定义为历史证据（`context.py:1478-1482`）。框只后移，只有间隔接近 24h 时才会有旧视图落到新框外。 |
| R5 | fresh 回退（`_alert_rebuild`）也用同一条规则：有附加就按 B 取框，没有就保持 J5。理由：两条路径对同一组行得到相同的锚点。 |
| R6 | `renew_basis` 不加附加水位。在构建输入和提交之间到达的附加会漏掉，但事件仍会进入输入，下一次续开就能补上。理由：差距只有秒级，不值得扩大 `RENEW_INPUT_STALE` 的面。 |
| R7 | 不改 handoff note 和 prompt 常量，所以 `prompt_revision` 不变。 |

必答类别：时间与时钟见 W1、R1（DB 时钟，截到 UTC 秒）。状态转移见 W2。权限：框长不变，不新增任何角色权限，只有 web 写 `runs.input`。数据合同：无迁移，输入版本不变（R2）。旧数据：在途 Run 和已归档证据不受影响。并发见 R6。外部调用：不变。验收与 `passes`：不改 F2 的五步，不翻转 `passes`。

## 3. 允许修改的文件、可观察行为与真实证据

- 允许修改：`opspilot/investigation/inputs.py`（`AlertAnchor` 接受框终点，`continuation_input` 可以换框）、`opspilot/web/service.py`（`_successor_input`、`_alert_rebuild`）、`docs/development.md:139` 那句话、M1-04 记录的 J5 修订行、F2 任务记录、测试。
- 不改：tools、executor、registry、`context.py`、迁移、grants、worker。
- 给独立测试作者的确定性行为（走公开接口加 PG）：
  1. 没有附加时，new_run 和超时续开的 `_time_contract` 等于首个 Run（现有 J5 测试原样保持绿）。
  2. 首次 firing → attached（新的 `startsAt`）→ cancel+new_run：后继框终点等于创建时的 DB 时刻（±1s，取同一次控制里 deadline − run_seconds），框长 24h，`anchor` 等于附加 `startsAt`，默认查询窗起点 = `max(anchor−1h, 框起点)`。
  3. 结论发布后的追问续开得到同样结果。
  4. 一次查询覆盖附加 episode（例如 `[startsAt, startsAt+5m]`，在框内）时不被 `WINDOW_OUT_OF_SCOPE` 拒绝，同一查询放在首个 Run 上会被拒。
  5. 对 `waiting_human` Run 做 follow_up：同一个 Run，输入逐字节不变（W2-A）。
  6. 重复投递和 `resolved_attached` 不触发换框。附加早于前一框终点时不再换框。
  7. fresh 回退在有附加时取同一个锚点（R5）。
  8. 输入版本串不变，`tool_schema_revision` 不变。
- 最小真实证据：在 kind 里依次做 firing → attached → new_run（或发布后追问）。附 LangSmith trace，显示该 Run 有一次覆盖附加 episode 的指标查询返回 `ok`，以及 `summary.json`。这部分可以并入 PR-E 的 kind 串行会话，不单独占一个窗口。

## 4. 是否纳入本批

建议纳入 M2 收尾，但不作为 F2 `passes` 的门槛（F2 第 1 步只要求关联正确，`feature_list.json` F2 steps[0]）。排期放在 **F11 PR-4b 合并之后、PR-E kind 会话之前**。

理由：
- 改动只在 web 侧，无迁移、无版本变化，PR 小。
- PR-4b 正在改同一组函数（`_successor_input`、`inputs.py`），并行会冲突。
- 如果不修，PR-E 的 trace 还会再写一次「再次触发的 episode 未观察到」，在演示里很显眼。修完后真实证据可以借 PR-E 的窗口免费取得。

如果 PR-4b 拖到 PR-E 之后，就延后 #305，不阻塞 PR-E。

## 5. 未确认

- 可逆默认 R4 的边界：报告校验是否在别处按窗口重核继承视图。本次只读到 `delivered_from_context` 没有重核。
- PR-4b 实现中是否已经改动 `continuation_input` 或 `_alert_rebuild` 的签名。没有读它的 worktree diff。
- otel-demo 的 Prometheus 保留期能否覆盖后移后的整框（24h）：推断不影响，因为原框也是 24h。
- 发布附加（W3-B）在实测中出现的频率：没有数据。


## 裁决（2026-10-22）

用户裁决 W1-B、W2-A、W3-A（按推荐）；R1–R7 可逆默认照做。开工条件：F11 PR-4b 合并后；不作为 F2 passes 门槛；真实证据并入 PR-E kind 会话。
