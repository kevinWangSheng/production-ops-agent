# M0 上游与 OTel 固定源码映射草案

2026-09-08；仅公开源码核查。复用 [上游计划](../research/upstream-led-project-plan-2026-09-06.md) 和 [Holmes审计](../research/holmes-source-audit-2026-09-07.md)，重新抓取原文。版本固定表示本批输入可复核，不宣称最新、已运行或镜像可复现。实际来源 URL、原文件 SHA256、原样文件和许可证在 [sources.json](../evidence/m0-c/sources.json)。未读取任何 `.env`（包括上游模板）。

## HolmesGPT

保持已有研究 SHA `5e983c17f30e93099c7d775167266d4cd1d586c4`；[config.py](https://github.com/HolmesGPT/holmesgpt/blob/5e983c17f30e93099c7d775167266d4cd1d586c4/holmes/config.py) 的 create_tool_executor 接受工具标签、enable_all_toolsets_possible、前提缓存和复用参数；本次实际读取这一实现。[models.py](https://github.com/HolmesGPT/holmesgpt/blob/5e983c17f30e93099c7d775167266d4cd1d586c4/holmes/core/models.py) 的 ToolCallResult/format_tool_result_data 提供结构化结果到模型消息的参考，不能直接证明本项目持久证据可解析。Apache-2.0 LICENSE 原样留存。

建议配置草案：显式启用目标范围只读工具，禁用“自动启用所有可用工具”，独立 endpoint/权限控制；模型 profile 沿用已批准 DeepSeek，实际 provider 参数和 image digest 未运行冻结。基线必须记录配置 hash、模型/工具/权限/预算及无法匹配项；不照搬任意 shell 和供应商状态存储。

复用映射：F3 调查输入和结构化工具结果可参考；F1/F6 的外部因果评价与健康观察、F11 的独立 ReleaseObservation、F7 外部权限与动作审计需要本项目合同适配/扩展。其余全部活动功能的细粒度源码/运行映射仍沿 F14 待完成；既有 issue 报告和源码观察不升级为已复现缺陷。本轮未重新查询 issue 当前状态、未复现缺陷、未运行 unchanged baseline 或 adopted fix。

## OTel Demo

选定可核查历史 release **2.0.2**，tag 解析到 commit **63649d6d6a59de88fb421b88c3c3a6185b6d21ad**，用于本批静态草案，最终运行版本可按兼容性证据调整。[固定 Compose](https://github.com/open-telemetry/opentelemetry-demo/blob/63649d6d6a59de88fb421b88c3c3a6185b6d21ad/docker-compose.yml)、[Collector](https://github.com/open-telemetry/opentelemetry-demo/blob/63649d6d6a59de88fb421b88c3c3a6185b6d21ad/src/otel-collector/otelcol-config.yml)、[Prometheus](https://github.com/open-telemetry/opentelemetry-demo/blob/63649d6d6a59de88fb421b88c3c3a6185b6d21ad/src/prometheus/prometheus-config.yaml)。

核查事实：checkout 显式 OTEL_SERVICE_NAME=checkout，关联 cart/currency/email/payment/product-catalog/shipping 等依赖。Collector metrics 出口到 prometheus:9090/api/v1/otlp；traces 到 jaeger:4317；logs 到 opensearch:9200 的 otel index。以上是内部遥测写入出口，**不是授权给调查者的只读查询入口**。Prometheus 提升 service.name/service.namespace/service.instance.id 等资源属性；未列 service.version 与 Kubernetes 不可变 UID。Compose Prometheus 留存为 1h；其他后端留存仍未验证。镜像使用变量插值，本轮没有解析变量或获取 digest。

映射草案：指标查询使用隔离 Prometheus 只读 API；trace 使用只读 Jaeger query；日志使用 OpenSearch 只读 index 查询；每个来源需真实拒绝跨目标与写操作。integration/cluster UID/namespace/resource UID/revision 需从部署注册记录和遥测变换建立显式关联，不能用 service.name 充当不可变身份。Compose 不能证明 F6 要求的 Kubernetes deployment/pod 信号：后续固定 Helm 版本、Kubernetes API 只读凭据与正常采样后才能补齐；当前无 chart/hash 或实际标签一致性证明。

HealthProfile 草案包含 deployment、有效请求量、error、latency、pods、相关 dependencies，窗口和阈值待正常流量 baseline 校准。发布实验应变更应用镜像/配置并记录 before/after digest，采集旧新实例与发布前后窗口；不以 chart 重建代替发布。1h 默认保留是否足够必须根据实验跨度测定后显式改配置；不能凭无数据证明 healthy。

尚未运行 Compose/Helm、故障注入、流量/重置、查询、权限拒绝、资源测量、模型或 trace。正常遥测前提、实际 endpoint/凭据scope、镜像digest、全配置hash、来源留存、延迟回归和上游基线均是后续工作，不作为本批 blocker 隐藏或宣称完成。
