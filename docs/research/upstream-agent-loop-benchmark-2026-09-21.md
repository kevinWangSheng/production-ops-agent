# 上游 Agent loop 与 PR #29 对标调研

日期：2026-09-21。目标是核对 PR #29 的手写调查循环，看看上游项目如何处理模型轮次、工具结果、上下文加载/压缩、会话 ID、持久化和恢复。本文是源码/官方文档阅读，不是上游运行测试，也不是对任何上游项目的生产认证。

## 证据范围

本轮补读了当前上游仓库 HEAD 的关键源码定位，并结合仓库中已经固定的专项审计。当前 HEAD 仅用于确认代码形态，后续上游仍可能变化；三项目的完整调用链和历史限制见仓库内 [Holmes 审计](holmes-source-audit-2026-09-07.md)、[OpenSRE 审计](opensre-source-audit-2026-09-07.md)、[Stratus 审计](stratus-source-audit-2026-09-07.md)。

| 项目 | 本轮固定来源 | 重点入口 |
| --- | --- | --- |
| HolmesGPT | [`773fddf3293f1804e17feeee862e5cc98a51025d`](https://github.com/HolmesGPT/holmesgpt/tree/773fddf3293f1804e17feeee862e5cc98a51025d) | [`tool_calling_llm.py`](https://github.com/HolmesGPT/holmesgpt/blob/773fddf3293f1804e17feeee862e5cc98a51025d/holmes/core/tool_calling_llm.py)、上下文 compaction/limiter |
| OpenSRE | [`430387420b91d3708339fe6130ba45a1897b31e8`](https://github.com/Tracer-Cloud/opensre/tree/430387420b91d3708339fe6130ba45a1897b31e8) | [`react_loop.py`](https://github.com/Tracer-Cloud/opensre/blob/430387420b91d3708339fe6130ba45a1897b31e8/core/agent/react_loop.py)、session lifecycle |
| Stratus/SREGym | [`c0d57d13d25231a9a6f68390afe460cbfda4d77e`](https://github.com/SREGym/SREGym/tree/c0d57d13d25231a9a6f68390afe460cbfda4d77e) | [`base_agent.py`](https://github.com/SREGym/SREGym/blob/c0d57d13d25231a9a6f68390afe460cbfda4d77e/clients/stratus/stratus_agent/base_agent.py)、driver |
| LangGraph Python | [`49cce0ca852be4cfb567a1cbe0e511ff325a1682`](https://github.com/langchain-ai/langgraph/tree/49cce0ca852be4cfb567a1cbe0e511ff325a1682)；同步阅读 [官方 persistence/interrupt 文档](https://docs.langchain.com/oss/python/langgraph/persistence) | checkpointer、`thread_id`、pending writes、interrupt/resume |
| OpenAI Agents SDK | [`ad93f5420edfecb30c6d2bc3a1a22047918cba1a`](https://github.com/openai/openai-agents-python/tree/ad93f5420edfecb30c6d2bc3a1a22047918cba1a)；同步阅读 [Runner lifecycle](https://github.com/openai/openai-agents-python/blob/ad93f5420edfecb30c6d2bc3a1a22047918cba1a/docs/running_agents.md) 和 [sessions](https://github.com/openai/openai-agents-python/blob/ad93f5420edfecb30c6d2bc3a1a22047918cba1a/docs/sessions/index.md) | Runner loop、`max_turns`、`RunState`、Session、compaction |

## 先固定 PR #29 的实际形态

在 `feature/m1-01-investigation-loop`（当前读取 SHA `9f3506fcc4025deb359ef38c09a97fe0ec29c923`）中，`InvestigationLoop.run()` 在内存 `messages` 列表上执行：生成 system/user/evidence 消息，循环调用模型，提交 assistant 响应，执行工具并把 tool message append 回列表；最后一个模型请求保留给报告。`opspilot/investigation/limits.py` 将 `MAX_MODEL_REQUESTS_PER_RUN` 固定为 4，另外限制工具数量、工具总时间、请求大小、请求超时和 Run 墙钟时间。

持久化接口会提交步骤、工具结果和预算账本，#30 可以从业务记录恢复 pending tool；但本轮看到的 #29 loop 没有在每一轮通过 `run_id/step_id/evidence_id` 从 PostgreSQL 重建完整 `messages`，也没有接入自动摘要、跨 Run 续接或 token 级上下文压缩。这里的结论是对该分支静态源码的描述，不是对合并后 main 的运行证明。

因此它目前是“单个有界 Run 的模型—工具循环”，不是完整的长程调查调度器。4 是当前 M1-01 的资源冻结值，不是上游通用经验，也不能解释为一次任意事故调查的足够容量。

## 上游实现对照

### HolmesGPT：共享工具循环，加了真正的上下文缩减，但持久恢复分入口

当前 `ToolCallingLLM` 以 `while` 驱动模型和工具调用；工具调用结果加入后续消息，工具结果过大时先 spill/截断，历史过大时调用 compaction。源码入口：[tool_calling_llm.py](https://github.com/HolmesGPT/holmesgpt/blob/773fddf3293f1804e17feeee862e5cc98a51025d/holmes/core/tool_calling_llm.py#L633-L742)、[context window limiter](https://github.com/HolmesGPT/holmesgpt/blob/773fddf3293f1804e17feeee862e5cc98a51025d/holmes/core/tools_utils/tool_context_window_limiter.py#L33-L140)、[compaction](https://github.com/HolmesGPT/holmesgpt/blob/773fddf3293f1804e17feeee862e5cc98a51025d/holmes/core/truncation/compaction.py#L196-L340)。

Holmes 的普通 HTTP/CLI 路径常由调用方带回 history；可选 conversation worker 才从 Supabase 事件恢复消息。历史审计已确认：这个恢复路径不等于恢复任意正在执行的网络调用栈，普通 one-shot 入口也不自动获得持久会话。结论是它给了“长循环中的上下文管理”真实实现，但不能直接作为 OpsPilot 的 Incident/Run 恢复合同。

本轮没有找到一个全局固定为 4 的模型轮次上限；实际停止由工具调用终止、取消、上下文/运行约束和入口配置共同决定。

### OpenSRE：会话、任务计划和 ReAct loop 是分层的

`react_loop.py` 从 `run_input.max_iterations` 驱动迭代，并结合停滞、工具终止、上下文预算和取消决定停止：[当前源码](https://github.com/Tracer-Cloud/opensre/blob/430387420b91d3708339fe6130ba45a1897b31e8/core/agent/react_loop.py#L180-L278)。它不是把模型请求数硬编码成一个产品常数。

上下文由 envelope 组装，包含会话消息、工具事实、知识、目标和任务计划。显式 resume 会通过 session ID 读取 JSONL，会恢复 user/assistant messages、accumulated context、goal、task plan 和 history：[session lifecycle](https://github.com/Tracer-Cloud/opensre/blob/430387420b91d3708339fe6130ba45a1897b31e8/core/agent_harness/session/lifecycle.py#L167-L315)。这是真正的“按会话标识加载上下文并继续”，但它也明确不恢复活跃网络调用栈；默认 one-shot headless 配置并不打开持久 session store。

OpenSRE 的启示是：`session_id` 读取历史、`run/attempt` 调度 claim、ReAct loop 迭代上限应是不同职责，不能把一个 `run_id` 字段等同于完整恢复能力。

### Stratus/SREGym：图式 loop 和阶段 retry，但主要是 benchmark 轨迹

Stratus 使用 LangGraph `StateGraph`/`ToolNode`，状态中累积 `messages` 和 `num_steps`；工具节点结束后递增步骤，达到配置的 `max_step` 则停止：[BaseAgent](https://github.com/SREGym/SREGym/blob/c0d57d13d25231a9a6f68390afe460cbfda4d77e/clients/stratus/stratus_agent/base_agent.py#L44-L175)。driver 的 mitigation retry 会生成阶段摘要、重新构造 Agent 并再次运行，而不是从中断指令栈精确续接：[driver](https://github.com/SREGym/SREGym/blob/c0d57d13d25231a9a6f68390afe460cbfda4d77e/clients/stratus/stratus_agent/driver/driver.py#L429-L620)。

当前路径使用 `MemorySaver` 和固定 `thread_id`，完整流程结束后写 combined trajectory JSONL；没有把这份轨迹重新读回并驱动图执行的崩溃恢复路径。它证明了“阶段摘要/重试”和“图中消息累积”的实现方式，但不能作为 durable Incident worker 的恢复样板。

### LangGraph：把检查点、线程 ID 和暂停/恢复做成运行时能力

官方文档把 checkpointer 定义为线程级 graph state 快照，把 store 定义为跨线程的应用数据；调用必须给 `configurable.thread_id`，同一 ID 才会读取同一线程的 checkpoint：[persistence](https://docs.langchain.com/oss/python/langgraph/persistence#quickstart)。当前源码还定义了 `put_writes`/pending writes：同一 super-step 中已经成功的节点在失败后恢复时可以避免重复执行，[checkpoint README](https://github.com/langchain-ai/langgraph/blob/49cce0ca852be4cfb567a1cbe0e511ff325a1682/libs/checkpoint/README.md#L19-L69)。

`interrupt()` 会保存状态并等待外部输入；使用相同 `thread_id` 和 `Command(resume=...)` 恢复。官方明确说明恢复会从包含 interrupt 的 node 开头重跑，interrupt 前的副作用必须幂等：[interrupt 文档](https://docs.langchain.com/oss/python/langgraph/interrupts#resuming-interrupts)。这与“精确恢复 Python 调用栈”不同，但比 PR #29 当前内存列表提供了明确的状态读取、暂停和 replay 语义。

LangGraph 没有规定事故调查必须 4 轮；停止条件由应用传入的图逻辑/预算决定。生产使用还要选择持久 checkpointer、checkpoint retention 和外部 redispatch；框架本身不替 OpsPilot 实现 Incident、lease、证据采纳或外部工具 exactly-once。

### OpenAI Agents SDK：显式 `max_turns`，Session/RunState/compaction 分开

`Runner` 的官方 loop 是：调用模型；若 final output 则结束；若 handoff 则切换 agent；若工具调用则执行并把结果带入下一轮。`max_turns` 是可传参数，默认值当前源码为 10，也可以传 `None` 关闭；超过上限抛出 `MaxTurnsExceeded`：[running agents](https://github.com/openai/openai-agents-python/blob/ad93f5420edfecb30c6d2bc3a1a22047918cba1a/docs/running_agents.md#the-agent-loop)、[`run.py`](https://github.com/openai/openai-agents-python/blob/ad93f5420edfecb30c6d2bc3a1a22047918cba1a/src/agents/run.py#L260-L320)、[`run_config.py`](https://github.com/openai/openai-agents-python/blob/ad93f5420edfecb30c6d2bc3a1a22047918cba1a/src/agents/run_config.py#L1-L20)。

Session 负责按 `session_id` 保存/读取消息；官方提供 SQLite、SQLAlchemy/PostgreSQL 和 Redis 适配器。`RunState` 可序列化暂停状态，恢复时继续未完成 run，并保留已使用的 turn 预算；这是“会话历史”和“当前执行状态”两个不同层次：[sessions](https://github.com/openai/openai-agents-python/blob/ad93f5420edfecb30c6d2bc3a1a22047918cba1a/docs/sessions/index.md)、[running agents](https://github.com/openai/openai-agents-python/blob/ad93f5420edfecb30c6d2bc3a1a22047918cba1a/docs/running_agents.md#errors-and-recovery)。SDK 还提供 `OpenAIResponsesCompactionSession`，把 compaction 作为 session 能力，而不是把历史无限 append。

这是一条比“4 次请求后生成报告”更清晰的参考路径：单次 run 仍有可配置上限，但上下文 history、暂停状态、预算和压缩有独立接口。

官方文档还把 DBOS 列为可选的可靠性集成，用数据库保存进度以支持长运行和重启；这属于额外运行时集成，不是 `Runner` 核心自动提供的 Incident 恢复能力。

## 关键比较

| 能力 | PR #29 当前分支 | HolmesGPT | OpenSRE | Stratus | LangGraph | OpenAI Agents SDK |
| --- | --- | --- | --- | --- | --- | --- |
| 模型/工具循环 | 手写 `messages` append | 共享 loop | ReAct loop | graph/tool node | graph node/edge | Runner loop |
| 固定小轮次 | 每 Run 固定 4 次模型请求 | 入口/运行约束组合 | `max_iterations` 可配并有停滞/取消 | `max_step` 配置 | 应用自行定义 | `max_turns` 可配，默认 10 |
| 上下文加载 | 初始输入 + 本轮内存列表 | history + tool spill/compaction | session/envelope 可恢复 | 内存 state；轨迹事后写盘 | checkpoint 按 `thread_id` 读取 | Session/RunState/Responses state |
| 压缩/摘要 | 当前 loop 未接通自动压缩 | 有工具结果限制和 history compaction | 有 context budget/envelope | 有 trim/阶段摘要，但路径有限 | checkpoint retention；压缩由应用负责 | 有 compaction session |
| 崩溃/暂停恢复 | #30 恢复业务步骤；#29 未重建完整 messages | 部分 worker 路径 | 显式 session resume；不恢复活调用栈 | 未证明 durable resume | checkpointer + interrupt/replay | RunState/session；需选持久后端 |
| 证据/事故业务语义 | OpsPilot 自己的合同 | 上游自己的 tool/result 语义 | 上游自己的 task/session 语义 | benchmark oracle/trajectory | 运行时状态，不是 Incident 账本 | SDK session，不是 Incident 账本 |

## 对 PR #29 的结论

1. **“模型→工具→结果→模型”只是共同的 loop 形状，不足以称为长程 Agent 实现。** 上游真正有差异的部分在于 history 的来源、持久化粒度、压缩触发、暂停/恢复语义和停止条件。
2. **4 次不是上游共识。** OpenSRE、LangGraph 和 OpenAI Agents SDK 都把轮次/迭代作为可配置运行约束；OpenAI 当前默认 10，LangGraph 不内置事故轮次常数，Holmes 则由入口和上下文/运行限制控制。PR #29 的 4 应继续作为验证阶段的单 Run ceiling 记录，不能作为产品长程能力的容量结论。
3. **#29 当前缺少的不是“再加一个 ID 字段”，而是一个可验证的恢复上下文路径。** 至少需要明确：从 `run_id`/step 记录如何重建有序 model/tool messages；哪些工具结果可复用；模型调用中断发生在提交前后如何处理；恢复后如何继续预算；上下文过大时如何压缩且保留证据引用。
4. **LangGraph 的价值是现成的 checkpoint/thread/replay 机制，不是自动让调查更聪明。** 如果采用，仍需把 graph state 与 OpsPilot 的 Run、lease、evidence 和工具权限合同对齐；如果不采用，手写 loop 也必须显式补齐同等的状态和恢复合同，而不能只保留 `while` + 4 次上限。
5. **建议把 #29 的审查门槛从“能否完成一次 mock loop”提高到“能否证明长任务的可恢复边界”。** 在产品实现继续前，至少做一个不使用真实凭据的确定性实验：模型工具循环超过 4 轮、工具结果导致上下文膨胀、进程在工具完成/步骤提交前后退出、从数据库恢复并继续；分别验证重复查询、证据 ID、预算和迟到结果的语义。

以上是对标证据和审查建议，不自动修改 SPEC、ADR、ROADMAP 或把 LangGraph 选型视为已决定。
