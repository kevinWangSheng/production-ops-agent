# #244 原始资料保留期清理（关闭后 30 天）

- 状态：PR-1 #377、PR-3 #382、PR-2 #388（squash ad33c86c，2026-10-28）已合并；收尾文档 PR #389 含 C3 §11 两处已知偏离，用户 2026-10-28 确认，合并后关闭 #244
- 更新日期：2026-10-28
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

### PR-2 合同补充（r3，lead 2026-10-28，均为「多保留」方向的可逆默认，依据见各行）

| 编号 | 要点 | 依据 |
|---|---|---|
| r3-A 归属 | 五类行按下面的方式归到事故：evidence → `run_id` 所属 Run 的事故；恢复读数 → 观察会话的事故；告警投递 → `incident_id`；发布查询结果 / 发布对比查询 → `opspilot_release_incident_links` 的 `incident_id` 与 `related_incident_id`（经样本 / 对比找到发布观察）。一行只在它归属的**全部**事故都可清理时才清理；归属不到任何事故的行（`incident_id` 为空的告警投递、未关联事故的发布观察）不清理 | 合同 U1「无 close 时间的不动」同向保守；0021 只在异常被消费时建立事故关联 |
| r3-B 合并 | 按 `merged_into_incident_id` 指针把被并入事故与保留事故视为一组：组内任一事故不满足清理条件，整组都不清理 | #245 合并语义：被并入事故的告警与发布已挂到保留事故 |
| r3-C 评测依赖 | 已核对：评测资料是仓库文件（`evals/`），PG 中没有引用事故、Run 或证据的评测表，「冻结评测依赖」在 PG 侧无可阻挡的对象；本轮不写判定、不写测试。将来评测引用 PG 行时再加 | `grep opspilot_ opspilot/evaluation/` 无表；C3 §11「评测资料独立归档」 |
| r3-D 接口 | 见下「清理 CLI 公开接口」 | 可逆技术细节，lead 钉死以便独立测试作者与实现者对齐 |
| r3-D 补充 | `failed` 行的 `error` = `"<异常类名>: <主消息>"`，psycopg 错误取 `diag.message_primary`；不含 detail/context/hint，避免行值进运维日志 | lead 2026-10-28，独立验收 AC4 |

#### 清理 CLI 公开接口（r3-D）

- 进程入口：`python -m opspilot.retention [--apply] [--days N]`。DSN 只读环境变量 `OPSPILOT_RETENTION_DSN`（以属于 `opspilot_retention` 的 login 连接）；未设置时退出码 2。另加 `make retention`（dry-run）与 `make retention-apply` 两个运维入口，与 `make migrate` 同级。
- 天数：默认 30；`N < 30`（含 0、负数、非整数）退出码 2，stderr 写 `RETENTION_DAYS_INVALID`，零写入。没有关闭开关。
- 库函数（测试接缝）：`opspilot.retention.run_retention(dsn: str, *, days: int = 30, apply: bool = False, after_scan: Callable[[], None] | None = None) -> RetentionReport`。`after_scan` 在扫描出候选之后、对任何事故加锁之前调用一次（竞态测试用）。`days < 30` 抛 `ValueError("RETENTION_DAYS_INVALID")`。
- `RetentionReport`：`lines: tuple[dict, ...]`（即下面的 JSON 行）、`failed: int`。
- 输出：stdout 每行一个 JSON 对象，三种：
  - `{"kind": "blocked", "incident_id": str, "blocked_by": [str, ...]}`：仍有 `present` 载荷的事故，及被阻挡合并组的全部成员（各列自己的原因；本身无阻挡的成员为 `MERGE_GROUP_BLOCKED`）。`blocked_by` 取值：`NOT_CLOSED`、`CLOSED_AT_MISSING`、`WITHIN_RETENTION`、`ACTIVE_RUN`、`ACTIVE_OBSERVATION`、`POSTMORTEM_GENERATION_PENDING`、`DRAFT_UNDER_REVIEW`、`ACTIVE_KNOWLEDGE`、`MERGE_GROUP_BLOCKED`、`CHANGED_AFTER_SCAN`（锁后复查不再满足）。
  - `{"kind": "candidate" | "cleaned" | "failed", "incident_id": str, "table": <五个表名之一>, "object_id": str, "raw_sha256": str | null, "error"?: str}`。`object_id`：evidence 为 `evidence_id`；读数 `"<sample_id>:<signal_name>"`；发布查询 `"<sample_id>:<query_key>"`；发布对比 `"<comparison_id>:<query_key>"`；告警投递 `str(delivery_id)`。dry-run 只出 `candidate`；`--apply` 出 `cleaned` / `failed`。
  - 最后一行 `{"kind": "summary", "apply": bool, "days": int, "candidates": int, "cleaned": int, "failed": int, "blocked": int}`。
