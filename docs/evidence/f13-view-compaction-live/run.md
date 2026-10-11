# #199 有界真实运行：traces 视图压缩后，卡住的复盘在 v3 下生成并审核（补齐 #169 第 4 项）

- 日期：2026-10-11 14:48–14:56Z；分支 `feature/F13-view-compaction`，代码运行的 commit `92f434e`（运行期间工作区干净）；证据提交为 `35800ee`（首版）和本目录的计数校正提交
- 合同：M1-03 r9（D47–D52，见 [M1-03 任务记录](../../tasks/2026-10-05-m1-03-postmortem.md) 与[合同决定表](../../tasks/2026-10-05-m1-03-postmortem-contract.md)）；真实 Run 要求来自 #199 接口 r9 的「验收」一节
- 冻结摘要：[live-runs/cedc47eb-79b4-4748-85cd-9bb4b0ecd9b6/summary.json](live-runs/cedc47eb-79b4-4748-85cd-9bb4b0ecd9b6/summary.json)（以 v3 生成尝试 ID 命名）。原始 ledger（复盘投影全文、结论全文、F13 审计行、尝试行）不入库，sha256 `a7d165fd…dc1ef0757`，169331 字节（ADR-0006）
- 类别：在 #169 真实运行库的副本上做有界真实运行，调用真实模型。审核由运行 Agent 经工作台 HTTP 表单提交（D33 测试驱动模拟审核），不是真人审阅。这不是生产证明
- 执行者：未参与实现的新上下文 Agent
- **结论：第 1–4 项全部通过。** v2 下封顶的生成任务在 v3 下恰好重开一次；49569 字节的 traces 视图压缩成 5406 字节的 `span_groups_only`；真实 `deepseek-flash` 生成的 v1 进入 `under_review`，引用全部有效、争议为 0；工作台批准返回 200。全程 PG 日志中 ERROR、42501 和 `permission denied` 都是 0。#169 第 4 项（worker 角色写草稿、web 角色审核）由此补齐

## 环境

- PostgreSQL 17.9：本 worktree 的 `tmp/pg-live-copy`（#169 数据目录的逐字节副本），以 `pg_ctl … -o "-p 55494 -k <worktree>/tmp"` 启动，库 `m0_budget`。日志设置沿用副本里 #169 的 `postgresql.conf`：`log_connections`、`log_disconnections`、`log_min_error_statement=error`，`log_line_prefix` 含 user、app、SQLSTATE。日志写到新文件 `tmp/pg-live-copy.log`。运行结束后执行 `pg_ctl -D tmp/pg-live-copy stop`
- owner `m0_lab` 只用于以下操作：`python -m opspilot.schema check`（输出 `schema at head 0011_runtime_roles`，本改动没有迁移）；每 10 s 一次的 `pg_stat_activity` 采样（`lab-monitor`）；只读查询与投影（`lab-readonly`，会话设 `default_transaction_read_only=on`）。登录角色沿用 #169 在该集群里建的 `opspilot_web_lab ∈ opspilot_web`、`opspilot_worker_lab ∈ opspilot_worker`，没有新建或修改角色
- 两个进程都用 `env -i` 启动，DSN 带 application_name。驱动脚本由 #169 归档复制到本 worktree 被忽略的 `tmp/f13-live/` 再改，归档本身只读：
  - web：`python -m opspilot.web serve`，用户 `opspilot_web_lab`，`OPSPILOT_BIND=127.0.0.1:8794`，`OPSPILOT_TOOL_PROFILE=otel-demo`，事件 token 只含哈希，Basic Auth 账号沿用 #169 的实验账号 `pgroles-lab`，目标身份文件与 #169 相同
  - worker：`python -m opspilot.worker_main`，用户 `opspilot_worker_lab`，同一 profile，`OPSPILOT_TRACE=lab`。复盘版本为 `f13-postmortem-prompt-v4` / `f13-postmortem-input-v3` / `f13-postmortem-output-v2`，模型 `deepseek-flash`，profile `deepseek-flash/json/thinking-high/no-tools`
  - 没有启动 Observer，没有动 kind 实验环境：副本中该事故的恢复已经确认，调查 Run 已经 `completed`
- trace：round `f13-view-compaction-live-20261011`，project `opspilot-lab-f13-view-compaction-live-20261011`。运行前用 `lab_evidence.prepare_lab_project` 建立 project，回读得到 `trace_tier=longlived`
- 防循环上限：沿用产品默认值，每次复盘生成最多 2 次模型请求（R9），每个水位最多 3 次尝试（R7）

