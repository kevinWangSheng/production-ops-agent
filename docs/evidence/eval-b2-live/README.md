# PR #229（评测 PR-B2）有界真实运行：默认路径与录制路径

- 日期：2026-10-14。第 1 轮 20:00–20:10Z 在 commit `acedfa3` 上跑；第 2 轮 20:21–20:27Z 在窗口规则修复后的 commit `e145246` 上跑。两轮运行期间，工作区除忽略的 `tmp/` 外没有改动，commit 与各摘要的 `versions.commit` 一致。分支 `feature/eval-b2-replay`
- 类别：有界真实软件环境运行加真实模型调用（`deepseek-flash`）。这不是生产证明，也不是 B2 的独立验收
- 冻结摘要（ADR-0006）：
  - 默认路径：[live-runs/ec6bcb1e-…/summary.json](live-runs/ec6bcb1e-d838-41ce-bfff-ef610b14e596/summary.json)
  - 第 1 次录制，被拒：[live-runs/4cf3a8ee-…/summary.json](live-runs/4cf3a8ee-ae47-4184-9de0-6f69d284f95f/summary.json)
  - 第 2 次录制，完整：[live-runs/3b1d7fcd-…/summary.json](live-runs/3b1d7fcd-640f-4711-814b-a92898bba3a9/summary.json)
  - 原始 ledger 在忽略目录 `tmp/lab-ledgers/eval-b2-live/` 下，录制包在 `tmp/eval/eval-b2-live-<run_id>/` 下，都不入库
- **结论**
  - **默认路径通过**
  - **第 1 次录制被 B2 自己的核验拒绝**，判为 `IDENTITY_MISMATCH`，录制没有完成。这次失败暴露了一个缺陷，见下文「第 1 次录制被拒」
  - **修复后的第 2 次录制通过**：`complete=true`，结构回放 `structural_verified=true`，回放期间模型调用 0 次

## 怎么跑的

- 驱动是 `tmp/eval-b2-live/driver.py`（忽略目录，不入库），按 `scripts/m1_live_runner.py` 的组合改写：`InvestigationRunner` + PostgreSQL + `otel-demo` 工具 profile（真实 Prometheus/Jaeger 只读查询），`OPSPILOT_TRACE=lab`。问题文本是「Checkout is failing for users…report the most likely cause」
- PostgreSQL 17.9：在本 worktree 的 `tmp/eval-b2-live/pg` 新建临时实例，端口 55497，`opspilot.schema migrate` 迁到 head `0015_release_observation_sessions`。每轮跑完都执行 `pg_ctl stop`。没有使用 55431 的 lab 实例
- kind `opspilot-m1`：两轮开跑前，frontend 的 2 分钟 span rate 分别为 2.28/s 和 1.35/s，都大于 0，没有重启 otel-collector
  - 每轮用 `kind_lab.py fault inject` 注入故障（paymentFailure → 100%），experiment-id 为 `eval-b2-live-20261014` 和 `-r2`。等 payment 的错误 span rate 大于 0 后开跑
  - 每轮跑完执行 `fault restore`，ConfigMap 回到 `1ee5c025…`，结果为 `verified`
- 防循环上限与超时都用产品默认值，没有放宽：每 Run 最多 100 次模型请求，Run 墙钟 7200 s
- 凭据：DeepSeek 与 LangSmith 凭据经 `scripts/m1_live_flash_loop.read_key` / `load_langsmith_env` 从 `M0_ENV_FILE` 读取。Prometheus 调查侧账号用 `set -a; . tmp/m1-kind-lab/prometheus-investigator.env` 载入。没有打印凭据值
- trace：round `eval-b2-live-20261014`，project `opspilot-lab-eval-b2-live-20261014`，`trace_tier=longlived`

## 结果

| 项 | 默认路径（hooks 为 None） | 第 1 次录制（`acedfa3`） | 第 2 次录制（`e145246`） |
|---|---|---|---|
| Run | `ec6bcb1e-…` | `4cf3a8ee-…` | `3b1d7fcd-…` |
| 调查结果 | `published` / `completed`，没有交接 | `published` / `completed`，没有交接 | `published` / `completed`，没有交接 |
| 模型请求 / 工具调用 | 8 / 12 | 8 / 14 | 9 / 16 |
| token（输入 / 输出） | 233054 / 20962 | 132532 / 18415 | 142360 / 19735 |
| 报告 | `supported`：payment `Charge` 以「Invalid token」拒绝扣款 → checkout gRPC 13 → HTTP 500 | `partial`：失败链相同，触发原因未查明 | `partial`：PlaceOrder 失败与下游 `PaymentService/Charge` 失败逐一对应，失败链相同 |
| 录制 | 不适用 | `complete=false`，`IDENTITY_MISMATCH` | **`complete=true`**，没有 `INCOMPLETE` 标记，recording.json sha256 `aaf08d18…0efe79`，51551 字节 |
| 事件计数 | 不适用 | 44：model 8、http 8、tool 14、delivery 14 | 50：model 9、http 9、tool 16、delivery 16；原始输入 1 份 |
| 结构回放 | 不适用 | 未执行 | **`structural_verified=true`**，`error=null`，attempt `ran`，没有 `replay_miss`，回放期间模型调用 0 次 |
| trace | [根 run](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/b8905490-920a-4201-b1f1-d82639fc6f34/r/00000000-0000-0000-1d14-45c6f7012815?poll=true)，OTel `2bb63b65…` | [根 run](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/b8905490-920a-4201-b1f1-d82639fc6f34/r/00000000-0000-0000-b16d-494570b95a77?poll=true)，OTel `5acfaf8f…` | [根 run](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/b8905490-920a-4201-b1f1-d82639fc6f34/r/00000000-0000-0000-df30-1cbf19978416?poll=true)，OTel `8b21b01c…` |

