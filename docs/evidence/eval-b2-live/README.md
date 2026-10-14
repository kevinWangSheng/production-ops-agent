# PR #229（评测 PR-B2）有界真实运行：默认路径与录制路径

- 日期：2026-10-14 20:00–20:10Z；分支 `feature/eval-b2-replay`，代码运行的 commit `acedfa3`（运行期间工作区除忽略的 `tmp/` 外没有改动；与摘要 `versions.commit` 一致）
- 类别：有界真实软件环境运行加真实模型调用（`deepseek-flash`）。这不是生产证明，也不是 B2 的独立验收
- 冻结摘要（ADR-0006）：默认路径 [live-runs/ec6bcb1e-…/summary.json](live-runs/ec6bcb1e-d838-41ce-bfff-ef610b14e596/summary.json)，录制路径 [live-runs/4cf3a8ee-…/summary.json](live-runs/4cf3a8ee-ae47-4184-9de0-6f69d284f95f/summary.json)。原始 ledger 在忽略目录 `tmp/lab-ledgers/eval-b2-live/` 下，录制包在 `tmp/eval/eval-b2-live-4cf3a8ee-…/` 下，都不入库
- **结论：默认路径通过。录制路径的真实 Run 正常发布结论，但录制被 B2 自己的核验判为不完整（`IDENTITY_MISMATCH`），所以结构回放没有执行。** 原因见下文「录制失败」

## 怎么跑的

- 驱动是 `tmp/eval-b2-live/driver.py`（忽略目录，不入库），按 `scripts/m1_live_runner.py` 的组合改写：`InvestigationRunner` + PostgreSQL + `otel-demo` 工具 profile（真实 Prometheus/Jaeger 只读查询），`OPSPILOT_TRACE=lab`。问题文本是「Checkout is failing for users…report the most likely cause」
- PostgreSQL 17.9：在本 worktree 的 `tmp/eval-b2-live/pg` 新建临时实例，端口 55497，`opspilot.schema migrate` 迁到 head `0015_release_observation_sessions`，跑完后 `pg_ctl stop`。没有使用 55431 的 lab 实例
- kind `opspilot-m1`：开跑前 frontend 的 2 分钟 span rate 为 2.28/s（之后 3.07/s），没有重启 otel-collector。`kind_lab.py fault inject --experiment-id eval-b2-live-20261014`（paymentFailure → 100%），payment 错误 span rate 到 0.05/s 后开跑，两个 Run 跑完后 `fault restore`，ConfigMap 回到 `1ee5c025…`，`verified`
- 防循环上限与超时都用产品默认值，没有放宽：每 Run 最多 100 次模型请求，Run 墙钟 7200 s
- 凭据经 `scripts/m1_live_flash_loop.read_key` / `load_langsmith_env` 从 `M0_ENV_FILE` 读取；Prometheus 调查侧账号用 `set -a; . tmp/m1-kind-lab/prometheus-investigator.env`。没有打印凭据值
- trace：round `eval-b2-live-20261014`，project `opspilot-lab-eval-b2-live-20261014`，`trace_tier=longlived`

## 结果

| 项 | 默认路径（hooks 为 None） | 录制路径（`record()` + `CaptureHooks`） |
|---|---|---|
| Run | `ec6bcb1e-d838-41ce-bfff-ef610b14e596` | `4cf3a8ee-ae47-4184-9de0-6f69d284f95f` |
| 结果 | `published`，`execution=completed`，没有交接，Run `completed` | `published`，`execution=completed`，没有交接，Run `completed` |
| 模型请求 / 工具调用 | 8 / 12 | 8 / 14 |
| token（输入 / 输出） | 233054 / 20962 | 132532 / 18415 |
| 报告 | `conclusion=supported`：payment `Charge` 以「Invalid token」拒绝扣款，checkout 报 gRPC 13，前端返回 HTTP 500，错误只出现在窗口最后一个时间桶 | `conclusion=partial`：同一条失败链（payment `charge.js:37` 「Invalid token」→ checkout gRPC 13 → HTTP 500），触发原因未查明 |
| trace | [根 run](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/b8905490-920a-4201-b1f1-d82639fc6f34/r/00000000-0000-0000-1d14-45c6f7012815?poll=true)，OTel `2bb63b65821b12e3a14d5c8da8d74488`，丢弃 span 0 | [根 run](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/b8905490-920a-4201-b1f1-d82639fc6f34/r/00000000-0000-0000-b16d-494570b95a77?poll=true)，OTel `5acfaf8f19d2ba9f8645b489d1fc27a3`，丢弃 span 0 |

