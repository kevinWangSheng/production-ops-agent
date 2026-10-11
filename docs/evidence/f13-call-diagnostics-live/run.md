# #202 有界真实运行：复盘生成逐请求记录 finish_reason 与 token 拆分

- 日期：2026-10-11 19:43–19:50Z；分支 `feature/F13-output-budget`，代码运行的 commit `cc9966b`（运行期间工作区干净，只新增被忽略的 `tmp/` 内容）
- 合同：M1-03 r10（D53–D56，见 [M1-03 任务记录](../../tasks/2026-10-05-m1-03-postmortem.md) 与[合同决定表](../../tasks/2026-10-05-m1-03-postmortem-contract.md)）；真实 Run 要求来自 #202 接口 r10 的「验收」一节
- 冻结摘要：[live-runs/7ae6b3b0-aba1-4782-9623-1cb1f00e580b/summary.json](live-runs/7ae6b3b0-aba1-4782-9623-1cb1f00e580b/summary.json)（以 v3 生成尝试 ID 命名）。原始 ledger（复盘结果投影全文、尝试行）不入库，sha256 `3d37d0a4…2e73d1d89`，126221 字节（ADR-0006）
- 类别：在 #169 真实运行库的新副本上做有界真实运行，调用真实模型。没有执行审核动作。这不是生产证明
- 执行者：未参与实现的新上下文 Agent
- **结论：第 1–4 项全部通过。** v2 下封顶的生成任务在 v3 下重开，一次真实生成尝试发出 2 次请求，草稿进入 `under_review`。尝试行 `model_calls` 有 2 项，逐项 token 之和等于尝试行合计；PG、LangSmith trace、工作台页面三处的 finish_reason 与 token 数值逐项相同。第 1 次请求 `finish_reason=length`，输出 16384 token 中 reasoning 占 71.5%。全程 PG 日志中 ERROR、42501 和 `permission denied` 都是 0

## #202 的回答（观测值）

| 请求 | 用途 | finish_reason | 输入 token | 输出 token | 其中 reasoning | 可见输出 | reasoning 占输出 | 结局 | problems |
|---|---|---|---|---|---|---|---|---|---|
| 1 | generate | **length** | 53313 | 16384 | 11710 | 4674 | 71.47% | repair_requested | `NOT_JSON` |
| 2 | repair | stop | 58010 | 6467 | 1065 | 5402 | 16.47% | accepted | 无 |

- 有一次请求以 `length` 结束：第 1 次（generate）。它的输出 token 正好是 `MAX_OUTPUT_TOKENS`=16384，其中 11710 是 reasoning、4674 是可见输出，可见部分不是可解析的 JSON（`NOT_JSON`），于是触发修正请求
- 第 2 次（repair）以 `stop` 结束，6467 输出 token 中 reasoning 1065，输出被采纳
- 这是一次生成、一个输入上的观测，不说明其他输入或其他时刻的分布

## 环境

- PostgreSQL 17：本 worktree 的 `tmp/pg-live-copy`（#169 原始真实运行数据目录的新逐字节副本），以 `pg_ctl … -o "-p 55497 -k <worktree>/tmp"` 启动，库 `m0_budget`，日志写到新文件 `tmp/pg-live-copy.log`。日志设置沿用副本里 #169 的 `postgresql.conf`：`log_connections`、`log_disconnections`、`log_min_error_statement=error`，`log_line_prefix` 含 user、app、SQLSTATE。运行结束后执行 `pg_ctl -D tmp/pg-live-copy stop`
- 迁移：运行前 `python -m opspilot.schema check` 输出 `schema is at 0011_runtime_roles, head is 0012_generation_call_diagnostics`（退出 1）；owner `m0_lab` 执行 `make migrate` 输出 `schema upgraded: 0012_generation_call_diagnostics`；之后 check 输出 `schema at head 0012_generation_call_diagnostics`（退出 0）。迁移后旧尝试行 `model_calls=[]`；`model_calls` 列权限为 `opspilot_web` SELECT、`opspilot_worker` SELECT+UPDATE
- owner 只用于：迁移、schema check、每 10 s 一次的 `pg_stat_activity` 采样（`lab-monitor`）、只读查询与投影（`lab-readonly`，会话设 `default_transaction_read_only=on`）。登录角色沿用 #169 在该集群里建的 `opspilot_web_lab ∈ opspilot_web`、`opspilot_worker_lab ∈ opspilot_worker`，没有新建或修改角色
- 两个进程都用 `env -i` 启动，DSN 带 application_name。驱动脚本从 #199 归档复制到本 worktree 被忽略的 `tmp/f13-diag-live/` 再改，归档本身只读：
  - web：`python -m opspilot.web serve`，用户 `opspilot_web_lab`，`OPSPILOT_BIND=127.0.0.1:8797`，`OPSPILOT_TOOL_PROFILE=otel-demo`，Basic Auth 实验账号 `pgroles-lab`，目标身份文件与 #169 相同
  - worker：`python -m opspilot.worker_main`，用户 `opspilot_worker_lab`，同一 profile，`OPSPILOT_TRACE=lab`。复盘版本 `f13-postmortem-prompt-v4` / `f13-postmortem-input-v3` / `f13-postmortem-output-v2`，模型 `deepseek-flash`，profile `deepseek-flash/json/thinking-high/no-tools`
  - 没有启动 Observer，没有动 kind 实验环境
