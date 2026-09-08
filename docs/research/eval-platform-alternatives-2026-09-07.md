# LangWatch / Braintrust：OpsPilot 评测与观测接入依据

核查日期：2026-09-07。状态：官方文档静态调查；未注册账号、安装 SDK、上传数据或运行平台。本文不作最终选型。框架候选 Python LangGraph 不要求绑定任何一个观测平台。

## LangWatch

- **本地实验与自定义评分。** Python `langwatch.experiment.init()`、`loop()`、`log()` 允许现有代码执行任务、计算 numeric/pass-fail 分数并记录实验；`target()` 组织候选对比并关联 trace。可以将本地环境运行器的结果作为实验行，而非必须让托管平台执行被测 Agent。[SDK experiments](https://langwatch.ai/docs/evaluations/experiments/sdk)
- **运行 trace 也能追加外部评分。** `add_evaluation` 或 collector API 接收 `score`、`passed`、`label`、`details`；独立确定性检查器可以计算后上传，不必改成 LLM judge。[Custom scoring](https://langwatch.ai/docs/evaluations/evaluators/custom-scoring)
- **CI。** 官方提供 SDK 实验进入 CI/CD 的流程；阈值和失败策略仍要由项目定义，平台显示实验不等于替项目批准发布。[CI/CD](https://langwatch.ai/docs/evaluations/experiments/ci-cd)
- **Scenario 边界。** 官方将场景描述为模拟用户与 Agent 的多轮交互，代码测试可在自己的测试运行器执行。适合补充追问、纠正、交接等交互测试；并非自带 Kubernetes 故障控制、环境重置和独立业务恢复验证。可以连接真实 Agent，但要由我们另行提供真实软件环境与裁判。[Scenario in code](https://langwatch.ai/docs/agent-testing/scenarios-in-code)、[Scenarios](https://langwatch.ai/docs/agent-testing/scenarios)
- **人工反馈与数据。** 官方索引包含 annotations、review queues、reviewed traces 转 dataset 和 user feedback；相关页面部分本轮抓取失败，不能声称已验证人工队列全流程。数据集 Python/TS/API 程序访问已确认；所读页面未找到不可变历史版本 pin 语义，标记为待核查，不能以 prompt versioning 代替 dataset versioning。[Dataset access](https://langwatch.ai/docs/datasets/programmatic-access)、[官方索引](https://langwatch.ai/docs/llms.txt)
- **框架与 OTel。** 仓库列出 LangGraph、LangChain 等集成，并说明基于 OTel/OTLP；这提供可接入依据，不证明节点、工具、恢复前后 trace 在我们的配置中自动完整。[官方仓库](https://github.com/langwatch/langwatch)
- **自托管与许可证。** 官方仓库为 Apache-2.0 核心、MIT SDK；`platform/app/ee/` 企业模块另需商业许可。提供 Compose、Helm，自托管涉及数据库、队列、分析存储等运行责任；不能把开源等同零维护。[LICENSE](https://github.com/langwatch/langwatch/blob/main/LICENSE.md)、[Self-hosting](https://langwatch.ai/docs/self-hosting/overview)
- **成本维度。** 当前价格页按 core seats、events、额外保留存储计费；event 包含 LLM、工具、检索、evaluation、simulation step，因此一次事故可产生许多收费事件。页面当前 Developer 为 50k events/月、14 天访问、2 users；Growth 为 €29/core-seat/月、200k events 后 €5/100k。模型调用费用另测。旧博客报价不同，采用当前价格页，不用旧文推算。[Pricing](https://langwatch.ai/pricing)

## Braintrust

- **本地执行。** Python `Eval(data, task, scores)` 在自己的进程执行，并把结果记录为 experiment；CLI 支持本地执行以及不上传模式。我们的 K8s 控制器可以在本地 task 外围组织实验，平台接收结果。[Create experiments](https://www.braintrust.dev/docs/evaluate/run-evaluations)、[CLI](https://www.braintrust.dev/docs/reference/cli/eval)
- **自定义 scorers。** 支持内联 Python/TS 自定义代码、CLI 推送和 UI scorer；对需要复杂依赖或访问内部环境的裁判，内联本地代码是已文档化路径。[Scorers](https://www.braintrust.dev/docs/evaluate/write-scorers)
- **远程执行与 sandbox 不同。** Remote evals 由 UI 触发、在用户基础设施执行；sandbox 是托管隔离执行，文档标 beta 且 Pro/Enterprise。两者均不意味着已经有 Kubernetes 故障环境。OpsPilot 无需为本地 CI 实验开启 inbound remote-eval server。[Remote evals](https://www.braintrust.dev/docs/evaluate/remote-evals)
- **数据集可复现。** 修改有版本，可按 version/xact ID pin；named snapshots 额外属于 Pro/Enterprise。应区分基础版本读取与付费命名快照。已有 KB 明确 `init(..., dataset=dataset)` 对 experiment 关联数据集版本的重要性。[Datasets](https://www.braintrust.dev/docs/annotate/datasets)、[Linking caveat](https://www.braintrust.dev/docs/kb/dataset-not-linked-to-experiment-when-using-evalasync-with-parent)
- **人工反馈。** 支持 logs/experiments/datasets review、标签、纠正和用户反馈。当前 human-review 页面泛称配置 review scores 为 Pro/Enterprise，但 pricing/plans 明示 Starter 每项目 1 个 human review score；存在文档粒度冲突，免费可用边界需账号验证，不应承诺无限人工评分维度。[Human review](https://www.braintrust.dev/docs/annotate/human-review)、[Plans](https://www.braintrust.dev/docs/plans-and-limits)
- **CI。** CLI 可在 CI 跑并返回机器可读结果；官方 GitHub action 会自动发 PR comment。我们的本地 CI/检查无需启用自动外部评论。[CI section](https://www.braintrust.dev/docs/evaluate/run-evaluations)
- **LangGraph 和 OTel。** LangGraph 经 LangChain callbacks 捕获运行、节点、模型调用；另可用 OTLP/OTel 接入 project 或 experiment。需要验证工具自定义 span、checkpoint resume 的关联及脱敏。[LangGraph](https://www.braintrust.dev/docs/integrations/agent-frameworks/langgraph)、[OTel](https://www.braintrust.dev/docs/integrations/sdk-integrations/opentelemetry)
- **自托管边界。** 官方方案主要为用户自托管 data plane，UI、认证、平台更新由 Braintrust 管理，提供 AWS/GCP/Azure Terraform。不能把这个描述成完整开源平台可免费离线部署；商业 self-host/on-prem 具体条件依计划与合同。[Self-hosting](https://www.braintrust.dev/docs/admin/self-hosting)
- **成本维度。** 平台费、processed data GB、scores 数量、延长保留存储、平台模型 credits/token。自定义 code scorer 输出也属于收费 scores。当前 Starter 为 $0、1GB processed data、10k scores、14-day retention；超量和更高计划按价目表。多维评分乘以事故重复次数会增加 score 数。[Pricing](https://www.braintrust.dev/pricing)

## 对 OpsPilot 的推导（非平台现成功能）

本地/隔离实验运行器负责：环境初始化、健康检查、故障注入、真实 Agent 运行、独立结果检查与清理。平台负责接收 trace、实验输出、版本元数据和分数，提供对比与反馈工作台。`IncidentScenario -> IncidentOutcome` 由前者建立事实，后者不能仅凭报告文本认证环境恢复。

上传应关联 `incident_id`、run/attempt、scenario 和环境版本、代码/模型/工具/知识版本、evaluator 版本、实验与证据引用。控制 hidden ground truth 只进入独立裁判，不进入被测 Agent；平台不应成为唯一原始证据保存点。

两平台均有自定义本地 evaluator 路径，因此不能因使用 LangGraph 排除它们。最后选型还需一个受控样本验证：完整 trace 树、外部裁判 score 关联、可导出数据、版本重跑、失败时 flush/丢失行为、真实 billable volume。LangWatch 需要额外核实 dataset pin 和人工 review 实际路径；Braintrust 需要核实免费人工评分边界及需要的保留时间成本。