- 退出码：0 无失败；1 有 `failed` 行；2 配置错误。
- 批次：一个事故（r3-B 为一组）一个事务；锁序 incident（按 id 升序）→ run / session → postmortem / knowledge；锁后复查全部条件，不满足输出 `blocked` + `CHANGED_AFTER_SCAN` 且不写；单行更新失败记 `failed`，同批其余事故照常提交。

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
- PR-3 清扫复核 5 条处置（发布重放 cleaned 分支）：把计划校验中不读响应体的部分抽成 `query_plan_consistent`（正常路径的 `query_evidence_consistent` 也经它）；样本 cleaned 时每行（含 cleaned 行，按 DTO 去 body 校验）仍过计划/窗口/decision_at 校验，实例取存储评估；对比 cleaned 时仍校验 query 行元数据与 before 计划、`window_before` 与冻结 profile、attempts 1–3、存储记录的 comparison/observation/ending/sample/format_version/attempts/status/window/after_instances 与外层行和冻结事实一致。篡改与 `REPLAY_DATA_CLEANED` 并报。5 项 `_impl` 测试先红后绿。
- PR-3 复验 2 条处置：① 对比 cleaned 时核对存储的 after 信号与 `AFTER_EVIDENCE_INSUFFICIENT`（由保留的异常样本评估推出，抽成 `after_scope_signals`/`after_insufficient`，正常路径共用）。② `QUERY_EVIDENCE_INCONSISTENT` 不再短路计划校验；边界：该码可由映射原文决定的实例集产生（切片 key 随实例而定），cleaned 后无法从元数据反证，故不做精确等价——无码时计划必须成立，有码时记录必须呈现其后果（无 before 实例与取值、`unavailable`），且不依赖实例的行仍须符合计划。4 项 `_impl` 测试先红后绿。
- PR-3 二次复验处置（逐字段核对 cleaned 对比记录）：可由保留元数据、冻结 profile、保留异常评估推出的都比对——记录身份/窗口/attempts/after 实例、信号列表的 scope 与顺序、after 信号全字段、before 信号的 scope/name/query_keys（已评估时为固定的每信号 key，未评估时为空）/作用域 reason_codes、`AFTER_EVIDENCE_INSUFFICIENT` 与 status 推导（抽成 `before_signal_frame`/`comparison_status`/`profile_usable`，正常路径共用后两者）。边界（需原文/实例集，cleaned 后不可知）：before 信号的 value/sample_count/latest_sample_at、无码时的 `before_instances`、`QUERY_EVIDENCE_INCONSISTENT` 本身及其他质量码是否该出现（只核对内部一致）。5 项 `_impl` 测试先红后绿。
- PR-3 机器人 P2 处置：表上不约束 cleaned 行保留的 `raw_sha256` 形态；F6 cleaned 读数要求 64 位小写 hex，否则 `RAW_HASH_MISMATCH:<name>`（与 DATA_CLEANED 并报 INTEGRITY_MISMATCH）；cleaned 证据同理 `hashes_verified=False`，图表与验收引用解析不再放行。`_impl` 测试先红后绿。
- PR-3 机器人分诊（lead 2026-10-27）：同一样本或对比中部分查询已清理时，仍保留原文的行照常重算哈希，被篡改报 `RAW_SHA256_MISMATCH`/`COMPARISON_MISMATCH`（与 `REPLAY_DATA_CLEANED` 并存）；另 3 条已由 7d49eb78 覆盖。改动小，lead 自核：`_impl` 测试先红后绿，重放相关 220 条通过。
- PR-3 机器人对 8076e164 新提 1 条（采纳）：F6 有已清理读数的样本仍把每行保留的信号名、查询、来源、时间窗与冻结 profile 核对，只跳过依赖原文的检查；`_impl` 测试先红后绿，F6/F7/F11 相关 333 条通过。
- PR-3 机器人对 00853272 新提 1 条（采纳）：对比查询行属于本对比的绑定校验提到 cleaned 分支之前；`_impl` 测试先红后绿，发布重放相关 332 条通过。
- PR-3 机器人对 0fa60d24 新提 1 条（lead 采纳并修）：恢复读数 API 对已清理读数的畸形保留哈希返回 `hashes_verified: false`，与证据 API 和重放一致；absent 读数不变。`_impl` 测试先红后绿，Web/证据相关 392 条通过。
- PR-2 实现（`opspilot/retention.py`，`make retention` / `make retention-apply`）可逆决定：「复盘生成待定」= 生成尝试 `running`、生成租约未过期或退回后待重新生成（`pending_regeneration_version` 非空）；「活跃 Run」= `close` 会取消的 5 个状态；「活跃观察」= 会话 `authorized`；事务单元为合并组，并与跨组归属行（发布关联 `incident_id` + `related_incident_id`）所涉的组合并；多事故归属行的输出 `incident_id` 取归属中 id 最小者；锁后复查失败时对该单元涉及的事故各出一行 `CHANGED_AFTER_SCAN`。`RETENTION_GRANTS` 未改。
- PR-2 独立审查（Claude Fable 5.1，Codex 容量满）0 阻塞，2 可选采纳：① 单元级错误（加锁/复查的锁超时、死锁、断连）回滚该单元，每个事故一行 `failed`（`table`/`object_id`/`raw_sha256` 为 null，`error` 同规则），继续下一单元；连接已断则停止但仍输出 summary。② 被阻挡合并组的全部成员都输出 `blocked`（r3-D 措辞已改）。`_impl` 测试先红后绿。
- PR-2 lead 裁决（不改代码）：「复盘生成待定」保持窄定义（running 尝试、未过期租约、待重新生成），生成只读 view 与元数据、不读 raw（`jobs.py:567-571`），清理不影响之后的生成；F13「未终态」即非终态尝试。
- PR-2 lead 裁决：`GenerationStore.claim` 不锁事故行，可能落在复查与提交之间；同理无害（生成不读 raw）。
- PR-2 lead 裁决：发布观察会话不计入 `ACTIVE_OBSERVATION`；产品中不可达（关联需要 ending，已结束的发布会话不会再变）。
- PR-2 小清理：合并组与事务单元共用一个 union-find（`_Sets`），去掉 `reasons.get(m, ["NOT_CLOSED"])` 的死哨兵。
- PR-2 机器人 P2（b20805f2，采纳）：合并组中已无 present 行的成员不在组映射里，apply 建单元时 `KeyError`（部分清理后重试即可触发）；`_merge_groups` 改为给组内全部成员建键。`_impl` 测试先红后绿。

## 下一步与交接

- PR-2 收尾：独立验收 22 例（Codex，main 上 21 红）全部通过；独立审查（Codex 容量满，回落全新上下文 Claude Fable 5.1）0 阻塞、可选 7 采纳 2；机器人 P2 1 条采纳（部分清理后重试的 KeyError）。最终 33 例通过、CI 绿。
- 本文档 PR：C3 §11 写入当前实现与两处已知偏离（90 天归档 #374；外部平台副本依赖平台保留期，#243），ROADMAP F13 行更新。用户 2026-10-28 确认，合并后关闭 #244。
- 延后项各有 issue：#374（90 天归档）、#375（steps/inputs/runs 恢复合同）、#243（outbox 与外部副本删除）。
