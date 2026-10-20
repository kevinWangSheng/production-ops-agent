# judge 流式传输（SSE）合同预审

- 状态：预审完成，待用户/lead 裁决（决定表 r0）；未实施
- 更新日期：2026-10-20
- 依据：用户裁决 2026-10-20（judge 改流式保活，零重试，900 s / 65536 / 8 MiB / 2 MiB 不变）；[B3 接口](2026-10-14-eval-pr-b3-interface.md) L16–L22；[P7 校准记录](2026-10-18-eval-p7-calibration.md) L55（debug 第一轮诊断）
- 工作区：`chore/judge-streaming`，`/Users/shenghuikevin/dev/AI/poa-judge-stream`（基线 `6c1cd22`）

## 目标与范围

zai 非流式调用 5–12 min 无字节，本机 Clash TUN 下长静默连接被断（`JUDGE_TRANSPORT_ERROR`）。改为 `stream: true` 的 SSE 读取，使推理期间持续有字节。只改 judge 传输/组装与相应 B3 条款；不改 P6 判分、复核、校准计数、prompt。预审者未参与实现，也不实施。

## 已核对的事实

代码（基线 `6c1cd22`）：
- `opspilot/evaluation/judge.py:429-442` 请求体固定 `"stream": False`；`:172,191` adapter_version 只接受 `judge-adapters-1`，hash 覆盖全部非密钥字段（`:213-226`）。
- `:327-366` `HTTPJudgeTransport`：`asyncio.timeout(900)` 总墙钟 + httpx2 `timeout=900`（逐操作），`retries=0`、`follow_redirects=False`、`trust_env=False`；`aiter_bytes` 累计原始字节超 `max_response_bytes` 抛 `JUDGE_RESPONSE_OVERSIZE`；非 2xx 也读完正文。
- `:597-625` `judge_once` 再独立计时、计字节，拼接后交 `_output`；`:509-559` `_output` 先查 model 身份、单 choice、index=0、finish_reason=stop、role、tool_calls/function_call，最后才取 usage → 失败时 usage 恒为 None（与诊断「失败时为 null」一致）；`JudgeObservation`（`:461-473`）无 finish_reason 字段。
- 断连：httpx2 非超时异常落入 `:646` `except Exception` → `error/JUDGE_TRANSPORT_ERROR`。
- 测试钉住：`tests/acceptance/test_eval_b3_judge.py:478`、`tests/unit/test_eval_b3_impl.py:337` 断言 `body["stream"] is False`；`test_eval_b3_impl.py:496-540` 用 `httpx2.MockTransport` 喂非 SSE 正文（`b"x"*20` 超限用例在 SSE 下语义变）；其余假 transport（`tests/acceptance/eval_b3_support.py:190-195,253-271` 的 `http()/completion()`、`eval_p7_support.py:126-145` monkeypatch `HTTPJudgeTransport.send` 返回 `label_reply`）都直接返回**非流式 chat.completion JSON**。`judge-adapters-1` 字面值未被测试钉住；`test_eval_b3_impl.py:1020-1035` 枚举 config 字段。

官方文档：
- Z.ai：`stream` 为 true 时 Event Stream，结束发 `data: [DONE]`；finish_reason 枚举含 `stop/tool_calls/length/sensitive/model_context_window_exceeded/network_error`；未提 stream_options、keep-alive、流中错误格式（https://docs.z.ai/api-reference/llm/chat-completion）。流式示例每块顶层带 `id/created/model`，`delta.content` 与 `delta.reasoning_content` 增量，finish_reason 与 usage 只在末块，末块 `delta.role="assistant"`（https://docs.z.ai/guides/capabilities/streaming）。
- DeepSeek：`data:` SSE、`data: [DONE]` 结束；`stream_options.include_usage` 仅 stream=true 可用，否则 400；`id/model` 每块相同；`delta.reasoning_content`（thinking 模式）；页面称 usage 随末个内容块、无单独 usage 块（https://api-docs.deepseek.com/api/create-chat-completion）。排队期间流式返回 `: keep-alive` 注释，非流式返回空行；10 min 未开始推理则断开（https://api-docs.deepseek.com/quick_start/rate_limit）。
- OpenAI：chunk 每块同 `id`、带 `model`；`delta` 含 content/role/refusal/tool_calls；`include_usage` 时 usage 仅末块非 null，末块 `choices` 可为空；流中断则可能收不到 usage 块（https://developers.openai.com/api/reference/resources/chat/subresources/completions/streaming-events）。

