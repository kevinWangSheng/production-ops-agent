# #202 第二步有界真实运行：复盘输出上限 65,536

**用户判据已达成。** 在 #169 原始真实运行库的新副本上（同一个在 v2 下 `INPUT_TOO_LARGE` 封顶的输入），新 worker 的首次请求以 `finish_reason=stop` 结束，没有修正请求（`model_requests=1`，`repaired=false`），草稿版本 1 进入 `under_review`。只做了一次生成尝试，没有重试。

**限制：这次的首次请求只用了 11703 个输出 token，低于旧上限 16384。** 本次单个样本没有说明「旧上限 16384 下这次一定会截断」。上一轮（[f13-call-diagnostics-live](../f13-call-diagnostics-live/run.md)）同一输入的首次请求用满 16384 后截断。两次的差别来自模型输出的随机性。本次能证明的是：请求实际发出的 `max_tokens` 为 65536，在这个上限下首次请求正常结束，用时 55.5 s，远低于 360 s 超时。

- 日期：2026-10-12 03:20:45–03:25:06Z（本地 2026-10-11 PDT）；分支 `feature/F13-output-65k`，被测代码 commit `341572b76aa33950d9842b1dc346cd5b6296a6db`（运行期间工作区干净，只新增被忽略的 `tmp/` 内容）
- 合同：M1-03 r11（D57–D60，用户裁决 U1=B，见 [M1-03 合同决定表](../../tasks/2026-10-05-m1-03-postmortem-contract.md)）
- 冻结摘要：[live-runs/52a4e52f-1c48-4718-a11f-16dc41be1bfb/summary.json](live-runs/52a4e52f-1c48-4718-a11f-16dc41be1bfb/summary.json)，以生成尝试 ID 命名。原始 ledger 不入库，sha256 `8a07a632…8e37c3c0`，122557 字节（ADR-0006）
- 类别：在 #169 真实运行库副本上的有界真实运行，调用真实模型。没有执行审核动作。这不是生产证明
- 执行者：未参与实现的新上下文 Agent

## 逐请求（来自尝试行 `model_calls`）

| 请求 | 用途 | 实发 max_tokens | finish_reason | 输入 token | 输出 token | 其中 reasoning | 可见输出 | reasoning 占比 | 结局 | problems | 用时（trace） |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | generate | **65536** | **stop** | 53313 | 11703 | 6437 | 5266 | 55.0% | accepted | 无 | 55.51 s（03:21:37.166–03:22:32.679） |

- 实发 `max_tokens`：LangSmith llm 子 run 的 `invocation_params.max_tokens = 65536`。这个值来自 span 属性 `gen_ai.request.max_tokens`，取自 `ModelCall.max_tokens`（`opspilot/tracing.py:400`）。DeepSeek 客户端用 `serialized_request(call)` 生成请求体，其中 `"max_tokens": call.max_tokens`（`opspilot/investigation/loop.py:193`、`client.py:167`），所以 trace 记录的值就是请求体里的值。代码常量 `MAX_OUTPUT_TOKENS = 65_536` 与此相同
- 用时与超时：单次请求 55.51 s，超时为 360 s（span 属性 `opspilot.model.timeout_seconds=360`），只用了超时的 15.4%。整个尝试从 03:21:37.144 到 03:22:32.706，共 55.6 s，生成租约为 900 s。本次输出没有接近 65536，所以 D58 推断的「满额约 304–317 s」仍未实测
- 尝试：`52a4e52f-1c48-4718-a11f-16dc41be1bfb`，`succeeded`，error_code 为空，`model_requests=1/2`，`repaired=false`（来自版本 1 的 generation record），version 1
- 草稿：版本 1 `under_review`，20 条结论（其中 15 条为模型结论），引用失败 0，争议 0。审核为可选项，本次没有执行

## 环境

