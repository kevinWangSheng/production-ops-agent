# M1 退出：从干净环境起的一体化端到端运行

- 日期：2026-10-15 06:39–08:24Z；分支 `chore/m1-exit-e2e`（起点 `main` `8ff4a23`，本分支不改 `opspilot/`）
- 依据：[M1 退出决策 D1-A](https://github.com/kevinWangSheng/production-ops-agent/blob/chore/m1-exit-decision/docs/tasks/2026-10-14-m1-exit-decision.md)（PR #236，未合并时以该分支为准）：空库迁移 → 非 owner 角色启动 web/worker/Observer → kind 注入故障 → Alertmanager 告警 → 调查发布结论 → 恢复故障 → 自动恢复观察 → 复盘草稿 → 工作台批准；结果可解释即算数，失败如实记录，不为通过重跑，不翻任何 `passes`
- 产品代码：告警、调查、观察三段跑在 `8ff4a23`（与 `e1b8867` 相比只差文档，`git diff --stat e1b8867 8ff4a23` 只有 3 个 `docs/` 文件）；复盘重新生成跑在 PR #240 的 HEAD `95ee1bb`（合同 r12，未合并，见下文「复盘」）
- 冻结摘要：[run/summary.json](run/summary.json)（`frozen_at` 08:24:26Z，含 collect/verify/approve 与 HealthProfile 的 sha256，`artifacts`）；批准记录 [run/approve.json](run/approve.json)。原始 ledger 不入库，本机 `tmp/lab-ledgers/m1-exit-e2e/98f27b4d-…/ledger.json`（sha256 `d1b08747…a3aca7`，2,717,446 B，与摘要 `raw_ledger` 一致）
- 判据：[verify.py](verify.py) 与 [#184 第 3 步的 verify.py](../m1-184-self-heal-live/verify.py) 逐字相同（独立 Codex 按合同编写），本次只用其 S1 判据；取证 [collect.py](collect.py) 由 #184 版本改名重定向（只改场景名、round 与 artifacts 列表）；批准 [approve.py](approve.py) 以工作台 Basic Auth 审核人身份提交页面上的批准动作并只读回读
- 类别：有界真实软件环境运行 + 真实模型调用（DeepSeek `deepseek-flash`）。故障只由 kind lab 的 flagd 钩子注入与恢复，告警由真实 Prometheus/Alertmanager 产生；不是生产证明

## 环境

- 新起 PostgreSQL 17 实例 127.0.0.1:55571（库 `m0_budget`），`make migrate` 到 `0016_release_deadline_evidence`（摘要 `versions.schema_head`）；告警投递 `delivery_id` 从 1 开始。「空库」本身由 lead 记录，冻结证据只能间接佐证（未确认）。
- 三个产品进程用非 owner 登录角色：web `opspilot_web_lab`、worker `opspilot_worker_lab`、Observer `opspilot_observer_lab`（见启动脚本，下文哈希）。Observer 以 `env -i` 启动，只带自己的 DSN、Prometheus 地址与 Observer 账号文件；实测权限 `read_only` + `database_temp`（后者是 PUBLIC 默认 `TEMP`，同 #184）。
- web：`opspilot.web serve`，绑定 127.0.0.1:8080，工具 profile `otel-demo`，HealthProfile 为随产品发布的 `otel-demo-checkout.json`（revision `otel-demo-checkout@b72bbe2e30be`：采样间隔 60 s、最多 40 次、持续健康窗 600 s、期限 3600 s）；自动观察循环用默认间隔。
- worker：`opspilot.worker_main`，`OPSPILOT_TRACE=lab`；trace project `opspilot-lab-m1-exit-e2e-20261015`（`trace_tier=longlived`）。
- kind `opspilot-m1`（OTel Demo chart 0.37.8）。实验环境占用 06:38Z 声明、07:30Z 释放，为 lead 记录，不在冻结证据中（未确认）。

## 时间线

库内时间取自冻结摘要；watch 时间是 [watch 脚本](#原始文件) 每 10 s 轮询看到状态变化的时刻，晚于库内时间几秒。

| 时间（UTC） | 事件 |
|---|---|
| 06:39:10 | 余额读数 `before`（200.39 CNY） |
| 06:39:46 | `fault inject`：flagd ConfigMap `1ee5c025…` → `e47d27c2…`，`verified: true`（paymentFailure 100%） |
| 06:45:53 | Alertmanager webhook firing（`startsAt` 06:44:54，fingerprint `83f7541ae0105d42`）→ `created`，事故 `d40e777a-…`、Run `98f27b4d-…`（watch 06:45:59 首次看到） |
| 06:45:55–06:48:31 | worker 调查：8 次模型请求、19 次工具调用（库 `budget_spent=8`、`tool_operations_used=19`，trace 子 run llm 8、tool 19），`report_available`，结论 `partial`（PlaceOrder 错误集中在 payment `Charge`「Invalid token」；flag 值未观测到，根因机制未确立） |
| 06:48:53 | web 循环自动授权：`auto_authorize_observation` / `system`，代际 0 → 0，会话 `b9515b54-…` `authorization_source=automatic`，deadline 07:48:53；事故 `observing_recovery`（watch 06:49:00） |
| 06:49:01 | watch 看到授权后 `fault restore`：ConfigMap 回到 `1ee5c025…`，`verified: true` |
| 06:49:57–06:53:59 | 样本 1–5 `degraded`（error_ratio 1.0 → 0；第 5 个 error_ratio 已为 0，p95 660 ms 超界） |
| 06:52:53 | 告警 resolved → `resolved_attached`（不改生命周期） |
| 06:54:59–07:00:00 | 样本 6–11 `healthy`（p95 约 190 ms） |
| 07:01:01–07:07:03 | 样本 12–18 `degraded`：error_ratio 0、依赖错误率 0，只有 checkout p95 超过 500 ms 上界（700 → 1820 ms） |
| 07:08:03–07:17:06 | 样本 19–28 `healthy`（p95 350–380 ms，后降到约 190 ms） |
| 07:18:06–07:21:07 | 样本 29–32 `degraded`：error_ratio 0，p95 560–660 ms |
| 07:22:08–07:29:09 | 样本 33–40 `healthy`；`healthy_since` 07:22:08，到第 40 个样本时健康窗 421 s < 600 s |
| 07:29:10 | ending `max_samples_exhausted`，transition `observation_ended_unconfirmed`，`observing_recovery → open`（交接） |
| 07:29:12–07:29:13 | 自动复盘第 1 次尝试 `failed`，`INPUT_TOO_LARGE`，0 次模型请求；生成任务封顶（watch 行 `INPUT_TOO_LARGE/f3`） |
| 07:29:51 | 余额读数 `after-observation`（200.03 CNY） |
| 07:30:30 | verify.py S1 判据（摘要 `verdicts.checked_at`） |
| 08:16:35–08:16:39 | worker 停止后以 PR #240 HEAD `95ee1bb` 重启（`run-worker-r12.sh`，同一库与 trace round） |
| 08:16:39–08:18:31 | 复盘第 2 次尝试 `succeeded`：1 次模型请求（prompt 59,414 / completion 24,860 tokens），版本 1，进入 `under_review` |
| 08:23:54 | 工作台批准：版本页 200、POST review 200，版本 1 `under_review → approved`，发布 2 条知识 revision |
| 08:24:03 | 余额读数 `after` |
| 08:24:26 | 冻结摘要 |

## 判据结果（verify.py，S1，7/11）

- 通过：C1 发布前提（当前 Run `completed`、有结论）；C2 唯一授权决定与会话；C3 一次 system 授权、代际不变；C4 无任何人工 control；C5 automatic + `revision=null`；C6 F6 来源 automatic、`permissions` 无 `human_control`、`actions` 无人工动作；C7 离线重放一致（`unknown`，健康窗 421 s）。
- 未通过（均因恢复未确认）：S1-1 会话 `expired`/`max_samples_exhausted`（期望 `completed`/`recovery_confirmed`）；S1-2 事故 `open`（期望 `resolved`，库与 F6 一致）；S1-3 未确认、健康窗 421 s < 600 s；S1-4 事故页没有 "recovered, no remediation recorded"（来源 automatic 正常显示，页面 200，sha256 `498172ac…`）。
- 这是 S1（期望恢复）判据，未用 #184 的 S2（期望到期交接）判据重判。按合同，本次结局是另一条合法路径：40 次采样耗尽、未确认 → 事故保持 `open` 并交接，F6 `human_interaction=handoff`，`handoff_reasons` = `MAX_SAMPLES_EXHAUSTED`、`OBSERVATION_UNCONFIRMED`。
- trace：[根 run](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/b1a27f49-b0eb-4ef6-9d79-363801a4ad1f/r/00000000-0000-0000-3055-6f91339217a7?poll=true)，`read_back = found`，project 内根 run 1 个（调查），子 run llm 8、tool 19，与库一致。复盘生成是否另有 trace 未核查（未确认）。

## 复盘与批准

- 第 1 次尝试（`8ff4a23`，输入策略 v3）：40 样本 × 8 信号的恢复读数使模型输入达 265,032 B，超过 262,144 B 上限，未发请求即失败（字节数取自 PR #240 正文；读数与目录约占 73% 为 lead 测算，未确认）。凡 `max_samples_exhausted` 的会话都会复现。
- 修复：PR #240（合同 r12，输入策略 v4、提示词 v5）只压缩给模型的恢复读数与目录投影，数据行与引用校验不变；按 D52 已封顶的 v3 任务在 v4 下自动重开。worker 重启后无人工操作即出现第 2 次尝试并成功（`worker-r12.log`）。
- 第 2 次尝试的输入 146,729 B、22 条结论引用全部通过、争议 0：为 lead 从库中读到，不在冻结摘要中（未确认）。冻结证据能确认的是：尝试 `succeeded`、版本 1、批准前状态 `under_review`（引用全部通过的版本才会自动进入 `under_review`）。
- 批准（`approve.json`）：审核人 `e2e-lab`（lab Basic Auth），版本页含批准表单、页面 200、POST 200；版本 1 内容 sha256 `d4a1df2d…` 批准前后不变，复盘代际 2 → 3；发布两条知识 revision 1：「Checkout PlaceOrder failure burst localized to the payment Charge call」与「Error-ratio alert firing on a single-digit span count」，`approved_by_kind = basic_auth`。

## 结论：哪些走通了、哪些没有

- 走通：干净库迁移与非 owner 角色启动；真实告警 → 自动建事故 → 真实模型调查并发布结论；结论发布后无人点击的自动授权；Observer 独立采样 40 次并按合同结束、交接；离线重放与库一致；复盘草稿（在 #240 修复后）→ 工作台人工批准 → 知识发布。
- 没走通：恢复未确认。故障在授权后 1 秒恢复，错误率从样本 5 起一直为 0，但 checkout p95 在 12–18、29–32 两段超出 500 ms 上界，健康窗两次被打断，最后一段只累积到 421 s，40 次采样就用完了（40 × 60 s 先于 3600 s 期限耗尽）。
- 原因判断（lead 运行时观察，未入冻结证据，未确认）：同时有其他开发任务在跑，宿主内存吃紧（swap 16.6/17.4 GB，kind VM 常驻内存约 0.85–1.4 GB），p95 抬升是环境干扰，不是故障残留。冻结证据与此相容的部分：两段退化期间 error_ratio 与依赖错误率均为 0，p95 退化后自行回落。按 D1「可解释即算数、不为通过重跑」，不重跑。
- 首次复盘失败暴露的是产品缺陷（输入超限），已由 #240 修复，但 #240 尚未合并；本次复盘成功依赖未合并代码。

## 费用

同一 DeepSeek key 余额三次读数（`balance.jsonl`，与摘要 `cost` 一致）：充值余额 200.39（06:39:10）→ 200.03（07:29:51）→ 200.02（08:24:03），合计约 0.37 CNY；前段 0.36 对应调查（8 次请求，失败的复盘 0 次请求），后段 0.01 对应复盘生成（1 次请求）。08:24 读数出现 4.73 CNY 赠送余额（`granted_balance`，总余额 204.76），来源不明，不计入。同一 key 在窗口内是否有其他会话调用未核查。

## 观察到但不属本次判定

- 宿主内存干扰：见上。若要在同一环境复现「恢复确认」，运行期间不要并行跑其他重负载任务。
- 极低流量下恢复判定对延迟敏感：观察期间 checkout 请求率约 0.01–0.05 req/s（每 5 分钟窗口只有个位数到十几个 PlaceOrder span），p95 由少数几个慢请求决定（推断：单个慢请求就可能让整个 5 分钟窗口越过 500 ms，未逐请求核查）。HealthProfile 阈值的既有行为，本次不调整。
- 第 40 个样本时健康窗已有 421 s，再有 3 个健康样本即可达到 600 s；期限还剩约 19 分钟，但 40 次上限先到。若按 #184 S2 设计「期限先于次数耗尽」，本次相反，是另一种合法结束方式。
- 调查结论为 `partial`：模型定位到 payment `Charge` 的「Invalid token」并怀疑 flag 注入，但调查工具读不到 flagd 配置，未把根因写成已确立；与只读工具面一致。

## 原始文件

工程机 `tmp/m1-exit-e2e/` 下，git 忽略、不入库（sha256 前 12 位）：`timeline.log` `f5f8a701651a`（5,601 B，与摘要 `timeline` 一致）、`s1-verdicts.json` `f82d91210455`、`balance.jsonl` `754f1d899cb7`、`inject.log` `013b19999c37`、`web.log` `7ea2d431dad2`、`worker.log` `23e522614cb5`、`worker-r12.log` `4299fada638b`、`observer.log` `96acd9f6d475`、`run-web.sh` `2d5489b594df`、`run-worker.sh` `5dc5298e49d6`、`run-worker-r12.sh` `3ba5c027bdb7`、`run-observer.sh` `e3dbd3895ac0`、`watch.sh` `802f1126ef01`。凭据文件（工作台账号、web 私有环境、`.env`）不列出、不入库。
