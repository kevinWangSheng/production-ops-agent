# #244 原始资料保留期清理（关闭后 30 天）

- 状态：进行中（合同 r2；PR-1 #377 实现完成，待独立验收测试与审查）
- 更新日期：2026-10-27
- 依据：[issue #244](https://github.com/kevinWangSheng/production-ops-agent/issues/244) Agent Brief；C3 §11（保留与删除）、§7（PostgreSQL 业务记录是恢复权威）；[F13 第 4 步合同](2026-10-15-f13-step4-contract.md) U7；[M1-03 记录](2026-10-05-m1-03-postmortem.md) D12；ADR-0006。功能 F13 相关，不改 F13 验收。
- 裁决：`~/.agent/handoffs/opspilot-rulings-2026-10-27.md` 第 2、3 节（下称「裁决」）；预审：`~/.agent/handoffs/opspilot-prereview-244-2026-10-27.md`（Codex gpt-6-astra→gpt-6-sol，r0.1，基线 `17bc865e`，下称「预审」）。
- 工作区：PR-1 `feature/F13-244-retention-schema`，`/Users/shenghuikevin/dev/AI/poa-244-impl`（起点 origin/main 17bc865e，PR #377）；独立测试 `chore/244-pr1-acceptance-tests`，`poa-244-tests`。

## 目标与范围

产品目前没有任何清理任务。本轮按 C3 §11「关闭后原始资料默认 30 天」做最小交付：清理五类原始载荷——`opspilot_evidence.raw`、`opspilot_observation_signal_readings.raw`、`opspilot_release_query_results.raw`、`opspilot_release_comparison_queries.raw`、`opspilot_alert_deliveries.alert_json`。原列置 NULL 并记 `payload_state`，行、哈希、时间与 `evidence.view` 保留。

不在范围：`opspilot_steps.response/tool_results`、`opspilot_inputs.content`、`opspilot_runs.input`（恢复合同 #375）；报告、审计、知识 90 天或归档（#374）；LangSmith 外部副本删除（归 #243 后续）。

## 前提与完成条件

- 前提：`main` `17bc865e`；迁移编号用 0023（#245 PR-1 先占 0022，本项 PR-1 等其合并后再合）。
- 完成条件：PR-1–PR-3 合并，#244 独立验收（独立测试作者编写）通过、独立审查无未处置发现。F13 步骤与 `passes` 不变。不需要 kind。

## 合同（r2）

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
| r2-L1 | 行锁需要表上至少一列 UPDATE：`opspilot_retention` 对 incidents、runs、observation_sessions 各授一列 lock-only UPDATE，配守卫触发器拒绝其任何实际 UPDATE（42501）；F13 表无锁或写权限，靠事故行锁 + 锁后复查（新增知识引用都要经过本事故 under_review 版本，生成与批准路径先锁事故行；论证见 PR #377） | lead 2026-10-27，按 0011 lock-only 先例 |

## 实施切分（均目标 main，不 stacked）

1. PR-1 schema + 角色 + 守卫（用户门：新数据库角色与权限、迁移 0023）：`closed_at`、raw 可空、`payload_state` 与回填、P2 写入路径、`opspilot_retention` 与 `RETENTION_GRANTS`、不可变触发器对清理更新的受限豁免。
2. PR-2 清理 CLI（dry-run/apply）：保留判定、锁后复查、close/reopen 竞态。
3. PR-3 页面与重放显示：evidence API 与 F3 页面、告警页、F6 重放、发布重放显示 cleaned/unknown。

## 执行进展与证据

- 2026-10-27：合同 r1 写入；PR-1 实现开为 #377，实现中追加 r2-L1；#374（90 天归档）、#375（steps/inputs/runs 恢复合同）已开；#244 已贴 Agent Brief。

- 独立验收测试：Codex（gpt-6.1-sol 起，容量中断后 gpt-6-astra 续跑），`tests/acceptance/test_244_pr1_retention_schema_roles.py` 7 例；main 上 6 红 1 绿，本分支 7 绿。lead 修了两处测试构造错误（TRUNCATE 探针误传参数、F13 写入探针的 object_id 用了文本），并按实现裁定 `RETENTION_GRANTS` 为单一角色「关系 → 权限」映射。
- 独立审查：Codex astra/sol 均容量满，按用户 10-26 规则回落全新上下文 Claude Fable 5.1；两轴 0 阻塞、6 可选（另跑 23 条 SQL 越权探针，结果均符合合同）。处置：development.md 的 r1/r2 措辞已改；其余记为已知：retention 空更新 `SET raw=raw` 在运行时三表放行（无数据影响）、迁移外删 `opspilot_retention` 角色会使三表 UPDATE 报错（运维边界）、INSERT 即 closed 也写 `closed_at`（产品无此路径）。
- 改接 0022（#378 合并后）：0023 `down_revision` 改为 `0022_incident_association`，非验收 head 钉值由实现者更新。三个验收文件的钉值由 lead 按合同变更改（00fe27a2、测试分支下一提交）：#347 head 改 0023（`RUNTIME_GRANTS` 指纹不变）；#244 `BEFORE_RETENTION` 改 0022（0022 的 web 新授权本就不属 0023）；#245 AC8 head 改 0023，拒绝降级后停在 0022（迁移逐个提交，0023 先退回，0022 再拒绝）。三文件 27 例在本分支通过。
- 合并顺序（lead 2026-10-27，可逆执行细节）：读侧 PR-3 先于清理 CLI PR-2 合并，保证出现 cleaned 行之前，evidence API 与工具层已能处理 `raw=None`（#377 机器人 P1 指出 `opspilot/web/evidence.py` 对 cleaned 证据会抛异常；PR-1 无产品代码写 cleaned，故不在 PR-1 修）。
- PR-3 必须覆盖（审查预警）：`hashes_verified=None` 在图表投影被当作 `hash_mismatch`，工具层证据读取对 `raw=None` 会 `AttributeError`。
- PR-3（#382）已知限制（lead 裁决 2026-10-27）：告警已清理的事故 reopen 后开新 Run，若需重建告警上下文（`_alert_rebuild`：续开输入回退路径与 #245 改绑路径），仍按 `INCOMPATIBLE_STATE` 拒绝该控制动作，仅日志区分「cleaned」与「unreadable」。理由：只在关闭满 30 天被清理后又 reopen 再开 Run 时发生；改为无告警上下文起 Run 会改调查输入构建，需有界真实 Run，超出读侧范围；fail-closed 可逆。工具层证据读取经核对无 `AttributeError`（`_intact()` 已把 `raw=None` 拒为 `EVIDENCE_NOT_READABLE`），PR-3 以测试钉住。
- PR-3 独立审查（Codex gpt-6-astra）阻塞 1 处置：读侧一律以 `payload_state == 'cleaned'` 判定已清理（evidence API/页面、图表、告警页与告警上下文日志、恢复读数、F6 均改为看状态字段，告警投递行读出 `payload_state`）；`raw` 为空但状态非 cleaned 时不显示已清理、不画图（`hashes_verified=None` 即 hash_mismatch），API 只省略 raw 字段。`_impl` 测试先红后绿。
- PR-3 独立审查阻塞 2 处置：发布重放不再遇 cleaned 提前返回；被清理样本只跳过重算与指纹，改用存储的评估参与折叠，发布/样本/审计/结束/对比绑定等校验照常执行，篡改与 `REPLAY_DATA_CLEANED` 并存、结论 unknown；对比仅 cleaned 时另校验存储记录的状态与时间顺序。F6 侧同理补了 cleaned 哨兵读数。`_impl` 测试（伪造 release.status 与样本 outcome）先红后绿。
- PR-3 复验阻塞处置：已清理的 profile-unavailable 哨兵读数仍校验保留的 `query` 等于会话 profile revision，不一致报 `SENTINEL_MISMATCH:query`（与 `STORED_OBSERVATION_DATA_CLEANED` 并报 INTEGRITY_MISMATCH）；只跳过需要 bundle 的校验。`_impl` 测试先红后绿。

## 下一步与交接

- 派 PR-1 实现与独立测试作者，与 #245 PR-1 并行；全量 `tests/acceptance` 两条线错开跑（本机 swap 紧）。预审未确认项 1–8 由实现者按需核对，属可逆细节的写一行理由。
