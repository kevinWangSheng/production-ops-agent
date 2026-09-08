# 次级开源对标：OpenSRE 与 kagent

> 状态：历史讨论或研究证据，非当前规格。2026-09-06 已确认的产品边界以仓库根目录 SPEC.md 和 docs/adr/0001-readonly-investigation-boundary.md 为准；本文旧的候选、建议及待决表述不覆盖该共识。

2026-09-06 只读官方 README、架构说明和 issue 全文。未部署、未复现，issue 状态是读取时网页状态，不代表缺陷已在当前 main 确认。主对标 HolmesGPT 另行研究。

## OpenSRE：最接近完整 AI SRE 产品的辅助对标

[官方仓库](https://github.com/Tracer-Cloud/opensre) 当前自称 v0.1/Public Alpha，说明 API/集成仍可能变化。提供交互式 CLI、headless、Python AgentSession；把日志、指标、trace、近期发布和 runbook 纳入调查，输出证据链接，支持可选 remediation、会话恢复与成本统计。README 已有工具审批入口，不能称其“没有审批”。可复用的是调查会话、集成和运行接口，不必重做完整聊天产品；其跨云、通信和数据平台范围明显大于本项目。

具体问题来源：[issue #6039](https://github.com/Tracer-Cloud/opensre/issues/6039) 报告调度任务结束时用旧对象覆盖运行期间的用户暂停/修改，提供阻塞执行→暂停→放行的复现步骤，定位 `cd7e1b913`，并明确仅做提取函数隔离验证、未跑完整集成。读取时 Open 且关联 #6044。它证明有具体报告与可验证问题，**不证明最新代码仍坏**。对本项目有价值的工作主题是“人工暂停不会被迟到任务完成覆盖”，需先核关联修复，再在真实导入路径复现；已有修复就复用并加入自己的端到端场景。

## kagent：Kubernetes Agent 运行平台对标

[官方仓库](https://github.com/kagent-dev/kagent) 定位为 Kubernetes-native agent framework；Agent/ModelConfig/工具由 CRD 表达，包含 controller、UI、ADK engine 和 CLI，支持 MCP 及 OpenTelemetry tracing，处于活跃开发。适合借鉴部署、配置和可观测性；它是通用运行平台，不是直接替代本项目的事故处理业务。整体引入意味着接受 CRD/controller/ADK 运维与版本成本。

具体来源：[issue #2688](https://github.com/kagent-dev/kagent/issues/2688) 在 9 月 3 日提出 v2 MCPToolBinding 无法表达 RequireApproval/AllowedHeaders，要求 CRD→compiler→ADK→人工继续的完整契约；读取时 Open/Backlog。这是特定 v2 接口报告，不能扩大成“kagent 没有 HITL”。[issue #2669](https://github.com/kagent-dev/kagent/issues/2669) 请求保留模型缓存 token 并输出指标，列出 adapter 源码位置，读取时 Open、有关联 #2676。可借鉴的真实问题是“UI 声称支持的审批是否贯穿真实调用”和“成本变化能否追到缓存命中”；先审修复状态再决定贡献，不能直接复制 issue 当独创需求。

## K8sGPT：保留为轻量基线

[官方 README](https://github.com/k8sgpt-ai/k8sgpt) 已提供资源 analyzers、operator 持续扫描、MCP、custom analyzer 和模型解释。它适合验证“规则扫描加解释已经能解决多少问题”，不是首选完整 Agent 底座。当前只核其功能，不杜撰缺口。

建议：HolmesGPT 主线复用，OpenSRE 对照事故产品流程，kagent 对照运行与审批可观测性；不叠加三套平台。项目价值落在真实业务故障的补充证据、人工控制一致性与恢复确认。以上是设计建议，尚未证明上游无法满足，必须用所选版本与具体业务运行验证。