- trace：round `f13-call-diagnostics-live-20261011`，project `opspilot-lab-f13-call-diagnostics-live-20261011`。运行前用 `lab_evidence.prepare_lab_project` 建立，回读 `trace_tier=longlived`
- 防循环上限：产品默认值，每次生成最多 2 次模型请求，每个水位最多 3 次尝试（R7）

## 起点（运行前只读）

- 事故 `b23a19fd-cf49-5e1b-bd03-a3a89973e3fe`：`lifecycle=resolved`，复盘版本 0 个
- 生成任务：`lease_epoch=1`、`consecutive_failures=3`、`last_error_code=INPUT_TOO_LARGE`、`next_attempt_at` 为空
- 唯一一次尝试 `045af765-…`：`failed` / `INPUT_TOO_LARGE`，`f13-postmortem-input-v2`，模型请求 0
- web 起来后、worker 启动前，事故页（200）的尝试表中该尝试的逐请求列显示 `unknown`

## 时间线（UTC）

| 时间 | 事件 |
|---|---|
| 19:43:48 | PG 副本在 55497 启动，schema check 显示 0011 |
| 19:44:02 | `make migrate` 到 0012，复查 check 为 head |
| 19:44:15 | 记录余额 3.27 CNY；trace project 建立，`longlived` |
| 19:44:22 | 采样器、web（PID 21361）启动，web 以 `opspilot_web_lab` 连接；取 worker 前页面 |
| 19:44:30 | worker（PID 22112）启动，`trace export enabled mode=lab`；19:44:31.18 开始尝试 `7ae6b3b0-aba1-4782-9623-1cb1f00e580b` |
| 19:44:31.20–19:45:50.49 | 模型请求 1：`length`，53313 输入 / 16384 输出 |
| 19:45:50.50–19:46:13.15 | 模型请求 2：`stop`，58010 输入 / 6467 输出 |
| 19:46:13.19 | 尝试 `succeeded`，版本 1 `under_review` |
| ~19:46:25 | 以 Basic Auth 取事故页（200）；trace 回读 |
| 19:46:48 | `lsof` 对照 client_port 与 PID |
| 19:48:13 | 复查：尝试仍为 2 条，任务 `consecutive_failures=0`、无错误码 |
| 19:48:18 | 按 PID 用 SIGTERM 停 worker 和 web，两者正常退出；随后停采样器 |
| 19:49:26 | 只读投影、余额、冻结摘要；19:49:41 `pg_ctl stop` |

## 结果