- PostgreSQL 17：本 worktree 的 `tmp/pg-live-copy`（#169 原始真实运行数据目录的新逐字节副本），启动命令 `pg_ctl -D tmp/pg-live-copy -l tmp/pg-live-copy.log -o "-p 55503 -k <worktree>/tmp" -w start`，库 `m0_budget`，日志写到新文件。日志设置沿用副本里的 `postgresql.conf`：`log_connections`、`log_disconnections`、`log_min_error_statement=error`，`log_line_prefix` 含 user、app、SQLSTATE。结束时执行 `pg_ctl -D tmp/pg-live-copy stop`
- 迁移：迁移前 `python -m opspilot.schema check` 输出 `schema is at 0011_runtime_roles, head is 0012_generation_call_diagnostics`（退出码 1）；owner `m0_lab` 执行 `make migrate`，输出 `schema upgraded: 0012_generation_call_diagnostics`；迁移后 check 输出 `schema at head 0012_generation_call_diagnostics`（退出码 0）
- owner 只用于：迁移、schema check、每 10 s 一次的 `pg_stat_activity` 采样（`lab-monitor`），以及只读查询与投影（`lab-readonly`，会话设 `default_transaction_read_only=on`）。登录角色沿用 #169 在该集群里建的 `opspilot_web_lab ∈ opspilot_web`、`opspilot_worker_lab ∈ opspilot_worker`，没有新建或修改角色
- 进程都用 `env -i` 启动，DSN 带 application_name。驱动脚本从 `tmp/f13-output-archive/impl/f13-diag-live/` 复制到本 worktree 被忽略的 `tmp/f13-65k-live/` 后再修改，归档本身没有改动：
  - web：`python -m opspilot.web serve`，用户 `opspilot_web_lab`，`OPSPILOT_BIND=127.0.0.1:8798`，`OPSPILOT_TOOL_PROFILE=otel-demo`，Basic Auth 实验账号 `pgroles-lab`
  - worker：`python -m opspilot.worker_main`，用户 `opspilot_worker_lab`，`OPSPILOT_TRACE=lab`。复盘版本为 `f13-postmortem-prompt-v4`、`f13-postmortem-input-v3`、`f13-postmortem-output-v2`，模型 `deepseek-flash`，profile `deepseek-flash/json/thinking-high/no-tools`
  - 没有启动 Observer，没有动 kind 实验环境
- trace：round `f13-output-65k-live-20261011`，project `opspilot-lab-f13-output-65k-live-20261011`，运行前用 `lab_evidence.prepare_lab_project` 建立，回读 `trace_tier=longlived`
- 防循环上限：产品默认值，每次生成最多 2 次模型请求（R7 不变）

## 起点（运行前只读）

- 事故 `b23a19fd-cf49-5e1b-bd03-a3a89973e3fe`：`lifecycle=resolved`，复盘版本 0 个
- 生成任务：`lease_epoch=1`，`consecutive_failures=3`，`last_error_code=INPUT_TOO_LARGE`，`next_attempt_at` 为空，`failure_watermark` 非空
- 唯一一次尝试 `045af765-…`：`failed` / `INPUT_TOO_LARGE`，`f13-postmortem-input-v2`，模型请求 0，`model_calls=[]`
- web 启动后、worker 启动前，事故页返回 200，尝试表中该尝试的逐请求列显示 `unknown`

## 时间线（UTC）