## 起点（运行前只读）

- 事故 `b23a19fd-cf49-5e1b-bd03-a3a89973e3fe`：`lifecycle=resolved`，Run `8e8ad43b-…` 状态为 `completed`，复盘版本 0 个
- 生成任务：`consecutive_failures=3`、`last_error_code=INPUT_TOO_LARGE`、`next_attempt_at` 为空，`failure_watermark=1:1:1:8e8ad43b-…:0#r`
- 唯一一次尝试 `045af765-…`：`failed` / `INPUT_TOO_LARGE`，`input_policy_version=f13-postmortem-input-v2`，`prompt_version=f13-postmortem-prompt-v3`，模型请求 0
- web 进程起来后、worker 启动前，事故页（200）显示 `generation_failed`，以及「Generation stopped at this watermark after 3 failed attempts」

## 时间线（UTC）

| 时间 | 事件 |
|---|---|
| 14:48:00 | PG 副本在 55494 启动；14:48:07 schema check 显示 head 为 0011 |
| 14:48:41 | 记录余额 3.44 CNY；trace project 建立，`longlived` |
| 14:49:12 | 采样器、web（PID 52909）启动；14:49:14 web 以 `opspilot_web_lab` 建立连接 |
| 14:49:33 | worker（PID 54397）启动，`trace export enabled mode=lab`；14:49:33.99 领取生成任务，开始尝试 `cedc47eb-79b4-4748-85cd-9bb4b0ecd9b6` |
| 14:49:34–14:50:50 | 模型请求 1：53313 输入 / 16384 输出 token |
| 14:50:50–14:51:08 | 模型请求 2：57700 输入 / 5121 输出 token |
| 14:51:08.78 | 版本 1 写入，审计 `generate`（generation 1）、`submit`（generation 2），状态 `under_review` |
| 14:52:32 | 工作台批准，返回 200，`approved`，generation 3，发布知识条目 1 条 |
| 14:53:55 | 复查：尝试共 2 条，任务 `consecutive_failures=0`、没有错误码，worker 没有再次领取 |
| 14:54:01 | 用 `lsof` 把 client_port 与 PID 对照 |
| 14:54:06 | 按 PID 用 SIGTERM 停 worker 和 web，两者都正常退出；随后停采样器 |
| ~14:55 | 只读投影、重建输入、trace 回读、余额、冻结摘要，之后执行 `pg_ctl stop` |

## 结果

