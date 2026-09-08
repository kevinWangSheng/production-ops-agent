# OpsPilot 运行底座选型建议

日期：2026-09-07。状态：推荐方案，待选型评审及隔离验证；没有安装框架、运行模型、部署数据库或清除实现门禁。不是全栈实施方案。

## 推荐

优先推荐并验证 **Python + LangGraph 核心库** 组织共享调查循环与执行检查点；持久 checkpoint 优先评估 PostgreSQL saver。产品自己拥有 Incident/Run、证据、授权、调度和人机状态。LangSmith 可独立作为观测/eval 候选，不需要购买或自托管 LangSmith Deployment 才能使用 LangGraph。

这项推荐主要依据既定 F2/F8 的中断恢复和持续运行需求，以及 Python 上游逻辑的复用成本；不声称 LangGraph 能提高模型判断准确率。具体包版本、模型、API框架、前端、云商和机器数尚未锁定。

## 比较范围与证据

- [LangGraph 调查](runtime-choice-langgraph-2026-09-07.md)：固定 Python 源码 `81bf17b23123e4ef8b9d5f49fa09a0122fc2edd1`，源码 manifest 为 1.2.11；Postgres saver manifest 为 3.1.2。已读实现和相关测试，未确认其为本项目安装组合。
- [Pi 调查](runtime-choice-pi-2026-09-07.md)：主本地源码 `086c32e74530564922d011ade23ff582c9d63116`，agent-core 0.84.2；另一个 checkout 不混用。已查真实可用 Agent 与未完成新 harness 的区别。
- [OpenAI Agents SDK / 手写调查](runtime-choice-agents-sdk-2026-09-07.md) 与 [SDK 源码补查](runtime-choice-agents-sdk-source-supplement-2026-09-07.md)：当日官方文档加固定 `1d471a4775bf2f40179f411824da383deb4c3fca`（manifest 0.22.0）的 Runner/session/RunState 及测试；未全面审计外部 durable 集成。

三个候选均已核查关键源码路径，但覆盖范围仍不同；没有运行比较，没有构造加权分数、性能排名或开发工时估计。

## 为什么优先 LangGraph

我们需要的调查行为是动态选择工具、获取新证据、继续循环；并不需要症状分类或预设答案。LangGraph 的图可表达模型/工具往返，边根据工具调用和执行状态决定，符合 ADR-0002。

已有 checkpointer、interrupt/replay、重试和状态流式能力，能减少从零实现执行状态机制的工作。其优势建立在我们确实使用恰当的执行边界：模型输出、工具观察及必要的人工暂停，而不是把完整第三方循环放进一个大节点。具体节点划分由行为合同决定，本次不冻结。

Python 与 HolmesGPT/OpenSRE 已核逻辑同语言，可能减少类型/工具调用适配，但不是所有代码都可直接粘贴：它们的模型层、消息格式、配置和状态依赖仍需映射。这是有条件的工程成本判断，尚未测量。

## 必须付出的代价和仍需自建

LangGraph 的 state/reducer/thread/checkpoint/replay 语义、依赖版本、数据库表和升级兼容需要维护。checkpoint 不是完整 Incident 数据库，也不是业务审计的唯一真相。

默认 async durability 可能留下恢复窗口；建议验证 sync 模式，以数据库确认换取更清楚的执行边界。即使 sync，外部查询完成与结果持久化仍不原子。重试要保留查询尝试、新鲜度和预算事实。

仍需实现：事件接入/关联、Run派发和重派、跨worker所有权、背压、目标只读身份、超时/进程清理、证据保留、人工状态优先级、独立恢复观察、审核知识、运行告警和交付。Graph 不会替我们重启进程，也不会强制结束任何不合作的工具。

证据与 checkpoint 的一致性需要明确写入协议；即使都用同一个 PostgreSQL 实例，也不能默认是同一事务。大证据宜通过记录引用进入模型上下文，存储位置/retention后续设计。

## 其它路线何时更合适

### Pi Agent + pi-ai