| 时间 | 事件 |
|---|---|
| 03:20:45 | PG 副本在 55503 启动 |
| 03:20:46–52 | schema check（0011）→ `make migrate` → check（head 0012） |
| 03:21:19 | 记录余额 3.12 CNY；trace project 建立，`longlived` |
| 03:21:26 | 启动采样器（PID 99833）和 web（PID 99835），web 以 `opspilot_web_lab` 连接；获取 worker 启动前的页面 |
| 03:21:36 | worker（PID 913）启动，日志显示 `trace export enabled mode=lab`；03:21:37.144 开始尝试 `52a4e52f-…` |
| 03:21:37.166–03:22:32.679 | 模型请求 1：`stop`，53313 输入 / 11703 输出（03:21:58 请求进行中时用 `lsof` 对照 client_port 与 PID） |
| 03:22:32.706 | 尝试 `succeeded`，版本 1 进入 `under_review` |
| 03:22:52 | 以 Basic Auth 获取事故页（200）；回读 trace |
| 03:24:23 | 复查：尝试仍为 2 条，任务 `lease_epoch=2`、`consecutive_failures=0`，没有错误码和水位；随后按 PID 向 worker 和 web 发 SIGTERM，两者都正常退出，接着停止采样器 |
| 03:24:51 | 只读投影，读取余额，生成冻结摘要 |
| 03:25:06 | `pg_ctl stop`；03:25:07 再次读取余额 |

## 结果

| 项 | 预期 | 实际 | 判定 |
|---|---|---|---|
| 用户判据 | 首次请求 `stop`、`model_requests=1`、草稿 `under_review` | `stop` / 1 / `under_review`，`repaired=false` | 达成 |
| 实发上限 | 65536 | trace `invocation_params.max_tokens=65536`，与代码常量相同 | 通过 |
| 一次有界尝试 | 只有 1 条新尝试 | 共 2 条尝试：v2 的旧尝试和本次 v3 尝试。从成功到停止进程（03:22:32–03:24:23，多个 30 s 扫描周期）没有出现第 3 条 | 通过 |
| `model_calls` | 每个请求一项，只含合同字段，逐项之和等于尝试合计 | 1 项，等于 `model_requests=1`；键恰好是合同的 9 个；prompt 53313 和 completion 11703 都等于尝试行合计；visible = completion − reasoning（11703 − 6437 = 5266） | 通过 |
| 三处一致 | PG、trace、页面逐项相同 | 复盘生成[根 run](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/d7597bbd-266e-40dc-b757-accb911e7ec6/r/00000000-0000-0000-893a-1ad3b75eca03?poll=true)：`opspilot.postmortem_generation`，OTel trace `26384c32eeabb296f6aee465d6f933d0`，状态 `success`，53313 / 11703。1 个 `deepseek.chat.completions` llm 子 run：`finish_reason=stop`，`reasoning_tokens=6437`。页面（`pgroles-lab`，GET `/incidents/b23a19fd-…`，200）的逐请求列为 `generate · finish stop · prompt 53313 · completion 11703 (reasoning 6437, visible 5266) · accepted`，旧尝试显示 `unknown`。summary.json 的 `three_way_agreement` 中 `all_equal=true`，`field_checks` 全部为 true：finish_reason、prompt、completion、reasoning 三处直接比对；visible 比对 PG 与页面的直接值和 trace 推算值（trace 不导出 visible，D55）；outcome 与 problems 不在 trace 中，只比对 PG 与页面 | 通过 |
| PG 日志 | web、worker 的 ERROR / 42501 / `permission denied` 为 0 | 计数截止到第 183 行，即 03:24:23.889Z web 最后一个会话断开时（此时 worker 和 web 都已停止）。截止前共 183 行：web/worker 的行中 ERROR、FATAL、42501、`permission denied` 和非 `00000` 的 SQLSTATE 都是 0。全日志有 1 条 ERROR（第 29 行，03:21:00Z，SQLSTATE 42703），来自执行者自己的 owner 只读查询（`lab-readonly`）写错了列名，发生在 web/worker 启动之前，不属于 web/worker，见「偏差」。截止后的第 184–206 行只有冻结摘要时的 owner 只读连接、最后一次采样和关库记录，ERROR、FATAL、42501、`permission denied` 都是 0，没有计入冻结数字 | 通过 |
| 连接身份 | web、worker 从不以 owner 身份连接 | 截止前的连接日志：`opspilot_web_lab/opspilot-web` 2 个，`opspilot_worker_lab/opspilot-worker` 3 个；owner 只有 `lab-migrate` 2、`lab-schema-check` 2、`lab-monitor` 18、`lab-readonly` 17。`pg_stat_activity` 共 19 次快照（03:21:26–03:24:26），非 owner 用户只有这两个，带 `opspilot-*` 应用名的 owner 会话为 0。03:21:58 用 `lsof` 对照：web PID 99835 → 61384、61385（`opspilot_web_lab`），worker PID 913 → 61443、61444、61446（`opspilot_worker_lab`） | 通过 |
| 不含内容 | 证据中没有 reasoning、正文、提示词 | `model_calls` 只有合同的 9 个键。trace 回读只读取 `otel.span.opspilot.model.*` 元数据、`invocation_params.max_tokens`、起止时间和 token 数，不读取 inputs/outputs。summary.json 不含结论正文 | 通过 |

