# Eval 平台与运行底座联动选型

日期：2026-09-07。用户提到 languse，本轮按 Langfuse 理解。官方能力/部署/计费资料核查；未登录账户、创建项目、上传数据或运行平台 SDK。平台候选与运行底座均未最终采纳。

## 结论建议

在运行底座仍优先验证 Python + LangGraph、没有新增私有化硬要求的条件下，优先验证 **LangSmith Cloud 作为单一实验与追踪界面**，配合我们自己拥有的场景执行/结果判定代码。若开源自托管或控制部署成为首要约束，优先转为 **Langfuse**。这不是功能或效果排行榜；两者均有框架集成和完整 eval 功能，不因 LangGraph 名称相近就排除其它平台。

LangWatch/Braintrust 的已查能力与边界见 `eval-platform-alternatives-2026-09-07.md`。它们也能作为主平台；当前没有需要并行部署多个平台的已确认需求。

LangWatch 值得在多轮用户交互仿真、工具/任务行为测试权重较高时优先比较；Braintrust 值得在code-first实验/scorers和跨版本对照权重较高时比较。二者均有本地评分与LangGraph/OTel接入依据，未进行跨平台体验/性能实测；这些是适配倾向，不是能力排他结论。

## LangSmith：已查事实与适配判断

[评测文档](https://docs.langchain.com/langsmith/evaluation-concepts) 提供数据集/版本与splits、实验比较、代码/LLM/人工评分、annotation queues，以及离线和线上反馈循环。评测对象可包括 run/thread；不等于自动知道本项目 Incident 的全部语义。[本地评测](https://docs.langchain.com/langsmith/local) 提供 Python 本地执行路径。[数据集管理](https://docs.langchain.com/langsmith/manage-datasets-in-application) 支持从 trace/人工审阅转数据及导入文件。

建议理由：符合我们需要的 trace→人工分析→回归集→版本对比流程，并能与候选 LangGraph 同时核验。托管运行减少维护观测平台本身的工作；不是宣称其他托管平台没有同样能力。我们仍需验证自定义provider/tool spans、恢复后多Run关联以及真实场景的异步评测适配。

[自托管说明](https://docs.langchain.com/langsmith/self-hosted) 明确为 Enterprise 附加项；不能视为免费自托管组件。[当前价格](https://www.langchain.com/pricing) Developer $0/seat、含5k base traces/月；Plus $39/seat，之后另计用量。公开页使用 LCU/LSU。具体套餐的导出、大小限制与retention应在真实账户验证，不沿用旧博客单价或保证所有功能免费。

## Langfuse：同样具备 eval，不只是 tracing

[评测概览](https://langfuse.com/docs/evaluation/overview) 覆盖 live scoring、datasets、experiments、code/LLM evaluator、人工审阅与反馈。[SDK experiments](https://langfuse.com/docs/evaluation/experiments/experiments-via-sdk) 支持我们的 task函数、自定义item/experiment evaluator、异步执行和本地数据；函数型 evaluator 可在实验进程执行。[LangChain/LangGraph 集成](https://langfuse.com/integrations/frameworks/langchain) 已有支持，不能认为选LangGraph就必须选LangSmith。

[自托管](https://langfuse.com/self-hosting) 当前页面为v4，包含web/worker、PostgreSQL、ClickHouse、Redis/Valkey等存储组件，部分附加能力需license。开源自托管带来数据/部署控制，也引入升级、备份、容量和故障维护。也可直接用Cloud，不能把这部分运维成本当Cloud方案的缺点。

[价格页](https://langfuse.com/pricing) 当前Hobby含50k units/月和30天数据访问，Core起价$29/月，按用量另计。units不等于LangSmith的traces，不能拿免费数字直接比较成本。具体选用版本/配额需确认。

建议：若保留可自托管替代路径很重要，Langfuse是强候选；它也能搭配Pi或Agents SDK。迁移性仍依赖实际trace字段和实验映射，OTel不保证四个平台语义完全一致。

## 我们的 eval 运行合同（设计建议，依据既有 F1/F6/F8/F9）

尽量复用平台实验runner和既有场景harness。我们写的是该产品的task适配、结果合同与判定函数，不重做数据集平台、通用实验调度和评分界面。所谓离线eval可以是在隔离真实环境中的批量实验，不代表必须用固定文本或断网运行。

1. 版本化 IncidentScenario 定义环境/公开事件/负载与私有预期；固定上游和候选版本、模型、工具及评测器。
2. 我们的本地/CI/专用runner启动或重置真实软件环境、建立流量、注入问题并调用产品入口。评测平台不需要持有被测集群的操作者凭据。
3. Agent只接触运行可见上下文。注入脚本、参考答案、判定器私有数据与模型可调用工具隔离；不是把scenario标签用于选择prompt。
4. 收集多个Run、证据引用、状态变化、人工处理记录和独立服务观察，形成 IncidentOutcome。等待人工的时间与Agent实际计算时延分别统计。
5. 代码判定目标/证据身份、状态、权限和恢复事实；人工/校准后的LLM judge辅助评估解释与证据支持。LLM自身打高分不能覆盖确定性失败。
6. 将评分、版本、必要脱敏trace与artifact引用映射到平台experiment；人工查看失败并对照基线。原始关键工件在我们的存储中保留，不依赖付费批量导出才能复现实验。
7. 线上抽样反馈经审核进入开发回归集，封存集不反复用于调参；改动后跑相同评测，再部署并观察。

数据标识：incident_id、run_id、scenario_id、experiment_id、attempt和各版本。Incident可跨多个trace；thread或session只是平台的关联表示，不能自动等同于事故状态。把整个案例聚合评分与单次工具/span评分区分。

## 平台不会替我们解决的工作

真实环境健康/重置、流量/故障、基线公平性、测试集泄漏、独立恢复裁判、事故状态所有权、队列恢复与访问控制仍由项目负责。平台的对话模拟适合用户交互测试，不自动证明真实Kubernetes故障调查效果。

Agent自身版本的CI回归门槛属于软件交付验证，不是Agent获得被调查业务的发布门禁权；后者仍被SPEC排除。

## 不绑定运行底座与平台

LangGraph/Pi/Agents SDK负责执行；所选平台负责观测与实验界面；场景/评分和权威结果由项目代码持有。通过小型适配器上传实验与scores，不建设支持四个平台的通用平台层。只接一个主平台，保存基础数据格式和明确ID，必要时再实现替换。

业务指标/日志监控继续使用既有观测数据源，eval平台不能替代业务健康判断或Agent队列监控。平台上传失败应能被发现并保留待补传信息，但不让报表服务成为业务调查的未声明硬依赖；这需要实测，不能照抄厂商“零影响”宣传。

## 联动验证与预算

在同一小型隔离案例检验：完整模型/工具trace；进程重启后run关联；本地代码评分与平台显示一致；人工反馈进入新数据版本；导出或用本地工件重建一次对比；平台不可达时行为；敏感字段处理；实际套餐限制。先不同时建设两套平台。

成本按单Incident的model/tool spans、多个Run、实验重复、judge调用和retention计算；再加平台seat/用量或自托管资源。模型与judge费用不能漏算，免费trace与unit配额不可直接相除。当前无真实usage，不能给可信固定月总费用。

优先验证组合仍是 Python + LangGraph核心 + 持久checkpoint候选 + 本地场景/裁判 + LangSmith Cloud。若需开源私有化改用Langfuse；若运行底座改为SDK/Pi，不必同时迁移评测标准。最终平台选择仍是待验证的建议，不自动采纳额外托管部署服务。
