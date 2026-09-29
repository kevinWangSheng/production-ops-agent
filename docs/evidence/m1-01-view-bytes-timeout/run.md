# M1-01 限制审计剩余项：视图字节计量与 1M 上下文单次请求延迟

- 日期：2026-09-29（UTC 10:07 起，DeepSeek 非高峰时段）
- 授权：AGENTS.md「费用与真实调用」常设授权；只调模型，无 lab、无 PostgreSQL。
- 驱动：`scripts/m1_context_latency.py`，经产品自己的 `DeepSeekClient` 与 `serialized_request`（thinking 开、`reasoning_effort=high`、`stream=false`、tools 挂载），客户端超时放宽到 700 s 以便观察超过 360 s 的情形。
- 凭据只由脚本从私有 env 读取，账本只记体量、usage、耗时，不含提示或回复文本；已对全部文件 grep `sk-` 无命中。
- 内容为合成的指标形状 tool 视图（每条约 80 KB，低于 100 KiB 上限），每个请求内容与 system 首行随机串唯一，避免 KV 缓存命中。

## 文件

| 文件 | 内容 |
|---|---|
| `ledger.json` | 预填充阶梯：约 100k / 300k / 600k / 900k prompt tokens，各 2 次 |
| `smoke-100k.json` | 首次试跑（含一个 8 视图的校准请求，实为 279k tokens） |
| `decode-32k.json` | 解码探针第一次：模型 1042 tokens 后自行停止，无法测解码速率（保留为失败尝试） |
| `decode-64k.json` | 约 105k 上下文，要求输出到 `max_tokens=65536`，输出打满 |
| `decode-64k-at-975k.json` | 约 975k 上下文，同样打满 65,536 输出（前缀由前序请求缓存，见下） |
| `real-view-tokens.json` | 真实录制 fixture 经执行器得到的视图，在 DeepSeek 上的真实 token 数 |
| `view-overhead-measurement.txt` | 经真实执行器路径测得的 metrics 整视图字节与上限的差 |

`decode-64k-at-975k.json` 的先前一次同参数试跑（模型只答 41 tokens）被同名文件覆盖；补充的修正是把解码指令在长历史末尾重复一次。此前试跑未落盘。

## 预填充延迟（冷缓存，`ledger.json`）

| 目标 | 实测 prompt_tokens | 请求字节 | 耗时（2 次） |
|---|---|---|---|
| 100k | 104,773 | 263,451 | 2.26 s（另 1.80 s 那次命中缓存 104,576 tokens，不计入） |
| 300k | 313,655–313,657 | 790 KB | 6.27 s / 6.49 s |
| 600k | 661,798 | 1.67 MB | 12.59 s / 12.92 s |
| 900k | 975,123 | 2.46 MB | 21.25 s / 20.80 s |

约 21 s / 975k tokens，近似线性；全部 HTTP 200，无错误、无超时。所有请求 `response_model` 均通过客户端校验。请求字节最大 2.46 MB，低于 `MAX_HTTP_REQUEST_BYTES`（8 MiB）。

## 解码延迟（打满 `MAX_OUTPUT_TOKENS`=65,536）

- 105k 上下文：150.99 s，65,536 completion tokens（约 434 tokens/s，含约 2 s 预填充）。
- 975k 上下文：155.49 s，65,536 completion tokens（约 421 tokens/s）。该次前缀已缓存（`prompt_cache_hit_tokens` 974,976），所以 155 s 基本是纯解码；冷预填充另加约 21 s。
- 提供方接受了 975,182 prompt + 65,536 max_tokens = 1,040,718 tokens 的请求，没有按 1M 拒绝。

## 视图真实 token 数（`real-view-tokens.json`）

同一条视图重复 2 次与 22 次的 `prompt_tokens` 差除以 20：metrics fixture 视图 1,409 B → 605 tokens（2.33 B/token）；traces fixture 视图 58,357 B → 19,097 tokens（3.06 B/token）。合成指标视图（`ledger.json`，含 9 位小数字符串）约 2.5 B/token。差值含每条 assistant/tool 消息的固定包装（十几个 tokens），对 metrics 小视图占比较大，对 traces 大视图可忽略。

## 费用

- 用量：`ledger.json` 4,215,472 prompt + 567 completion tokens；两次解码探针另 105k+65.5k 与 975k（大部分缓存命中）+65.5k；`smoke-100k.json` 另 279k+105k。
- 定价页（2026-09-29 核对）deepseek-flash 非高峰缓存未命中 $0.15 / 1M 输入、$0.6 / 1M 输出；按全部 token 一律按未命中计的保守上界，预填充阶梯约 $0.63，解码探针与试跑合计约 $0.3，总上界约 $0.9。
- 余额差：CNY 23.94（`ledger.json` 前后，余额接口读数滞后）→ 23.55（阶梯跑完后单独查询），差 0.39。同账户有其他执行者并发调用，此差值不能归到本任务，也不能与上界对账；记为未对账。