默认路径：驱动断言 `DeepSeekClient.capture_http is None`，runner 构造的 `InvestigationLoop` 也没有 `capture_delivery`，Run 从领取、工具调用到发布都走产品默认路径。只凭一次 Run，不能证明挂点「不改变行为」，它证明的是挂点为 None 时真实调查能端到端完成并发布结论。两次 Run 的报告不同，属于模型输出的正常波动，不能据此比较两条路径。

## 录制失败

- 录制包内容：44 个事件，model 8、http 8、tool 14、delivery 14，原始输入 1 份。每次 model 事件恰好对应 1 个 http 子事件。`record()` 抛出 `IDENTITY_MISMATCH`，`recording.json` 写入了 `complete=false`、`incomplete_reasons=["IDENTITY_MISMATCH"]`，`INCOMPLETE` 标记保留
- 定位（离线复跑 `validate_recording`，没有调用模型）：失败出在 `opspilot/evaluation/tool_provenance.py` 的 `_check_view`，它要求视图的 `window` 等于 `operation.window`。本次有 3 次 `traces_search` 返回 view-token 模式的 `RESULT_TOO_LARGE`（165118、76062、34099 token，上限 25000）。产品 `opspilot/tools/executor.py` 的 `_view_refusal_detail` 会有意把视图的 `window` 写成实际查询窗（例如 `19:06:55–20:06:55`），而 `operation.window` 是 24 小时授权窗，两者不相等
- 这是 B2 合同和产品行为之间的冲突。接口文档 P2 修复合同（第 120 行）写的是「window 必须等于已重建 operation.window」，产品的 docstring 则说明这是为了让模型知道是哪一次调用被拒。只要真实 Run 遇到 view-token 拒绝，录制就无法完成，而 traces 查询很容易遇到
- 离线验证：只放宽这一条（token 模式下允许 `view.window` 等于 `operation.query` 的 start/end），其余规则不变，同一个录制包就能通过 `validate_recording`。所以这是唯一的阻断项。这只是一次诊断，没有修改产品代码或 evaluation 代码
- 结构回放（`mode="structural"`）：`record()` 没有返回 ref，`load_recording` 也会拒绝带 `INCOMPLETE` 标记的包，所以**没有执行**

## 驱动接线说明

- `InvestigationRunner` 没有 `capture_delivery` 参数。驱动在 Run 期间把 `opspilot.investigation.runner.InvestigationLoop` 临时替换为 `functools.partial(InvestigationLoop, capture_delivery=hooks.delivery)`，结束后恢复。这个注入只发生在驱动内部，以后正式接线需要 runner 提供传入口
- 工具快照需要在调用前冻结，所以驱动按首次领取（epoch 1，各 generation 为 0）预先构造 `QueryScope`，并在 executor factory 里断言实际 scope 与预测一致。注册快照用的是 `otel_demo._registrations()` / `_target()`，这两个是私有函数，因为接口里没有公开取注册 DTO 的入口
- `profile_hash` 取 profile versions 的 canonical SHA256，`projection_hash` 取 `opspilot/tools/otel_demo.py` 文件的 SHA256。两者都是驱动自定的口径
- 第一次录制尝试在 `_create` 处报 `EVIDENCE_UNAVAILABLE`，因为 `tmp/eval/` 父目录不存在，所以没有发起模型或工具调用。手动 `mkdir -m 700 tmp/eval` 后重跑

## 费用

DeepSeek 余额 200.82 CNY（20:00:48Z）→ 200.66 CNY（20:09:51Z），差 0.16 CNY。驱动按 token 估算的上限是 0.69 + 0.45 CNY，没有计入缓存命中折扣。余额可能有入账延迟，以余额差为准。
