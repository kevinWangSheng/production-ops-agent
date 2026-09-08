# 求职导向 Ops Agent：可复用实验场与 eval 工作流

> 状态：历史讨论或研究证据，非当前规格。2026-09-06 已确认的产品边界以仓库根目录 SPEC.md 和 docs/adr/0001-readonly-investigation-boundary.md 为准；本文旧的候选、建议及待决表述不覆盖该共识。

研究日期：2026-09-06。证据为当日读取的官方网页与上游 README；未部署、未跑 benchmark、未测量本项目资源。本文是讨论输入，不是规格变更或实现批准。现有资料位于 `docs/research-basis.md`；本轮详细笔记放入 `docs/research/`。

## 可复用资产：已查事实

**SREGym** 当前 README 列出 90 个问题，Lite 子集有 21 个代表性问题，官方建议 Lite 可以在 kind、8 vCPU、16 GB 内存运行。完整环境需要 Linux 节点的 SSH/root 权限，托管 Kubernetes 不能开箱使用；kind 不支持全部问题。包含故障、运行环境、Stratus 基线 agent 和评估入口。[官方仓库](https://github.com/SREGym/SREGym)

SREGym 默认把 agent 放在隔离容器，避免读取问题定义和评分逻辑，并限制访问 benchmark GitHub 源码。`svelte` profile 减少观测组件和 retention，但官方明确它改变 agent 可见环境，分数不可与 `full` 混比。故复用后必须记录 suite/profile/commit，不能只写“跑过 SREGym”。[官方运行说明](https://github.com/SREGym/SREGym#-usage)

**AIOpsLab** 可复用微服务部署、负载生成、故障注入、遥测和 orchestrator。支持本地 kind 和远端 Kubernetes；agent 通过异步 `get_action(state)` 接入。Problem 显式包含 application、task、fault、workload、evaluator，task 分 detection/localization/analysis/mitigation。适合借鉴评估协议，也有现成 client 基线。[官方仓库](https://github.com/microsoft/AIOpsLab)

**OpenTelemetry Demo** 官方 Kubernetes 部署要求应用自身有 6 GB 空闲内存，不能把它误读为整台主机总内存 6 GB。提供 Helm chart；文档说明 chart 跨版本升级不受支持，需重建，因此实验应固定 chart/image 版本。[部署文档](https://opentelemetry.io/docs/demo/kubernetes-deployment/)

Demo 自带 flagd 故障开关，例如支付失败、错误下游地址、高 CPU、内存泄漏、NotReady 和负载洪峰。这能快速提供“真进程、真调用、真遥测、可控故障”的演示基础，但不自动提供本项目的恢复裁判。[故障开关文档](https://opentelemetry.io/docs/demo/feature-flags/)

## 选择建议与适用边界

- 求职方向偏 Agent 应用工程：优先 OTel Demo + 一个发布故障族 + 自己的 investigation/typed proposal/独立 verifier，最快形成连贯演示。
- 求职方向偏 SRE agent/eval：优先 SREGym-Lite，先跑现有 Stratus 基线，再接自己的 agent；自定义发布回归作为补充套件。
- 不建议首期同时接三套环境。AIOpsLab 作为协议参考足够；是否作为主 harness 取决于实际接入成本。
- 可以大幅复用，但保留上游许可证与出处，并清楚解释自己增加的能力与结果。简历的可验证差异应是故障闭环、评估设计、失败分析、成本/延迟改善，而不是代码从零写的比例。

以上为针对求职作品的建议，未研究具体招聘岗位，不能据此证明市场需求或录用效果。

## LangSmith 已查能力与成本

LangSmith 用 run 表示一次模型/工具等工作、trace 聚合一次操作、thread 连接多轮 trace；支持手动 `traceable`、context manager 和 RunTree，不要求使用 LangChain。metadata/tag 可标记版本与环境，feedback 绑定 run。[观测概念](https://docs.langchain.com/langsmith/observability-concepts)

官方 eval 文档区分离线数据集评估与线上 live trace 评估；支持 splits、自动数据集版本及版本标签，建议 CI 固定版本。annotation queue 可人工审阅 trace，再转成回归样本。[评估概念](https://docs.langchain.com/langsmith/evaluation-concepts)

在线 judge 支持 filter、采样、历史回填和每周支出上限；文档称被在线 evaluator 处理的 trace 会升级 extended retention，并产生相关费用。[在线 judge](https://docs.langchain.com/langsmith/online-evaluations-llm-as-judge)

当前价格页：Developer 为单席位 $0/月，含 5,000 base traces；Plus $39/席位/月，含 10,000。页面使用 LCU/LSU，分别 $1.50/$1.00。个人首期无须为了 tracing/eval 购买部署方案。超额 trace 与 evaluator/model 消耗另外预算，不能沿用旧博客单价。[价格页](https://www.langchain.com/pricing)

发现官方文档冲突：价格页写 base 14 天、extended 400 天，而观测概念页写 SaaS trace 180 天。本文不把任何一个当成账户 retention 保证；购买或设置保存策略前应核对实际账户 billing/retention。关键实验结果应导出到项目自己的持久 artifact。[价格页](https://www.langchain.com/pricing)、[观测概念](https://docs.langchain.com/langsmith/observability-concepts)

## 推荐的完整闭环（项目设计建议）

1. 固定一套 lab、版本和健康负载；用确定性脚本证明健康→故障→人工恢复，先验证裁判会识别失败。
2. 复用基线 agent，建立无 agent/规则脚本/基础 agent 的对照。每次重建独立环境；相同故障种子、负载、超时和权限。
3. 从第一条运行开始记录 incident ID、experiment ID、scenario 版本、模型、prompt/tool/code 版本、每步输入输出、tokens、费用、延迟和错误；不依赖私有思维链。
4. 数据按故障族/模板来源分成开发与封存测试，不能把同一模板换几个名字随机分桶。故障脚本、真值和 verifier 在模型不可读的进程/容器；agent 只见症状与获准遥测。
5. 外部确定性裁判检查目标身份、动作权限、最终资源状态及真实业务探针。成功定义包含持续恢复窗口；同时计超时、误操作、无故障时乱动、转人工、成本与恢复时间。LLM judge 只辅助报告质量/证据相关性，需抽样人工校准。
6. 每个候选跑固定版本数据集与重复种子；保存每题结果、失败例子和 baseline 差值，不只一个总体成功率。CI 跑小 smoke，夜间或按需跑较大矩阵。
7. 常驻 lab 接近真实流量节奏注入故障，先 shadow/只读，再在人审下执行白名单动作；业务可观测性留在 Prometheus/OTel 等，agent 可观测性放 LangSmith，用 incident ID 关联。
8. 异常/低分/高成本 trace 抽样进入人工队列，确认后加到开发回归集；候选通过封存评估才晋级。不把已反复用来调参的题继续称为独立测试。

## 资源建议与不确定性

起步只需一台能分配约 8 vCPU/16 GB 给 lab 的主机，使用外部模型 API，无需 GPU；16 GB 是 SREGym-Lite 官方参考，不是本项目已经测得的峰值。若同机运行完整遥测、镜像构建与多个场景，建议规划 32 GB 主机并实测，串行跑场景优先于先买机器。

第二阶段可加一台独立 runner/负载与 verifier 主机，避免把被测集群故障误判为 judge 崩溃。只有确实要证明节点故障、调度与跨节点行为，再采用 1 control plane + 2 workers 的独立 VM 拓扑；同一物理机的多个 kind 节点不提供独立物理故障域。此拓扑是建议，不是上游最低要求。

预算按 VM 小时、磁盘/快照、网络、模型 tokens、judge tokens、trace 存储分别计算。尚未选地区/provider、测每题 token 和 lab 时长，不能给可信固定月费。先运行 10 次有代表性的事故测峰值与均值，再外推 `场景数 × 重复数 × 候选数 × 单次成本`。优先保留失败 trace 与实验汇总，控制在线 judge 采样。

证据仍缺：各上游固定 commit、ARM 镜像兼容实测、实际启动/重建耗时、所选故障与当前 SPEC 的一一映射、每次运行资源曲线、具体目标岗位。开始实现前应补齐与所选路线直接相关的部分。
