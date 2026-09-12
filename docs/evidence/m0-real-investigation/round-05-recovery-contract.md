# M0-04/B4 真实恢复与迟到结果实验合同（待批准）

状态：**待用户批准，未执行。** 本合同只准备一次有界真实模型 + 真实 PostgreSQL 组合验证；当前任务不发送 HTTP、不上传 trace、不启动 `m0-otel`。旧账本、旧 allocation 和旧证据不复用。

## 统一前提与数据流

- 固定代码、`deepseek-v4-flash`、thinking/high、first-investigation-v4、Holmes checkout、OTel Demo 2.0.2 镜像/配置、工具 schema、scope/registry、PG schema 和 evaluator 版本/hash；每项使用独立 `run_id`。
- 操作者只提交调查问题和连接上下文；注入答案、私有协议字段、凭据不进入模型上下文、报告、LangSmith 或 judge。
- 每项最多 **2 个模型 HTTP**，每项费用上界 **2 CNY**（按 1 CNY/请求预留；实际费用以账单为准），总上界 6 CNY；工具查询仍受 per-Run/查询/时间预算约束。
- LangSmith 仅允许白名单字段：Run/step/request 标识、模型/代码/工具/schema 版本、状态、耗时、工具调用摘要和 evidence_id/hash 引用；禁止 prompt 中凭据、provider 私有 response/reasoning、原始业务私有字段。上传后只回读同一 Run 的白名单字段并做 hash 对账。

## 实验 A：首流程逐步骤恢复

- **问题**：模型响应已提交后 worker 进程退出，真实 PG 能否重建已提交 ModelStep/ToolOperation、保留 evidence/hash 和预算，并只重做未完成操作？
- **前提**：专属 PG 可达；真实 OTel/代理只读查询可达；先取得固定 fault/normal 窗和独立观察；不把模型自述当恢复事实。
- **步骤**：
  1. 启动专属 PG 与 `m0-otel`（仅获批后）；记录版本、端口、容器/卷状态。
  2. 以真实模型发起第 1 个请求，提交响应和工具计划到 PG 后立即杀掉 worker；保留进程退出码、PG 行快照和原始 evidence。
  3. 用新进程/同版本代码 claim 同一 Run，读取 PG 重建；断言已提交消息/工具结果不重采，未完成 operation 最多有界重做，旧预算/期限不重置。
  4. 完成或 handoff 后停止 Compose，再停止专属 Colima；不 `down`，保留卷/原始/raw/view/hash/ledger。
- **通过判据**：`IncidentScenario -> IncidentOutcome` 中 Run/owner/generation/step/request 一致；已提交响应和 evidence 原始时间/hash 保持；未完成项有明确重做/unknown；没有重复真实查询超出合同；LangSmith 白名单上传/回读与本地 hash 一致。
- **失败处置**：任何 PG 重建、身份/预算/hash 不一致均记录 `blocked`/`handoff`，保存原始失败；不自动重跑或扩大预算。

## 实验 B：取消后的迟到结果拒绝

- **问题**：模型请求发出后人工 cancel，旧 response/工具结果迟到时是否只保留审计历史、不覆盖新控制状态？
- **前提**：真实 Run 已有可控取消入口；PG 当前控制代次可回读；不执行任何被调查系统写操作。
- **步骤**：
  1. 发起第 1 个真实模型请求但延迟提交结果（最多 1 HTTP）。
  2. 由外部控制事务提交 cancel，推进 generation、清除 current final；记录控制事件/时间。
  3. 送入迟到 model response 或 ToolOperation 结果；验证 commit 被拒且原始 payload/hash 只进入审计。
  4. 回读 Outcome，确认 `cancelled`/handoff 与最终控制版本一致；停止并保全环境。
- **通过判据**：迟到提交返回确定性拒绝码；新 generation 不被旧 response 改写；无额外模型/工具请求；unknown 费用保留。
- **失败处置**：若旧结果被采纳，标记 P1 人控失败，保留所有 PG/审计/raw 证据，不重跑。

## 实验 C：版本不兼容 handoff

- **问题**：保留未完成 Run 后切换不兼容的模型协议、工具 schema 或业务版本，是否阻塞为 `blocked(INCOMPATIBLE_STATE)`，而不是静默续跑？
- **前提**：使用与实验 A 不同的 `run_id`；先保存原版本/hash 和 PG 快照；只加载预先声明的不兼容候选，不修改历史记录。
- **步骤**：
  1. 准备一个已提交但未完成的 Run（最多 1 HTTP）。
  2. 替换 provider/tool/schema 版本为不兼容组合，尝试重建。
  3. 断言领取/续跑被阻塞，原 Run、已提交 evidence、预算和控制记录保持不变。
  4. 用显式新 Run 继承允许的业务事实（不继承私有协议状态）并 handoff；保全后停止环境。
- **通过判据**：旧 Run 状态为 `blocked`，原因精确为 `INCOMPATIBLE_STATE`；没有新模型/工具调用；显式新 Run 的版本/hash/控制代次独立且可审计。
- **失败处置**：静默换版本、覆盖旧状态或继承私有协议均记录为 P1，保留原始数据库和错误输出。

## 共同停机与证据保全

仅在用户批准且实验完成后：导出安全摘要、raw/view/hash、PG 快照、usage/timing/trace 白名单对账；执行 `docker compose stop`（不 `down`），再 `colima stop --profile m0-otel`；核对端口关闭、卷/目录仍存在。任何环境前提不满足都返回 denied/unknown/handoff，不扩大范围。

**批准门**：需要用户单独确认本合同的用途、每项最多 2 HTTP、每项 2 CNY/总 6 CNY 上界、白名单 trace 出口和专属环境启停；确认前不执行。