三个 Run 的 trace 都没有丢弃 span。

默认路径：驱动断言 `DeepSeekClient.capture_http is None`，runner 构造的 `InvestigationLoop` 也没有 `capture_delivery`，所以 Run 从领取、工具调用到发布都走产品默认路径。这一次 Run 只能证明挂点为 None 时，真实调查能端到端完成并发布结论；不能证明挂点「不改变行为」。各 Run 的报告不同，属于模型输出的正常波动，不能据此比较几条路径。

第 2 次录制中，每次 model 事件都恰好有 1 个 http 子事件。共有 3 次 `traces_search` 返回 view-token 模式的 `RESULT_TOO_LARGE`，分别为 240096、79877、38329 token，上限 25000。所以第 1 次录制被拒的那条规则，这次确实被真实数据覆盖到了。结构回放流程是：`load_recording` 按 `record()` 返回的 sha256 重新读包并逐件核验，再调用 `replay(mode="structural")`，attempt 的 `product_run_id` 为 null。

## 第 1 次录制被拒（缺陷证据，已在 `e145246` 修复）

- 第 1 次录制的录制包内容见上表。`record()` 抛出 `IDENTITY_MISMATCH`，`recording.json` 写入了 `complete=false`、`incomplete_reasons=["IDENTITY_MISMATCH"]`，`INCOMPLETE` 标记保留
- 定位：离线复跑 `validate_recording`，没有调用模型。失败出在 `opspilot/evaluation/tool_provenance.py` 的 `_check_view`，它要求视图的 `window` 等于 `operation.window`
  - 本次有 3 次 `traces_search` 返回 view-token 模式的 `RESULT_TOO_LARGE`（165118、76062、34099 token）
  - 产品 `opspilot/tools/executor.py` 的 `_view_refusal_detail` 会有意把视图的 `window` 写成实际查询窗（例如 `19:06:55–20:06:55`），而 `operation.window` 是 24 小时授权窗，两者不相等
- 这是 B2 合同和产品行为之间的冲突。接口文档 P2 修复合同的原文是「window 必须等于已重建 operation.window」；产品的 docstring 则说明，写查询窗是为了让模型知道是哪一次调用被拒。在修复前，只要真实 Run 遇到 view-token 拒绝，录制就无法完成，而 traces 查询很容易遇到这种拒绝
- 当时的离线验证：只放宽这一条，即在 token 模式下允许 `view.window` 等于 `operation.query` 的 start/end，其余规则不变，同一个录制包就能通过 `validate_recording`。所以这是唯一的阻断项
- 修复：`e145246` 只在 view-token 超限提示里，接受从冻结请求重建出来的实际查询窗。在 `e145246` 上离线复跑 `validate_recording`，同一个 4cf3a8ee 包能通过核验（包本身仍带 `complete=false` 和 `INCOMPLETE` 标记，不作完整包使用）

## 驱动接线缺口（留给 PR-C）

- **没有 `capture_delivery` 注入点**：`InvestigationRunner` 不接收 `capture_delivery`。驱动在 Run 期间把 `opspilot.investigation.runner.InvestigationLoop` 临时替换为 `functools.partial(InvestigationLoop, capture_delivery=hooks.delivery)`，Run 结束后恢复。这个注入只发生在驱动内部；正式接线需要 runner 提供传入口
- **取不到公开的注册 DTO**：`bind_tool_snapshots` 需要注册与目标 DTO，但接口里没有公开的取得入口，驱动只能调用私有的 `otel_demo._registrations()` / `_target()`
- 其他驱动自定的口径：
  - 工具快照必须在首次调用前冻结，所以驱动按首次领取（epoch 1，各 generation 为 0）预先构造 `QueryScope`，并在 executor factory 里断言实际 scope 与预测一致
  - `profile_hash` 取 profile versions 的 canonical SHA256，`projection_hash` 取 `opspilot/tools/otel_demo.py` 文件的 SHA256
- 首次尝试录制时，`_create` 报 `EVIDENCE_UNAVAILABLE`，原因是 `tmp/eval/` 父目录不存在。这次尝试没有发起模型或工具调用；手动 `mkdir -m 700 tmp/eval` 后重跑

## 费用

| 时间（UTC） | 余额（CNY） | 说明 |
|---|---|---|
| 20:00:48 | 200.82 | 第 1 轮前 |
| 20:09:51 | 200.66 | 第 1 轮后（默认路径 + 第 1 次录制） |
| 20:21:43 | 200.56 | 第 2 轮前 |
| 20:26:46 | 200.56 | 第 2 轮后 |

- 第 1 轮后到第 2 轮前，余额又少了 0.10 CNY，但这段时间本驱动没有发起模型调用。这笔扣款应该是第 1 轮的延迟入账，但无法排除同一 key 被其他进程使用
- 第 2 轮跑完后余额没有变化，第 2 轮的费用多半尚未入账
- 截至 20:26:46Z，三个 Run 合计余额差 0.26 CNY，这个数字可能偏低
- 驱动按 token 估算的上限是 0.69 + 0.45 + 0.48 CNY，没有计入缓存命中折扣