未确认：OpenAI 推理模型在 Chat Completions 流里思考阶段是否有字节（无 reasoning delta，若无 keep-alive 则仍可能长静默）；OpenAI 页面本次未取到 `[DONE]` 原文；Z.ai 是否接受/需要 `stream_options`、是否发 keep-alive 注释、流中错误的事件格式；三家 HTTP 错误在 stream=true 时是否仍为普通 JSON 正文；DeepSeek usage 是否在 include_usage 时出现独立空 choices 块（文档两处描述不同）。均需第一次真实调用核实。

## 决定表（r0）

A 组：需用户裁决（合同、上限语义、持久化 schema、验收测试）

| 编号 | 问题 | 选项与代价 | 推荐 | 影响面 |
|---|---|---|---|---|
| S1 | 哪些 provider 流式 | a 三家都流式：一条路径、校准对照同口径；OpenAI 是否真保活未确认 / b 仅 zai：改动小，但 adapter 分叉、D16 对照传输不同 | a | adapter、B3 L20 |
| S2 | `max_response_bytes`=2 MiB 量什么 | SSE 每事件约 190 B，65536 token 推理约 12 MiB 线上字节，原口径必超限。a 2 MiB 量组装后的 completion 文档（content+元数据，推理丢弃不计），另设固定线上上限 32 MiB 与单事件 1 MiB，超出均 `JUDGE_RESPONSE_OVERSIZE` / b 2 MiB 仍量线上字节：长推理必 oversize，等于没修 / c 放大 2 MiB：违背「上限不变」 | a；32 MiB 写为代码常量并由 adapter 版本承载，不新增 config 字段 | transport、B3 L16/L22 |
| S3 | `JudgeTransport.send` 接口 | a 签名不变；`HTTPJudgeTransport` 内部解析 SSE，返回的 `chunks` 为组装后的非流式 chat.completion JSON（纯函数 `assemble_sse(...)` 可单测）；`judge_once/_output` 与全部假 transport 不变 / b chunks 携带原始 SSE 字节、`judge_once` 解析：所有假回复要改写为 SSE，验收作者大面积返工 | a | B3 L22、测试 |
| S4 | 请求体 | `"stream": true`；openai、deepseek 加 `stream_options:{"include_usage":true}`；zai 不加（文档未列该参数，usage 本在末块）；加 `Accept: text/event-stream` 头 | 如左 | adapter、两处 `stream is False` 断言改 True（由验收作者改） |
| S5 | adapter 版本 | `judge-adapters-1` → `-2`，`_check_config` 只认 `-2`；hash 随之变化，`p7-debug-1` 记录与新 config 不可比（未正式校准，无已认证件受影响） | bump | config_hash、P7 debug 工件 |
| S6 | 失败时可观察性 | a `JudgeObservation` 新增 `finish_reason: str\|None`（默认 None），usage 与 finish_reason 只要收到末块就记录，不论之后 invalid；calibration/P7 DebugRecord 的严格解码接受此字段 / b 不改 DTO，只改 usage 提前提取 | a（诊断已证明需要；旧件缺字段按 None 读） | B3 L22/L34、calibration_flow、eval_p7 |

B 组：可逆默认（Agent 自定，一行理由）

