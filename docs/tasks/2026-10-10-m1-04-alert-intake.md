# M1-04 Alertmanager 告警接入

- 状态：待开始（授权与 r1 已定；合同预审未开始）
- 更新日期：2026-10-10
- 依据：[#167](https://github.com/kevinWangSheng/production-ops-agent/issues/167)；C3 §6「事件接收、去重与持久调度」、§9「页面、认证与人工操作」；PRODUCT-CONSTRAINTS「Evidence and context requirements」；feature_list F1 第 2 步（duplicate-event、wrong-target）、F7 第 5 步（审计重建）；SPEC 2026-10-10 段
- 工作区：授权 PR `chore/m1-04-alert-intake-gate`，`../production-ops-agent-alert-intake`；实施每步一个 worktree，写在执行节

## 目标与范围

事故入口目前只收 `target_id` + 自由文本问题，告警的结构化信息（服务、命名空间、严重级别、开始时间、PromQL）全部丢失，时间框锚定提交时刻。本切片新增 Alertmanager webhook 入口，保留结构化告警，按标签确定性解析目标，时间框锚定 `startsAt`。范围与不在范围内的项见 #167 正文；C3 §14 把「两个入口」排在 M2，本切片是用户提前开放的一部分。不翻任何 `passes`。

## 合同（r4）

用户 2026-10-10 按推荐裁决 #167 的待决 A–D：

- **A 目标解析不到**：接收（2xx），建事故但不绑目标，直接交接给人，不建 Run。不回 422：Alertmanager 一次投递一组告警，422 会连带拒掉同组可解析的告警。
- **B 重复通知、annotation 变化**：只按身份 `fingerprint:startsAt` 判重放，记录新值；不报 `INTAKE_KEY_CONFLICT`，不建新事故。理由：annotation 模板常含 `$value` 等易变值。
- **C resolved 告警**：只记录，并附到已有事故上供人查看；不建事故、不建 Run、不开恢复观察。与 #184 解耦，#184 若改由 resolved 触发观察，另行变更本条。
- **D 服务层**：受影响服务（`namespace` + `workload`）从标签确定性解析，只作焦点信息（Run 输入、工作台显示、登记处置预填观察对象）；不收窄工具授权，模型仍可查同一集成内其他服务。理由：尚无依赖图。

r2：第 1、2 步预审 E1–E15 用户 2026-10-10 全部按推荐采纳，R1–R12 按推荐作可逆默认；完整表见[合同决定表](2026-10-10-m1-04-alert-intake-contract.md)。

- **E1** 组内任一条持久化失败回 5xx，已成功条靠幂等重放；Alertmanager 2xx 成功、5xx 重试、其他不重试（`notify/util.go:189-227`）。
- **E2** `startsAt` 规范为 UTC 秒级，另存原始值。
- **E3** 告警身份索引：新表或 ledger + 唯一索引，与事故原子关联。
- **E4** Alertmanager 专用比较器，`/intake/events` 行为不变。
- **E5** 新增「仅交接、无 Run」事故语义（实现 A；含迁移与工作台展示）。
- **E6** 目标注册表加版本化 `match_labels`。
- **E7** 多目标命中视为歧义，走 E5 交接。
- **E8** resolved 只按同 `fingerprint:startsAt` 精确关联；找不到仅记录。
- **E9** annotation 变化追加版本事件，索引最新值。
- **E10** 原始 payload 脱敏后限长 JSON + 原始 hash。
- **E11** 受影响服务写入 `scope_facts.affected_service`，输入快照版本递增。
- **E12** 验收投影新增逐条告警结果与审计引用。
- **E13** Alertmanager 专用 token/actor，经 `credentials_file` 配置。
- **E14** lab webhook 路径明确且可验证（NodePort/宿主回环）。
- **E15** 独立、版本固定的 Alertmanager 部署并记录版本。

## 第 2 步公开接口（r3，lead 按可逆默认定，2026-10-10）

实现与测试都以本节为准；内部表名、模块拆分、SQL 由实现者自定。冲突时以 r1/r2 合同为准并上报 lead。

- **I1 端点**：`POST /intake/alertmanager`，只接受 `event_token` 通道（现有 `event_channel`）；UI 会话 401。请求体为 Alertmanager webhook v4 对象。整体无法解析、非对象、`version != "4"`、`alerts` 不是列表 → 400，`{"error": "INVALID_ALERTMANAGER_PAYLOAD"}`，不写任何记录。
- **I2 逐条处理与响应**：按 payload 顺序逐条处理，每条告警一个事务（R5、E1）。全部可持久化 → 200；任一条持久化失败 → 503，响应体相同（已成功的条目照常列出；Alertmanager 重试时它们按 I3 判为重放）。响应体：`{"results": [AlertResult, ...]}`，`AlertResult` = `{"fingerprint", "starts_at", "status", "outcome", "incident_id", "run_id", "delivery_key", "reason"}`：
  - `status`：`firing` | `resolved`（取告警自身的 `status`）。
  - `outcome`：`created`（新事故 + Run）| `replayed`（同身份已存在，含 annotation 变化）| `handoff_created`（E5/E7：仅交接事故，无 Run）| `handoff_replayed` | `resolved_attached`（E8：附到同身份事故）| `resolved_recorded`（找不到同身份事故，只记录）| `invalid`（R2：缺 fingerprint/startsAt/labels、startsAt 无法解析或无时区；`reason` 写原因码，不建任何记录，整体仍 200，因为重试修不好它）| `failed`（持久化失败，`reason` 写原因码；整体 503）。
  - `incident_id` / `run_id`：字符串或 `null`（仅交接与 resolved_recorded 时 `run_id` 为 `null`）。
- **I3 身份与去重**（E2、E4）：`starts_at` 规范为 UTC 秒 `YYYY-MM-DDTHH:MM:SSZ`（截断小数）；原始字符串另存。投递键 `delivery_key(source="alertmanager", external_event_id=f"{fingerprint}:{starts_at}")`。比较器为 Alertmanager 专用，`/intake/events` 与 `classify_intake_delivery` 行为不变。并发首投同一身份只产生一个事故（E3：唯一约束）。
- **I4 目标注册表**（E6）：`OPSPILOT_TARGET_IDENTITIES` 每个条目可选新增 `"match": {"version": 1, "labels": {<非空 str>: <非空 str>, ...}}`（至少一个标签；其他 version 或格式在加载时拒绝，与现有严格校验一致）。告警的 `labels` 包含某条目 `match.labels` 的全部键值即命中。恰好一个条目命中 → 绑定该条目的 `resource_uid` 为 `target_id`；零个或多个 → 仅交接事故（E5、E7），`reason` 为 `TARGET_UNRESOLVED` 或 `TARGET_AMBIGUOUS`。没有 `match` 的条目不参与告警匹配。
- **I5 受影响服务**（D、E11）：命中条目的 `namespace` + `workload` 写入 Run 输入 `scope_facts["affected_service"] = {"namespace": ..., "workload": ...}`，输入快照版本递增；不改变工具授权（`QueryScope.target_ids` 不变）。
- **I6 问题模板**（R3、R4）：确定性生成，只用告警名（`labels.alertname`）、`labels.severity`、命中条目的 namespace/workload、规范化 `starts_at`、从 `generatorURL` 的 `g0.expr` 解出的 PromQL（无法解析时省略）。**annotation 不进问题**（只在第 4 步作为不可信上下文）。每个字段限长 256 字符、PromQL 限长 2048，超长截断并在问题中标注截断；同一输入得到逐字节相同的问题。
- **I7 审计**（E9、E10、R7）：每次投递（含重放、仅交接、resolved、invalid 之外）追加一条告警记录：脱敏（`redact_credentials`）且限长（单告警 JSON ≤ 16 KiB，超出截断并标记）的告警 JSON + 原始告警 JSON 的 sha256 + 接收时间 + actor。annotation 变化追加新版本、不覆盖旧版本。
- **I8 验收投影**（E12）：新模块 `opspilot/acceptance_alert.py`，从 `opspilot.acceptance` 再导出，模式同 `acceptance_recovery`：
  - `alert_intake_records(conn, incident_id) -> AlertIntakeRecords`：读该事故已提交的告警投递记录、事故行与 Run 行。
  - `alert_intake_outcome(scenario, records) -> AlertIntakeOutcome`：`scenario.subject_id` 为事故 id，不一致 `ValueError("SUBJECT_MISMATCH")`。
  - `AlertIntakeOutcome` 字段：`scenario_id`、`incident_id`、`target_id: str | None`、`run_id: str | None`、`handoff: bool`、`handoff_reason: str | None`、`affected_service: tuple[str, str] | None`（namespace, workload）、`deliveries: tuple[AlertDelivery, ...]`（按接收顺序）。
  - `AlertDelivery` 字段：`fingerprint`、`starts_at`、`status`、`outcome`（I2 取值，不含 `invalid`/`failed`）、`actor`、`raw_sha256`、`annotation_revision: int`（同身份第几个 annotation 版本，从 1 起）、`truncated: bool`。
- **I9 工作台**：事故页展示告警身份、受影响服务、annotation 最新版本（以不可信文本转义渲染）与 resolved 记录；仅交接事故出现在列表中并显示交接原因。页面与 I8 投影同源，不另算。
- **I10 配置**：工作台以现有 `OPSPILOT_EVENT_TOKENS` 认证 Alertmanager（专用 actor 由运维配置，如实验环境的 `alertmanager`）；不新增环境变量。

### r4 补充（lead 按可逆默认定，2026-10-10；起因：独立测试作者 5 个 xfail 缺公开读取口）

- **I8 扩展**：
  - `AlertIntakeOutcome` 增加 `lifecycle: str`（事故 lifecycle）、`run_ids: tuple[str, ...]`（该事故全部 Run，按创建顺序）、`observation_session_ids: tuple[str, ...]`（该事故全部观察会话）、`run_input: Mapping[str, Any] | None`（接收时建的 Run 的已提交输入快照，原样：含 `question`、`scope_facts`、绑定目标与时间框字段；仅交接时为 `None`）。
  - `AlertDelivery` 增加 `received_at: str`（UTC ISO-8601）、`original_starts_at: str`（告警原文的 startsAt）、`audit_json: str`（已提交的脱敏、限长审计文本，原样）。`raw_sha256` 定义为 `opspilot.tools.registry.canonical(alert)` 的 UTF-8 字节的 sha256，`alert` 为 payload 中该条告警解析后的 JSON 对象。
- **I11 全局读取**（与事故无关）：`alert_deliveries_for_identity(conn, fingerprint, starts_at) -> tuple[AlertDelivery, ...]`（按接收顺序；`starts_at` 用 I3 规范形式；孤立 resolved 也能读到）与 `alert_delivery_count(conn) -> int`（已提交投递记录总数，用于断言 invalid/认证失败/400 不写记录）。两者从 `opspilot.acceptance` 再导出。
- **I12 失败注入**：表名 `opspilot_alert_deliveries` 是测试接缝。测试可在该表上装 PostgreSQL 触发器，对指定 fingerprint 的 INSERT 抛异常，模拟逐条持久化失败；端点须把它记为该条 `outcome=failed`（`reason` 非空）、回滚该条事务（不留身份、事故、Run）、整体 503，其他条照常提交。

## 计划

每步一个 PR，开工前做该步合同预审。

1. 实验环境：kind lab 加 Alertmanager、一条 checkout 错误率规则、带 bearer token 指向工作台的 webhook（chore）。
2. `POST /intake/alertmanager`：webhook v4 解析（告警模型参照上游 HolmesGPT `PrometheusAlert`，`46e3a72`）、逐条处理、去重（B）、目标与服务解析（A、D）、确定性问题模板、resolved 记录（C）、原始 payload 限长入 ledger。功能 PR，用户门。
3. 时间框锚定 `startsAt`：授权框覆盖 `[startsAt - 回看, now]`；输入快照与投影 revision 变化按既有规则处理。
4. 原始告警作为不可信上下文交给模型：`redact_credentials` + 长度上限；同步核查首轮 `question` 未脱敏（`opspilot/investigation/context.py`）。

验收与合同测试由未参与实现的全新上下文 Agent 按 #167 与 C3 §6 编写，覆盖重复通知、错误目标、组内多告警、resolved、超大或恶意 annotation。触碰调查 loop 的步骤（3、4）附有界真实 Run 的 trace 与 `summary.json`。

## 执行进展与证据

- 2026-10-10：授权 PR（SPEC 段、ROADMAP 行、本记录）。

- 2026-10-10：第 1 步实验环境（`chore/m1-04-lab-alertmanager`，`../production-ops-agent-lab-alertmanager`）：独立 Alertmanager release 1.24.0 / v0.28.1、checkout 错误率规则、经 `credentials_file` 带 bearer 的 webhook、Prometheus 配置哈希滚动。真实运行：firing 与 resolved 两次 webhook 送达宿主，bearer 匹配 `alertmanager` actor，[证据](../evidence/m1-04-lab/run.md)。实验环境动作：otel-collector 重启一次（计数器停滞）。
- 2026-10-10：第 2 步（`feature/m1-04-alertmanager-intake`，`../production-ops-agent-alert-intake-impl`）。三条并行线：实现（Claude Opus 5.5 子 Agent）、独立验收与合同测试（Codex，只依据合同与 r3/r4 接口，未见实现）、第 3、4 步预审（Codex 只读）。独立测试先暴露公开读取口缺口 → lead 定 r4（I8 扩展、I11、I12）；测试侧 3 处缺陷由测试作者修（DDL 绑定参数、harness 缺 tool face、跨投递比较 deadline），实现未为测试改断言。独立审查（Codex）P1 失败条目残留目标登记、P2 未知输入版本应为 `INCOMPATIBLE_STATE`，均已修，复验无新 P1/P2。检查：`make check` 3568 passed；PG 集成 545 passed；其余 PG 3697 passed；独立测试 45 passed。真实端到端（新上下文 Agent）：Alertmanager → `created` → 真实 deepseek-flash Run 发布报告 → resolved `resolved_attached`、lifecycle 不变，[证据](../evidence/m1-04-intake-live/run.md)，余额差 ≤ 0.15 CNY。过程失误：lead 曾把独立测试文件复制进实现 worktree 跑测试（实现者称未打开）；一次 Codex 续跑未带 `-C`，把测试写进主仓库，已移回，用户 WIP 未动。

## 下一步与交接

- 授权 PR 合并（用户门）。
- 合同预审：未参与实现的 Agent 只读核对真实代码，按九类列决定表，写入 `2026-10-10-m1-04-alert-intake-contract.md`，用户裁决为 r2。