| 项 | 预期 | 实际 | 判定 |
|---|---|---|---|
| 1 D52 恰好重开一次 | 新 worker 在 v3 下领取 v2 时封顶的任务，只领一次 | 尝试行共两条：`045af765-…` 为 v2 `failed`/`INPUT_TOO_LARGE`，模型请求 0；`cedc47eb-…` 为 v3 `succeeded`，version 1，模型请求 2/2。任务行变为 `consecutive_failures=0`，`last_error_code` 和 `failure_watermark` 为空，`lease_epoch` 从 1 变为 2。成功后到停进程前（14:51:08–14:54:06，经过多个 30 s 扫描周期）没有第 3 条尝试。worker 日志只有一行 `status=succeeded` | 通过 |
| 2 D47 压缩 | 该视图投影为 `span_groups_only`，没有 INPUT_TOO_LARGE | 视图 `54dedff4-…-t2:1933e66f-…` 按旧规则投影为 **49569 字节**，压缩后为 **5406 字节**（≤ 32768），`projection=span_groups_only`，`omitted_content_items=66`，保留 25 个 span group，`citable_as_fact=true`。证据目录条目带同一 `projection`，`citable_as_fact=true`。其余 12 个 `metrics_range_query` 视图（1331–6231 字节）没有 `projection` 字段，原样投影。总输入 137706 字节（≤ 256 KiB）。核对方法：运行后用 owner 只读会话调用 `GenerationStore.read_input` 与 `build_input` 重建输入，sha256 `5da21402…3536b103b` 和字节数与尝试行记录的 `input_sha256`/`input_bytes` 完全一致 | 通过 |
| 3 真实草稿进入审核 | 真实模型生成草稿，进入 `under_review` | 版本 1 `under_review`，共 18 条结论：5 条代码段，13 条模型结论（9 条 supported、4 条 uncertain）。`citations_valid` 全部为 true，引用失败 0，争议 0，知识提议 1 条（`ratio_alert_small_denominator_check`）。13 条模型结论中有 3 条引用压缩视图，见下节 | 通过 |
| 4 web 审核动作 | 用 Basic Auth 经工作台执行一次审核 | **批准**。以 `pgroles-lab` GET `/postmortems/ded45924-…/versions/1`（200），再提交页面渲染的 `form-approve`（隐藏字段 action、expected_generation=2、页面给出的幂等标签）到 `/postmortems/ded45924-…/versions/1/review`，返回 200：`state=approved`，generation 3，`replayed=false`。发布知识条目 `3e5718ee-…` revision 1，状态 `active`、`retrievable=true`。审计依次为 `generate`、`submit`（`principal_kind=worker`）和 `approve`（`pgroles-lab`，`basic_auth`）。选择批准的理由：版本处于 `under_review`，争议为 0，引用全部有效（D16 允许批准）；引用压缩视图的事实已与库中存储的 span_groups 逐项核对一致（见下节） | 通过 |
| PG 日志 | web、worker 进程的 ERROR / 42501 / `permission denied` 为 0 | 计数截止到第 327 行，即 14:54:06.879Z web 最后一个会话断开（worker 和 web 都已停止），与 summary.json `pg_log` 相同：327 行，ERROR 0、FATAL 0、`sqlstate=42501` 0、`permission denied` 0，SQLSTATE 非 `00000` 的行 0。截止之后到 `pg_ctl stop` 的第 328–358 行（14:55:22Z 起）只有运行后 owner 只读检查（`lab-readonly` 连接 6 个）和关库记录，这一段同样是 ERROR、FATAL、42501、`permission denied` 均为 0，没有计入冻结的数字 | 通过 |
| 连接身份 | web、worker 以各自的非 owner 登录角色连接，从不以 owner 连接 | 连接日志：`opspilot_web_lab/opspilot-web` 3 个，`opspilot_worker_lab/opspilot-worker` 3 个；owner 只有 `lab-monitor` 30、`lab-readonly` 43（截止同上；截止后另有 6 个运行后只读检查）、`lab-schema-check` 1。`pg_stat_activity` 共 30 次快照（14:49:14–14:54:04），出现的非 owner 用户只有这两个，带 `opspilot-*` 应用名的 owner 会话为 0。14:54:01 用 `lsof` 对照 client_port，不依赖进程自报的 application_name：web PID 52909 → 60874、61838（`opspilot_web_lab`），worker PID 54397 → 60970、60972（`opspilot_worker_lab`） | 通过 |
| trace | LangSmith 可见 | 复盘生成[根 run](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/226ac9e5-5965-422f-97c0-84d8463a20bd/r/00000000-0000-0000-0b00-481198872b09?poll=true)：`opspilot.postmortem_generation`，OTel `0889d94bf8c8b2fe4339a0860dd392c5`，`success`，2 个 llm 子 run，111013 输入 / 21505 输出 token，与尝试行的 `model_requests=2`、`prompt_tokens`、`completion_tokens` 一致 | 通过 |

### #169 第 4 项补齐

#169 第 4 项列为「未执行」的部分，这次在非 owner 角色下都实际执行了，PG 日志中 42501 为 0：

- worker 角色（`opspilot_worker_lab`）：D52 重开时对生成任务执行 `UPDATE`（`consecutive_failures`、`failure_watermark`）；领取租约；写尝试行；调用模型；写版本、18 条结论和 1 条提议；写 `generate`/`submit` 审计；把版本改为 `under_review`
- web 角色（`opspilot_web_lab`）：渲染事故页和版本页；执行批准，写 `approve` 审计、版本状态和知识条目 revision 1

拒绝、退回、替换、撤销这次没有执行，它们的授权仍然只有 PG 集成测试覆盖。

## D49：引用压缩视图的结论

13 条模型结论里有 3 条引用了 `span_groups_only` 视图，引用项的 `citable_as_fact=true`，`citations_valid=true`：

- 第 10 条，findings，`supported`，共 2 个引用（另一个是 `metrics_range_query` 视图 `54dedff4-…-t0`），内容全部来自 span_groups：
  > In the sampled span groups the only checkout operations with error rows are oteldemo.CheckoutService/PlaceOrder (2 rows, rpc.grpc.status_code=13) and its client call oteldemo.PaymentService/Charge (2 rows, rpc.grpc.status_code=2), matching service=payment operation grpc.oteldemo.PaymentService/Charge (2 error rows); checkout's other dependency operations in the same sample - oteldemo.CartService/GetCart, oteldemo.CurrencyService/Convert, oteldemo.ProductCatalogService/GetProduct and oteldemo.ShippingService/GetQuote - have no error rows and rpc.grpc.status_code=0 where a status is recorded.
