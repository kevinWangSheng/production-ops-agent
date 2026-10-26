# #347 发布事故后继 Run 在告警附加后换框

- 状态：进行中（实现与实现测试已完成，待独立审查与独立验收测试）
- 更新日期：2026-10-26
- 依据：[issue #347](https://github.com/kevinWangSheng/production-ops-agent/issues/347) Agent Brief（用户 2026-10-26 裁决 V1 = B、V2 = A、V3 = A）；[#305 合同](2026-10-24-305-successor-window-contract.md) W1-B、W2-A、R1、R5；[#343 合同](2026-10-24-343-alert-joins-release-incident-contract.md) R6、R7；[F11 PR-4 合同](2026-10-21-f11-pr4-contract.md) R3、R6。功能 F11。
- 工作区：`/Users/shenghuikevin/dev/AI/poa-347-impl`，分支 `feature/F11-347-release-successor-reframe`，起点 `origin/main` 78a9603。

## 目标与范围

#343 之后，告警可以附加到由发布异常开出的事故，但该事故的后继 Run 仍用发布 Run 的消费框 `[consumed_at − 24h, consumed_at]`，消费时刻之后附加的告警 episode 落在框外，后继 Run 查不到。本项让发布事故的后继按 #305 W1-B 同一规则换框，锚点保持 `released_at`。告警开的事故、tools、executor、迁移、grants、worker、Observer、`feature_list.json`、kind 真实 Run（#358）不在范围内。

## 前提与完成条件

- 前提：#305、#343 已合并（`main` 78a9603）。
- 完成条件（V3-A）：PG 确定性测试 + 网关级测试 + 独立审查通过；既有 #343 第 13 条 PG 测试由独立测试作者按合同改写。

## 合同（r1，抄 brief）

| 编号 | 裁决要点 |
|---|---|
| V1-B | 触发沿用 #305：episode = 每个 `delivery_key` 首条 `outcome='attached' AND status='firing'` 投递；截 UTC 秒后严格晚于前一快照 `time_policies[0].window.end` 时换框。重复投递、`resolved_attached`、发布附加不触发。新框 24h，终点 = 后继创建 DB 时刻（deadline − run_seconds）。 |
| V2-A | 锚点仍为 `released_at`（`release_released_at`），默认窗 `[max(released_at − 1h, 框起点), 框终点]`；`released_at + 24h` 之后创建时锚点截到框起点（`before_frame`）。 |
| 范围 | 只替换 `time_policies[0]` 的 window、anchor、default_query_window；`release_observation` 保持消费框锚点、逐字节不变；输入仍为 v6，不加 `alert_starts_at`；revision 不变。 |
| 路径 | new_run、overdue/concluded 时追问/纠正续开、fresh 回退（`INPUT_MISSING`）同一规则；同一 Run 重排队不换框（W2-A）；前一框终点不可解析 → `INPUT_INVALID`；后继的后继按前一快照框终点链式比较。 |
| 读侧 | 不改 `alert_records`（发布事故仍为 None，#343 R7）；新增按 `incident_id` 读附加投递的只读读取器。不新增 grants。 |
| V3-A | 合并门：PG 测试 + 网关级测试 + 独立审查；kind 真实 Run 由 #358 补做。 |

### 实现者可逆决定

| 决定 | 理由 |
|---|---|
| 读取器 `attached_alert_deliveries(incident_id)` 只取 `outcome='attached'` 行的六列，`ORDER BY delivery_id` | 触发只需要这些列；与 `alert_intake_records` 同序，`_attached_episode` 的「每个 key 首条」口径不变。 |
| 抽出 `_attached_episode`（触发）与 `_previous_frame_end`（读前一框终点），告警与发布两条分支共用 | 保证两类事故逐字同一触发规则与同一 `INPUT_INVALID` 口径；`_attached_anchor` 签名不变。 |
| 发布分支的 `released_at`、`consumed_at` 取自 `_release_rebuild`（存储记录），不从快照解析 | 与 `_check_release_fact` 同一来源；记录与快照不一致时由后者拒绝（`INCOMPATIBLE_STATE`）。 |
| 内存替身 `MemoryIncidentStore.attached_alert_deliveries` 返回空 | 内存替身没有告警接入；service 也按 `getattr` 容忍缺失，与 `alert_records` 一致。 |

## 进展与证据

- 实现：`ReleaseAnchor.frame_end`；`continuation_input(release=)`；`Workbench._release_reframe`、fresh 回退中的发布换框；`DurableIncidentStore.attached_alert_deliveries`。文档：`docs/development.md` 一句、#343 合同 R6 与 F11 PR-4 合同 R6 各一行修订。
- 实现测试：`tests/test_347_release_successor_reframe_impl.py`（含网关级：同一查询在 Run1 上 `WINDOW_OUT_OF_SCOPE`，在后继上放行并发出请求）、`tests/integration/test_347_release_successor_reframe_impl_postgres.py`。
- 红绿：撤掉产品改动后 13 个实现测试中 11 个失败（剩余 2 个是「无附加不变」「waiting_human 同 Run」，按设计在改动前后都通过）；恢复后全部通过。
- 回归（本地 PG 55642）：#305、#343、F11 PR-4b 单元/集成/验收 142 passed；web/worker/continuation/F2 PG 212 passed。#343 第 13 条在本分支保持绿（它只断言锚点与默认窗起点，二者对近期发布不变）。

## 待决

- 无需用户裁决的新问题。kind 真实 Run 见 #358。