| 项 | 预期 | 实际 | 判定 |
|---|---|---|---|
| 1 v3 重开并跑一次真实生成 | 新 worker 在 v3 下重开 v2 封顶的任务，一次真实生成尝试 | 尝试行共 2 条：`045af765-…`（v2，`failed`/`INPUT_TOO_LARGE`，请求 0）与 `7ae6b3b0-…`（v3，prompt v4，`succeeded`，version 1，请求 2/2）。任务 `lease_epoch` 1→2，`consecutive_failures=0`，`last_error_code` 与 `failure_watermark` 为空。成功后到停进程（19:46:13–19:48:18，多个 30 s 扫描周期）没有第 3 条尝试 | 通过 |
| 2 尝试行 `model_calls` | 每个真实请求一项，含 seq、purpose、finish_reason、四类 token、outcome、problems；逐项之和等于尝试合计 | 2 项，等于 `model_requests=2`；每项的键恰好是合同的 9 个字段，没有其他键。prompt 之和 53313+58010=111323 = 尝试行 `prompt_tokens`；completion 之和 16384+6467=22851 = `completion_tokens`。visible = completion − reasoning 两项都成立。逐项值见上方 #202 表 | 通过 |
| 3 trace 可见 | LLM run 的 metadata 有同样的 finish_reason 与 reasoning_tokens | 复盘生成[根 run](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/192bd20a-b8c0-4bc0-be95-84d7c215a0a1/r/00000000-0000-0000-f2f1-73a0110410cc?poll=true)：`opspilot.postmortem_generation`，OTel `e2cd24b148669b7a92ff81693eddd7b8`，`success`，111323 输入 / 22851 输出。经 LangSmith API 回读 2 个 `deepseek.chat.completions` llm 子 run：`otel.span.opspilot.model.finish_reason` 依次为 `length`、`stop`，`otel.span.opspilot.model.reasoning_tokens` 依次为 11710、1065，run 的 prompt/completion token 依次为 53313/16384、58010/6467，与 PG 逐项相同 | 通过 |
| 4 工作台页面 | Basic Auth 取事故页，尝试表显示同样数值 | 以 `pgroles-lab` GET `/incidents/b23a19fd-…`（200）。`postmortem-attempts` 表中新尝试的逐请求列：`generate · finish length · prompt 53313 · completion 16384 (reasoning 11710, visible 4674) · repair_requested · NOT_JSON`；`repair · finish stop · prompt 58010 · completion 6467 (reasoning 1065, visible 5402) · accepted`。旧尝试显示 `unknown`。summary.json `three_way_agreement` 对两项都是 `all_equal=true`（PG、trace、页面）。F13 验收投影 `recent_attempts` 带同样的 `model_calls`，旧尝试为 `[]` | 通过 |
| PG 日志 | web、worker 的 ERROR / 42501 / `permission denied` 为 0 | 计数截止到第 241 行，即 19:48:18.778Z web 最后一个会话断开（worker 和 web 都已停止），与 summary.json `pg_log` 相同：241 行，ERROR 0、FATAL 0、`sqlstate=42501` 0、`permission denied` 0，SQLSTATE 非 `00000` 的行 0。截止后到 `pg_ctl stop` 的第 242–262 行只有冻结摘要时的 3 个 `lab-readonly` 连接和关库记录，同样 ERROR、FATAL、42501、`permission denied` 均为 0，没有计入冻结数字 | 通过 |
| 连接身份 | web、worker 从不以 owner 连接 | 截止前连接日志：`opspilot_web_lab/opspilot-web` 3 个，`opspilot_worker_lab/opspilot-worker` 3 个；owner 只有 `lab-migrate` 2、`lab-schema-check` 2、`lab-monitor` 24、`lab-readonly` 25。`pg_stat_activity` 24 次快照（19:44:22–19:48:13），非 owner 用户只有这两个，带 `opspilot-*` 应用名的 owner 会话为 0。19:46:48 用 `lsof` 对照：web PID 21361 → 59053、59689（`opspilot_web_lab`），worker PID 22112 → 59094、59097（`opspilot_worker_lab`） | 通过 |
| 不含内容 | 诊断与证据中没有 reasoning_content、正文、提示词 | `model_calls` 每项只有合同 9 个键（`keys_exactly_contract=true`）。trace 回读脚本只取 `otel.span.opspilot.model.*` 元数据和 token 数，不读 inputs/outputs。summary.json 不含结论正文；检索 `reasoning_content`、`"content"`、`messages"`、`body"` 均 0 处 | 通过 |

草稿：版本 1 `under_review`，20 条结论（15 条模型结论），引用失败 0，争议 0。审核为可选项，本次未执行，版本留在 `under_review`。

## 偏差

- web 端口用 8797、PG 端口 55497，避免与同机其他运行冲突
- 本机找不到 gitleaks，没有做 gitleaks 扫描，改为字面匹配（见「凭据」）
- trace 回读脚本按本次需要重写：只输出 `otel.span.opspilot.model.*` 元数据和 token 数，不再像 #199 那样在输出里搜关键词
- summary.json 不再放引用压缩视图的结论正文（#199 放了），以满足「证据中不出现回复正文」

## 失败与限制

- 没有失败项，没有重试，没有改代码
- 第 1 次请求被 `length` 截断后，修正请求成功；离上限只剩 0 次余量，同样输入如果第 2 次也被截断，这次尝试会失败（`OUTPUT_INVALID`）。这类情形本次没有出现，只由合同测试覆盖
- 供应商失败、修正后仍不合格、引用失败草稿这几种结局下的 `model_calls` 本次没有触发，只由合同测试覆盖
- 调查侧 span 也会带 D55 的两个属性（共用函数），本次没有跑调查，未在真实 trace 中核对
- 只有一个事故、一次生成；数据是 #169 运行库的副本，不是生产库

## 费用

DeepSeek 余额从 3.27 降到 3.17 CNY，差 0.10 CNY，分别取于 19:44:15Z 和约 19:49:26Z。同一账户在此期间可能有其他会话的运行，所以这只是本次费用的上界。token 为 111323 输入 / 22851 输出（其中 reasoning 12775），尝试行与 trace 一致。

## 凭据

证据中没有 token、口令、API key，也没有幂等键原值。本机没有 gitleaks。把私有 env 文件、Prometheus 账号文件、webhook token 文件、实验 UI 账号与口令文件、目标身份文件中长度 ≥ 6 的 25 个值，逐一在本目录做字面匹配（不打印原值）。命中两类非机密值：`LANGSMITH_WORKSPACE_ID`，它等于 trace 链接里的组织 ID `/o/c7727675-…`（已在 #169、#199 证据中入库）；目标身份文件里的两行 JSON 结构片段（`"version": 1,` 与 `}`），不是凭据。