- 第 5 条，impact_summary，`supported`，共 12 个引用。其中来自 traces 的部分是：
  > in the sampled span groups the only checkout operations with error rows are oteldemo.CheckoutService/PlaceOrder (rpc.grpc.status_code=13) and its client call oteldemo.PaymentService/Charge (rpc.grpc.status_code=2), matching payment's grpc.oteldemo.PaymentService/Charge (2 error rows)

  同一条结论还写明了投影的限制：
  > the trace evidence is a span_groups_only projection of a 66-span sample with individual span content omitted, so it supports statements per service and operation only and no success/failure proportion
- 第 15 条，hypotheses，`uncertain`，共 5 个引用：
  > … no other checkout dependency operation carries errors in the sampled span groups …

核对：库中该视图 PlaceOrder 与 Charge 相关的 span_groups 行是 `checkout / oteldemo.CheckoutService/PlaceOrder`（rows 2，error_rows 2，status `…rpc.grpc.status_code=13`）、`checkout / oteldemo.PaymentService/Charge`（rows 2，error_rows 2，`…=2`）、`payment / grpc.oteldemo.PaymentService/Charge`（rows 2，error_rows 2）。上面三条引用都停在服务/操作的聚合层，与 D51 提示词规则一致，没有引用单个 span 的细节（span ID、单条耗时等）。这是对一次输出的人工核对，不能说明模型在其他输入上同样守规则。

## 偏差

- web 端口用 8794，而非 #169 的 8080，避免与同机其他运行冲突；allowed origin 随之为 `http://127.0.0.1:8794`
- 采样间隔从 20 s 改为 10 s，并记录 `client_port`
- 本次测得的旧规则投影是 49569 字节，任务说明（来自 #169）写的是 49521 字节，两者都超过 32768，差 48 字节。48 正好等于 `,"run_id":"<36 位 UUID>"` 的长度，`build_input` 的投影带 `run_id`，所以推测 #169 测量时没有算 `run_id`。这一点未核实 #169 的测量脚本
- 第一次冻结摘要时，`review_actions` 和 `knowledge` 被序列化成 dataclass 的 repr 字符串。删除这份本次新写的 summary.json 后，修好序列化重新冻结；库没有变化，ledger 用重新生成的这份
- 没有做负向启动检查（#169 已做，本次不要求）

## 失败与限制

- 没有失败项，没有重试，没有改代码
- 模型请求 1 的输出 token 是 16384，正好等于 `MAX_OUTPUT_TOKENS`，worker 随即发出第 2 次请求（修正轮，在 R9 的 2 次上限内）并得到被采纳的输出。据此推断第 1 次输出被长度截断；trace 回读里没有找到 finish_reason，这一点未直接确认。离上限只剩 0 次余量：同样输入如果第 2 次也被截断，这次尝试会失败
- D52 的另一半（v3 下再次 `INPUT_TOO_LARGE` 时照旧封顶、不再重开）本次没有触发，只由合同测试覆盖
- 只有一个事故、一次生成。审核是测试驱动的模拟审核，不是真人判断
- 数据是 #169 运行库的副本，不是生产库

## 费用

DeepSeek 余额从 3.44 降到 3.29 CNY，差 0.15 CNY，分别取于 14:48:41Z 和约 14:55Z。同一账户在此期间有其他会话的运行，所以这只是本次费用的上界。token 为 111013 输入 / 21505 输出（尝试行与 trace 一致）。

## 凭据

证据中没有 token、口令、API key，也没有幂等键原值。gitleaks 8.30.1 对本目录做 `dir` 扫描，结果是 `no leaks found`。另把私有 env 文件、Prometheus 账号文件、webhook token、实验 UI 口令与账号文件中的 36 个值，逐一在本目录做字面匹配（不打印原值）。命中两类非机密值：LangSmith 组织 ID（trace 链接里的 `/o/…`，与已入库的 #169 证据相同），以及 `.env` 中 `LANGSMITH_PROJECT` 的值 `default`（命中的是 `default_transaction_read_only` 里的普通单词）。
