# #244 原始资料保留期清理（关闭后 30 天）

- 状态：进行中（合同 r1 已定，待实施 PR-1）
- 更新日期：2026-10-27
- 依据：[issue #244](https://github.com/kevinWangSheng/production-ops-agent/issues/244) Agent Brief；C3 §11（保留与删除）、§7（PostgreSQL 业务记录是恢复权威）；[F13 第 4 步合同](2026-10-15-f13-step4-contract.md) U7；[M1-03 记录](2026-10-05-m1-03-postmortem.md) D12；ADR-0006。功能 F13 相关，不改 F13 验收。
- 裁决：`~/.agent/handoffs/opspilot-rulings-2026-10-27.md` 第 2、3 节（下称「裁决」）；预审：`~/.agent/handoffs/opspilot-prereview-244-2026-10-27.md`（Codex gpt-6-astra→gpt-6-sol，r0.1，基线 `17bc865e`，下称「预审」）。
- 工作区：PR-1 开工时填写。

## 目标与范围

产品目前没有任何清理任务。本轮按 C3 §11「关闭后原始资料默认 30 天」做最小交付：清理五类原始载荷——`opspilot_evidence.raw`、`opspilot_observation_signal_readings.raw`、`opspilot_release_query_results.raw`、`opspilot_release_comparison_queries.raw`、`opspilot_alert_deliveries.alert_json`。原列置 NULL 并记 `payload_state`，行、哈希、时间与 `evidence.view` 保留。

不在范围：`opspilot_steps.response/tool_results`、`opspilot_inputs.content`、`opspilot_runs.input`（恢复合同 #375）；报告、审计、知识 90 天或归档（#374）；LangSmith 外部副本删除（归 #243 后续）。

## 前提与完成条件

- 前提：`main` `17bc865e`；迁移编号用 0023（#245 PR-1 先占 0022，本项 PR-1 等其合并后再合）。
- 完成条件：PR-1–PR-3 合并，#244 独立验收（独立测试作者编写）通过、独立审查无未处置发现。F13 步骤与 `passes` 不变。不需要 kind。

## 合同（r1）

| 编号 | 裁决要点 | 出处 |
|---|---|---|
| U1 | 30 天从最后一次 close 起算：新增 `opspilot_incidents.closed_at`，close 事务写 DB 时钟，reopen 清空，再次 close 重新起算 | 裁决 §2；预审 §2 U1 |
| U2 | 固定保留：活跃 Run/观察、未终态复盘生成、审核中草稿、被 active 知识引用、冻结评测依赖 | 裁决 §2；预审 §2 U2 |
| U3 | 五类原始载荷置 NULL + `payload_state`（present/absent/cleaned）；`evidence.view` 保留；steps/inputs/runs 不清理 | 裁决 §2；预审 §1 第 3 项表、§2 U3 |
| U4 | 执行者为独立 CLI 进程 + 独立 `opspilot_retention` 角色，只授目标列的受限清理更新，无 DELETE/TRUNCATE/F13 写权限；web/worker/Observer 不增权。权限变更，PR-1 用户门 | 裁决 §2；预审 §2 U4、§3 |
| U5 | LangSmith 副本依赖平台 TTL（14/180 天），记为 C3 已知偏离，写进 C3 的文档 PR 交用户确认 | 裁决 §3；预审 §2 U5 |
| U6 | 默认 30 天；默认 dry-run，apply 显式开启；不提供关闭开关；无 close 时间的旧数据不动 | 裁决 §2；预审 §2 U6 |
| U7 | F13 步骤与 `passes` 不变；#244 单独验收 | 裁决 §2；预审 §2 U7 |
| U8 | 锁序 incident → run/session → postmortem/knowledge，锁后复查防竞态 | 裁决 §2；预审 §2 U8 |
| U9 | 报告、审计、知识 90 天本轮移出到 #374，记为 C3 已知偏离，交用户确认 | 裁决 §3；预审 §1 第 10 项、§2 U9 |
| P1 | 旧 Observer 行（raw 与 hash 皆空）迁移时回填 `absent`，不得标为 `cleaned` | 裁决 §2；预审 §4「本次 narrowed raw 形态草稿」 |
| P2 | 仅靠 `DEFAULT 'present'` 会破坏现有 raw=NULL 写入路径；须加插入触发器，或所有写路径显式写 `payload_state` | 同上 |
| R1–R6 | 可逆默认照预审：三态取值与回填（R1）；已清理证据 API 返回 200 + `cleaned`（R2）；F6 用 `STORED_OBSERVATION_DATA_CLEANED`、发布重放用 `REPLAY_DATA_CLEANED`，与「被篡改」区分（R3）；CLI 输出对象 ID、hash、原因与依赖（R4）；小事务批次、失败项单独记录（R5）；`RETENTION_GRANTS` 独立声明，不入 `RUNTIME_GRANTS`（R6） | 预审 §2 可逆默认 |

## 实施切分（均目标 main，不 stacked）

1. PR-1 schema + 角色 + 守卫（用户门：新数据库角色与权限、迁移 0023）：`closed_at`、raw 可空、`payload_state` 与回填、P2 写入路径、`opspilot_retention` 与 `RETENTION_GRANTS`、不可变触发器对清理更新的受限豁免。
2. PR-2 清理 CLI（dry-run/apply）：保留判定、锁后复查、close/reopen 竞态。
3. PR-3 页面与重放显示：evidence API 与 F3 页面、告警页、F6 重放、发布重放显示 cleaned/unknown。

## 执行进展与证据

- 2026-10-27：合同 r1 写入；#374（90 天归档）、#375（steps/inputs/runs 恢复合同）已开；#244 已贴 Agent Brief。

## 下一步与交接

- 派 PR-1 实现与独立测试作者，与 #245 PR-1 并行；全量 `tests/acceptance` 两条线错开跑（本机 swap 紧）。预审未确认项 1–8 由实现者按需核对，属可逆细节的写一行理由。