适合 TypeScript/Node 成为整体服务技术栈、希望直接控制上下文/工具/provider、并愿意自己拥有持久执行协议时。经典 Agent 是可用路线，不因新的 harness 尚未完成而被否定。

本次不选择 coding-agent RPC 为服务底座：它增加编码工具默认值、资源发现与子进程协议适配；这些没有对应的已确认产品收益。Pi 新 AgentHarness 在核查版本的关键方法抛 HarnessNotImplemented，不能用其设计文档给路线加分。若选择 Pi，应以真实 Agent API 的能力和应用需要补齐的工作评估。

### OpenAI Agents SDK

适合优先复用现成工具循环、MCP、会话和tracing，产品在外层控制Incident/Run与任务调度。它支持中间turn的session写入、序列化RunState与受控恢复，不能说没有恢复能力；支持provider/adapter，也不能说只能调用OpenAI。官方还有外部durable运行集成，需另核具体合同。

当前优先验证LangGraph并非因为品牌/模型绑定，而是显式步骤级执行状态更直接映射本项目可靠性目标；这尚未证明其整体工作量低于SDK。若SDK精确版本的状态边界经验证已足够，且移植成本显著更低，SDK可以取代首选。API服务/存储/恢复策略的归属仍由应用承担。

### 自行实现循环

可完全控制循环与状态；继续复用模型供应商SDK和现成基础设施，不必重写网络层。但需要维护参数/结果、stream、provider差异、取消、重试、状态序列化与升级协议。代码生成快不意味着维护合同少。

除非候选框架真实阻碍必要行为，或适配比小型自有实现更复杂，不建议一开始自行承担全部基础机制。Incident与证据业务逻辑无论选择哪条路线都需要自己拥有。

## 反证检查后的推荐强度

独立复核指出：SDK的实际状态能力强于只看概览时的印象；Pi新harness未完成不否定经典Agent；LangGraph逐步checkpoint依赖我们的执行边界设计，不是包住整个上游loop自动获得。因此结论是“优先推荐并验证LangGraph，SDK保留为可信最终候选”，不是运行测试已证明胜出。若恢复实验显示SDK整体更简单，应改变推荐。

## 本次明确不叠加的东西

不同时引入LangGraph和Pi/Agents SDK作为两个拥有同一循环的框架；不加Temporal作为第二套默认任务状态机；不因选LangGraph就默认采用高层LangChain agent、多Agent或LangSmith Deployment。若后续有独立需求，再通过设计依据评审增加。

## 采纳前的最小验证

这些是技术选型试验，不是重写产品功能；应在隔离环境实施，不访问真实业务写接口。

1. 共用可控模型输出和只读假工具贯通动态循环、上下文更新、trace、不同停止结果。证明无需症状路由。
2. 工具已返回但checkpoint尚未完成时终止worker，再启动新进程；记录准确的重做/丢失窗口、证据引用与预算。
3. 同一Run暂停/取消后让旧工作返回，再重复投递事件；验证状态优先级和所有权，不依赖图本身推断。
4. checkpoint数据库失败、阻塞工具、超时、升级旧state；验证错误/恢复说明和资源清理。
5. 两个实际候选model/provider的小型工具/schema/stream smoke，比较正确性、latency和cost；选择模型后另记录版本与支出上限。

若LangGraph需要破坏大量可复用调查逻辑、恢复语义不符合要求，或checkpoint对已有产品状态层收益很小，则重新比较Pi/Agents SDK。不能为了保住推荐而降低验收。

## 下一阶段

Eval/observability联动已补查，见 [平台选择](eval-platform-selection-2026-09-07.md)：在接受托管平台的条件下优先验证 LangSmith Cloud；开源自托管优先时考虑 Langfuse，LangWatch/Braintrust也有本地实验接入。运行底座与平台不绑定，场景和独立判定保留在项目侧。该建议未被记录为已接受技术决策。

评审本推荐后，将选定底座映射到既有上下文、工具、Incident/Run与证据合同，完善技术方案；随后确定API/存储细节、模拟环境资源和实施任务。本次尚未把推荐写成accepted ADR，SPEC/ADR-0002与所有验收保持不变。