## 偏差

- PG 端口用 55503、web 端口用 8798，避免与同机其他运行冲突
- 运行前的一条只读查询把 `opspilot_postmortem_versions` 的列名写错了（`incident_id`），在 PG 日志中留下 1 条 owner `lab-readonly` 的 ERROR（42703）。随后改用不带条件的计数重查，结果为 0。这条错误不来自 web 或 worker，所以 summary.json 的 `pg_log` 把全日志计数和 `web_worker` 计数分开记录
- trace 回读脚本新增读取 `invocation_params.max_tokens` 和请求起止时间；summary.json 用 `per_request`、`attempt_result`、`success_criterion` 替换了上一轮的 `issue_202_answer`。`three_way_agreement` 在冻结时就带有 `trace_derived` 和 `field_checks`，不是事后补算的
- round 名称中的日期 20261011 是本地日期，UTC 日期已经是 10-12
- 本机找不到 gitleaks，改用字面匹配（见「凭据」）

## 失败与限制

- 没有失败项，没有重试，没有改代码
- 首次请求的输出（11703）低于旧上限 16384。这次单个样本说明 65536 下首次请求能正常结束，但不能说明旧上限下一定会截断；上一轮同一输入的首次请求在 16384 处截断。只有一个事故、一次生成，不代表分布
- 没有观测到接近满额（65536）的输出，所以满额时的用时、它与 360 s 超时和 900 s 租约之间的余量都未确认（D58）
- 修正请求、`length` 截断、超时、`OUTPUT_INVALID`、引用失败这些结局本次都没有触发，只由合同测试覆盖
- 数据来自 #169 运行库的副本，不是生产库

## 费用

DeepSeek 余额 03:21:19Z 为 3.12 CNY。03:24:51Z 冻结摘要时读到的仍是 3.12（summary.json 的 `balance_after`），03:25:07Z 再读为 3.02，差 0.10 CNY。余额更新有延迟；同一账户这段时间里有其他会话在运行，所以 0.10 CNY 只是本次费用的上界。上一轮 token 量约为本次两倍，差额同为 0.10，说明这个差额里可能混有其他会话的费用。本次 token：53313 输入 / 11703 输出（其中 reasoning 6437），尝试行与 trace 一致。

## 凭据

证据中没有 token、口令、API key，也没有幂等键原值。本机没有 gitleaks。做法是把私有 env 文件、Prometheus 账号文件、webhook token 文件、实验 UI 账号与口令文件、目标身份文件里长度 ≥ 6 的值，逐一在本目录做字面匹配，不打印原值。值按整值和按分隔符拆开的片段分别匹配，共 36 个。命中的都是已知的非机密值：`LANGSMITH_WORKSPACE_ID`，等于 trace 链接里的组织 ID `/o/c7727675-…`，已在之前的证据中入库；`LANGSMITH_PROJECT` 的值 `default`，命中的是 `default_transaction_read_only` 里的普通单词；`.env` 中数据库 URL 的主机、库名和用户名片段（`127.0.0.1`、`m0_budget`、`m0_lab`）；UI 用户配置中的账号名 `pgroles-lab`，不含口令；目标身份文件里的 JSON 结构片段 `"version": 1,`。口令、API key、token 没有命中。
