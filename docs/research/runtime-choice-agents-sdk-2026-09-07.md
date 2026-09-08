# OpenAI Agents SDK 与自行实现：本项目选型依据

2026-09-07。来源：当日通过 OpenAI Docs 搜索并完整读取的官方文档；未安装 SDK、未执行模型请求、未固定或审计全部 SDK 源码。以下明确区分文档承诺和待运行验证，不从旧版本印象推断当前 SDK 缺少状态能力。

补充：本轮随后完成了固定 SDK 源码核查，见 [源码补充](runtime-choice-agents-sdk-source-supplement-2026-09-07.md)。确认已有中间turn会话保存、RunState序列化和受控恢复；本节文档证据的边界应与补充一起阅读。

## Agents SDK 已查能力

[SDK 总览](https://developers.openai.com/api/docs/guides/agents) 将 SDK 定义为 Agent run 与工具循环：应用仍拥有部署、工具实现、状态存储和审批决定。TypeScript 和 Python 均有 SDK。可直接定义工具，支持 MCP、会话、追踪与受控中断。

[Running agents](https://developers.openai.com/api/docs/guides/agents/running-agents) 区分手工输入历史、SDK session、服务端 Conversations 和 previous_response_id 四种续接策略，建议每个会话选择一种，避免重复历史。普通循环按模型输出继续工具调用或结束，不要求按事故症状分类。

[Results and state](https://developers.openai.com/api/docs/guides/agents/results) 提供 pending interruptions 和 TypeScript state / Python to_state()。暂停运行可以序列化保存后继续；这不同于把历史交给下一次新 run。不能称 SDK 只有聊天记忆，也不能把可序列化对象推断成应用在任意 SIGKILL 前已自动保存了所有工具结果。

[Guardrails and review](https://developers.openai.com/api/docs/guides/agents/guardrails-approvals) 支持输入/输出/工具检查、审批后恢复；文档也限定输入/输出 guardrail 的执行位置。输入或输出 guardrail 不能代替每个工具的授权检查。审批工具特性不是本项目开生产写权限的理由。

[Models/providers](https://developers.openai.com/api/docs/guides/agents/models) 允许非 OpenAI 模型走 provider/adapter；不能说 SDK 只能用 OpenAI。高级工具/transport 的支持可能依赖 Responses 路径，具体目标 provider 的 schema、stream、usage 与错误语义仍需验证。

[Integrations/observability](https://developers.openai.com/api/docs/guides/agents/integrations-observability) 有 SDK 管理的 stdio/HTTP MCP 与默认 tracing，可用 SDK/run 级控制。私有遥测工具适合由我们的 runtime 管连接和过滤；默认 tracing 不是本项目凭据/数据出域许可。最终 trace export/filter/storage 方案仍需确定。

## 适配判断（分析）

优势：以单个 Agent 的动态工具循环为中心，少写基础循环；工具、流式、会话和追踪有一致入口。对于单 Agent、应用自己管理 Incident/Run 的产品，属于认真可选的底座，不因品牌而排除。

需要自建或确认：incident 关联与人机状态、队列与调度、跨 worker 所有权、证据存储、读取新鲜度、恢复观测、预算/失败策略、审阅知识。持久化 RunState 的保存时机、崩溃窗口、恢复后已完成工具是否重复、状态跨 SDK 版本兼容，仍须具体 SDK 版本测试。

适用条件：更看重现成 tool-loop/MCP/tracing、主要模型与 SDK 路径匹配，并愿意在应用层拥有长任务调度与存储。反推荐条件：希望框架直接提供自定义步骤级 checkpoint/replay 作为主要状态结构，而不愿适配 run-state 边界。

没有足够证据在此文承诺它优于或劣于所有 LangGraph/Pi 配置；最终比较见选型汇总。

## 自行实现循环

[官方 function-calling 流程](https://developers.openai.com/api/docs/guides/agents#agents-sdk-vs-responses-api) 明确：应用可以直接接收工具调用、执行、返回结果并再次请求模型。这是可行路线，而不是必须使用 Agent 框架。

对本项目的分析：自行实现允许完全控制上下文、工具与状态协议，但要自己拥有 stream 组装、参数验证、工具结果回传、provider 差异、调用预算、取消、重试、状态序列化、版本迁移与恢复测试。Agent coding 降低写代码成本，不会消除这些运行合同的维护成本。仍可使用模型供应商 SDK，不需要重写 HTTP 客户端。

初期不推荐从零实现全部基础运行机制。若试验显示候选框架持续阻碍必要的行为控制，且替换成本高于一个小的自有实现，再用证据重新评估。领域层如 Incident、EvidencePacket、RecoveryObservation 本来就需要我们实现，与是否手写模型循环是两件事。

## 共同的决定性验证候选

同一组确定性工具与受控模型输出，分别测试：保存边界后杀进程并重启；同一 incident 的重复投递与旧 completion；模型/工具超时；取消后结果处置；证据引用跨运行保留；实际第二模型 provider 的工具schema/stream/error契约。先做无外部业务写入的隔离验证，再冻结具体包版本和实现选择。

本轮只是选型资料，不部署、不调用模型、不改变现有验收或实现门禁。