| 编号 | 问题 | 推荐与理由 |
|---|---|---|
| S7 | SSE 分帧 | 按 WHATWG：字节缓冲到行尾再 UTF-8 解码（非法即 `JUDGE_OUTPUT_INVALID`），兼容 `\n`/`\r\n`/`\r`；`:` 开头为注释（keep-alive）忽略；`data:` 去一个前导空格，同事件多行以 `\n` 连接；空行派发；`id:`/`retry:` 忽略；`event:` 缺省或 `message` 以外 → `JUDGE_OUTPUT_INVALID`。理由：标准行为，keep-alive 已有官方出处 |
| S8 | 终止 | 必须收到 `data: [DONE]`；收到即停止读取并关闭连接；EOF/断连在 `[DONE]` 前 → `error/JUDGE_TRANSPORT_ERROR`（与现行断连同码），零重试。理由：三家文档均以 `[DONE]` 结束，未完成的流不能当完整答案 |
| S9 | 200 的 Content-Type | 非 `text/event-stream`（忽略参数）→ `JUDGE_OUTPUT_INVALID`，不回退按非流式解析。理由：B3 不做宽松回退 |
| S10 | 非 2xx | 照旧读正文（受 S2 上限）后 `JUDGE_HTTP_ERROR`，不解析 SSE。理由：沿用现码 |
| S11 | 流中错误 | 事件 JSON 顶层含 `error` 键 → `error/JUDGE_HTTP_ERROR`；非对象/坏 JSON → `JUDGE_OUTPUT_INVALID`。finish_reason 为 `network_error`/`insufficient_system_resource` 等仍按非 stop → invalid，靠 S6 字段区分。理由：不扩大错误码集合 |
| S12 | 组装 content/推理 | 只拼接 `delta.content`；`reasoning_content` 只计入线上字节后丢弃，不保留不落盘；`delta.role` 若出现必须为 `assistant`、全程至少出现一次；`tool_calls`/`function_call` 非 null 或 `refusal` 非 null → 组装进文档由 `_output` 判 invalid；其他未知 delta 键忽略。理由：沿用 B3「只消费 content、私有协议不保留」 |
| S13 | finish_reason / usage / choices | 每块 `choices` 长度 ≤1 且 index=0（否则组装为多 choice 交 `_output` 拒绝）；非 null finish_reason 恰一次且其后只允许无 content 的 usage 块；usage 取最后一个非 null 对象；`choices` 为空只允许在 finish 之后。理由：覆盖 OpenAI 空 choices 末块与 Z.ai/DeepSeek 末块带 usage 两种形态 |
| S14 | 响应身份 | 每个数据块必须带 `model` 且全程字符串相同、`id` 全程相同；不一致时组装文档 `model=null`，由 `_output` 按 provider 给出 `JUDGE_IDENTITY_MISMATCH` 或 `JUDGE_RESPONSE_IDENTITY_INVALID`；一致则原规则 `response_identity_matches`。理由：复用现有分类，不在 transport 里重复 provider 逻辑 |
| S15 | 超时 | 保持单一 900 s 总墙钟（asyncio deadline + judge_once 复核），不加空闲超时；httpx2 逐操作超时仍 900。理由：OpenAI 思考阶段可能长时间无字节，空闲超时会误杀，且零重试下提前失败不省钱 |

## B3 合同改动（`2026-10-14-eval-pr-b3-interface.md`）

1. L16：`max_response_bytes=2097152` 后补「流式下指组装后 completion 文档字节；线上 SSE 字节另有固定 32 MiB、单事件 1 MiB 上限，同为 oversize」；adapter_version 写 `judge-adapters-2`。
2. L20：`stream=false` 改为「stream=true；OpenAI/DeepSeek 附 `stream_options.include_usage=true`；Accept `text/event-stream`」；「响应需 model 匹配…」前补「按 SSE 组装后」，并写入 S12–S14 规则；usage 句后补「末块已收到即记录，含失败」。
3. L22：transport 句补「HTTPJudgeTransport 解析 SSE（S7–S11），返回组装后的非流式 completion JSON；未收到 `[DONE]` 即断开为 TRANSPORT_ERROR；超时仍为总墙钟」；假 transport 返回组装后文档即可。
4. L34：`JudgeObservation` 字段列表加 `finish_reason`（S6）。
5. 「对草案的可逆接口调整与理由」节加一行：流式保活理由与本记录链接。

## 完成条件与下一步

- 完成条件：S1–S6 裁决写入本记录 r1；独立验收作者按 r1 改两处 `stream is False` 并新增 SSE 组装合同测试（keep-alive 注释、跨块拆行、无 `[DONE]` 断开、流中 error、身份跨块不一致、推理超 2 MiB 但 content 小仍 ok、线上超 32 MiB oversize、失败时 usage/finish_reason 已记录）；`test_eval_b3_impl.py:496-540` MockTransport 用例改 SSE 正文；`eval_p7_support` 假 transport 不变。触碰 judge 传输属 ADR-0006 范围：PR 附一次有界真实 debug 调用（每家 1 次即可核实上面的未确认项）。
- 下一步：lead 收集裁决 → 实现者开工（本预审者不实施）。
