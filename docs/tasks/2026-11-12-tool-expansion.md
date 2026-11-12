# 工具面拓展（对标 HolmesGPT，M3 之后）

- 状态：待开始（计划已立，决定题待裁决，未开工）
- 更新日期：2026-11-12
- 依据：用户 2026-11-11 方向「对标上游拓宽调查数据面、只保自动化主路径」，2026-11-12 补充标注（下文「范围与状态」「范围原则」）；C3 第 8 节；`PRODUCT-CONSTRAINTS.md:12`；上游 HolmesGPT `a0009195`（工具集清单见 `scripts/holmes_baseline/config.py:48-98`，共 48 个）。完整计划与 Kubernetes 预审在仓库外，本记录已收录其中决定所需的内容。
- 工作区：本记录由 `chore/tool-expansion-plan` 提交；各实施项开工时另开 worktree 并写入本节。

## 目标与范围

让调查能看到集群对象、Pod 日志、更细的 trace 与指标，作为新的扩展档位加入；现有 `otel-demo` 档位字节不变，已有验收证据不重跑。目标之一是能跑 ITBench。总跟踪项 #307 保留，剩余部分已拆到下表各项。

范围与状态（2026-11-12 标注，权威高于计划前文）：

- 这是计划与预审材料，不是实现报告。没有下载或安装 benchmark，没有回放 adapter、真实 Run 或评分接线；不能把计划中的工具称为已可用。
- ITBench-Lite SRE（35 个离线快照）与 ITBench 现场版（公开表 28 行、仓库目录 26 个场景）都可以做，但分轨、分证据、分结果；「ITBench 适配完成」必须注明轨道。
- 完整的生产级脱敏延期到产品化。原型只用合成 lab 或受控快照，不接真实秘密数据，不宣称生产就绪。
- 工具按需发现（search/catalog）未决定：现在只记录工具规模与上下文成本问题，固定注册表、档位与 `tool_schema_revision` 的语义不变。

## 前提与完成条件

- 前提：决定题裁决写入本记录（合同 r1）；一台能起 kind lab 的主机（本机腾出内存或用户决定租机）；A2 开工前补跑全量探针。
- 完成条件：各项以对应 issue 的「完成标志」栏为准；阶段 A 整体以 A7 的端到端主路径 Run 为准：报告同时引用指标、trace、集群对象、日志四类证据，仓库里有 `summary.json` 与 LangSmith trace 链接。

## 范围原则

1. **共同执行合同，分开语义实现**：Kubernetes 对象、Pod 日志、Prometheus、trace 共用只读执行器的超时、大小、审计和证据边界，各自拥有后端传输、参数校验和投影。任务清单（TodoWrite）与 runbook/skill 属调查行为或知识工具，不与数据源工具归为一类，也不默认计入同一能力分数。
2. **上游是参考，不是全量复制清单**：上游有、目标环境有数据、产品范围允许且有可观察收益才实现；没数据、违反约束或只是名称相似的，记录排除理由。上游没有的工具不承担对齐义务。
3. **优先适配真实语义**：本地后端与上游不同时（如 Jaeger 对 Tempo），按本地可验证接口实现对应子集，名称相同不等于行为等价。确需大改时按合同做完整 adapter，不为少改保留错误语义。
4. **模型上下文是后续评估维度**：schema 总量、选错工具、漏调用、额外请求与成本在实际运行中测量，是否做 catalog/search 等有数据再定。

## 阶段与工作项

估时为估计：一个执行 Agent 加独立测试作者加独立审查的工作日，只含主路径与红线。阶段 A 串行、一个工具集一个 PR，都要占 kind 窗口；开工前提是本机能起 lab（2026-11-11 swap 已用 13.5/14.3 GB），需用户腾内存或决定租机。

| 序号 | 标题 | 对标上游 | 前置 | 估时 | issue |
|---|---|---|---|---|---|
| A0 | SPEC 有界开放与 C3 第 8 节修订（用户门） | — | Q1–Q8 裁决 | 0.5 天 | #552 |
| A1 | lab 只读集群身份与拒绝取证 | chart 默认 RBAC | A0 | 0.5–1 天 | #553 |
| A2 | 扩展档位与集群对象只读查询 | `kubernetes/core` | A1 | 2–3 天 | #554 |
| A3 | Pod 日志 | `kubernetes/logs` | A2、Q3 | 1–1.5 天 | #555 |
| A4 | trace 按标签过滤、按 ID 取整条、列服务与操作 | `grafana/tempo` 子集，落在 Jaeger | A2、N3 | 1–1.5 天 | #556 |
| A5 | Prometheus 即时查询、序列与全部标签 | `prometheus/metrics` | A2 | 0.5–1 天 | #557 |
| A6 | 按列查询集群对象 | `kubernetes_tabular_query` | A2 | 0.5–1 天 | #558 |
| A7 | Kubernetes 调查指导 + 端到端主路径 Run | `generic_ask.jinja2` K8s 段 | A3–A5 | 0.5–1 天 | #559 |
| B1 | OpenSearch 日志检索 | `elasticsearch/data` | A2、开 OpenSearch 的环境 | 2–3 天 + 环境 0.5 天 | #560 |
| B2 | 告警规则、抓取目标与指标元数据 | Prometheus 余下工具 | A5、代理放行 | 1 天 | #561 |
| B3 | Pod 与节点实时用量 | `kubernetes/live-metrics` | A2、metrics-server | 0.5–1 天 | #562 |
| B4 | Helm 变更历史 | `helm/core` | A2、Q7 | 1.5–2 天 | #563 |
| B5 | ITBench 现场版接入预审 | — | 阶段 A | 1 天 | #564 |
| C1 | 调查任务清单工具 | `core_investigation` | A7 | 1 天 + 对照 0.5 天 | #565 |
| C2 | 按需取已审核 runbook 与复盘知识 | `skills` | 无 | 1.5–2 天 | #566 |
| C3 | Kafka 只读（不读消息内容） | `kafka/admin` 子集 | 开 Kafka、另批出口 | 2 天 | #567 |
| C4 | ArgoCD 只读 | `argocd/core` 子集 | 目标环境有 ArgoCD | 1.5 天 | #568 |
| C5 | Loki 或 VictoriaLogs | `grafana/loki`、`victorialogs` | 目标环境用其一 | 各 1.5 天 | #569 |
| C6 | 数据库只读查询 | `database/sql`、MongoDB | D3 裁决为加 | 未估 | #570 |
| N1 | ITBench-Lite 快照回放适配探针 | — | 无 | 未估 | #571 |
| — | 网页 UI 重做（设计稿已有，排在工具拓展之后） | — | 阶段 A | 未估 | #572 |

合计（估计）：A 6.5–10.5 天、真实 Run 7–9 次、kind 约 8–10 小时；B 5–7 天加一份预审；C（不含 C6）约 7.5–8.5 天，C4、C5 有条件。

不加的 32 个上游工具集，理由分六类：让模型自拼命令或自选地址（`bash`、`internet`、`connectivity_check`），需要宿主或节点特权（`docker`、`cilium`、`inspektor-gadget`），厂商或平台专属（Datadog、New Relic、Coralogix、AKS、OpenShift、Robusta、ServiceNow、Confluence、Notion、Slab），lab 没有该组件（RabbitMQ、Hubble、Crossplane、KubeVela），不是数据源或是给人看的（Grafana 面板、`opensearch/query_assist`、`elasticsearch/cluster`），多租户（`prometheus/multi-tenant-metrics`，产品约束排除）；逐项核数以 `scripts/holmes_baseline/config.py:48-98` 为准。

## 待决（全部未裁决）

| 编号 | 问题 | 推荐 |
|---|---|---|
| Q1 | SPEC `:26` 写着产品不持有 Kubernetes 凭据，F7 静态验收按此写 | 改为「扩展档位持有一份只读 ServiceAccount token」，A1 取证挂为扩展档位证据 |
| Q2 | 允许模型写 jq 表达式在产品里执行吗 | 允许，独立子进程、清空环境变量、超时与输出上限；C3 §8 补一句其定性 |
| Q3 | 日志、ConfigMap 可能含秘密，登记规则只许 `False` | 改规则：先脱敏再入库，补环境变量值脱敏，写明裸密钥识别不了的残余风险 |
| Q4 | 集群「当前状态」没有事故时间窗 | 时间记为查询时刻，工具描述写明不能证明事故窗内状态 |
| Q5 | lab 注入故障用的开关 ConfigMap 能否被读到（SPEC `:121` 隔离注入真值） | 允许并照实披露，开关类场景单独标出；区分度不够就换场景，不改授权 |
| Q6 | Chaos Mesh 等上游默认不授权的自定义资源 | 现在不加，留到 B5 |
| Q7 / D1 | Helm 发布记录存在 Secret 里（两份文件同一题） | 先不读 Helm 记录，变更历史来自 ReplicaSet 与事件，B4 降级 |
| Q8 / D2 | 新数据出口一次批到哪（同一题） | 批阶段 A 与 B1–B3（同一合成 lab），C3 另批；LangSmith 内容导出仍只限合成 lab |
| D3 | 加不加模型自写 SQL 的数据库查询 | 不加，遵守 `PRODUCT-CONSTRAINTS.md:12` |
| N1 | Lite 快照如何映射到各查询端点 | 未实现，单独探针 #571 |
| N2 | 现场版与 Lite 同号场景是否逐题一致 | 未确认，不据此合并分数 |
| N3 | Jaeger 能否提供标签过滤、按 ID 取整条、操作发现 | 未确认，A4 开工前做接口探针 |
| N4 | 集群级 nodes、kube-system、事件、控制面的读取范围 | 未裁决，作授权矩阵问题留到 B5；预审推荐的单 namespace 读不到 nodes |
| N5 | 日志、ConfigMap、Pod 环境变量里的秘密字段 | 完整脱敏延期，原型只用受控数据 |
| N6 | 工具 search/catalog 值不值得做 | 未决定，不改固定档位、注册表与 schema revision |
| X1 | 标注写「不修改现有产品安全合同」，而 A0 要开 SPEC 有界段并改 C3 §8（Q2、Q3） | 本记录的理解：PRODUCT-CONSTRAINTS 与基础档位的安全合同不改；扩展档位所需的 SPEC 开放段与 C3 §8 补文字是新增合同，走 A0 用户门。若该标注指 C3 也不改，A3 日志工具无法登记，需用户明确 |

可逆默认（实现者自定）共 16 条，例如：直接 HTTPS GET 打 API server、不装 `kubectl`；每个后端独立 `credential_ref`；Role 只给 `get`、`list`；去掉 `managedFields`；日志默认 100 条、上限 1000；新 Jaeger 工具沿用 `traces_` 前缀。A0 时随决定表 r1 一并写入。

## 下一步与交接

下一个会话与用户逐题对齐 Q1–Q8、D3、N4 与 X1，裁决写入本记录「合同（r1）」节后开 A0。阶段 A 开工前补跑全量探针（预审未跑）。未确认项：lab 里 Helm 记录的实际存储形态、metrics-server 与 cAdvisor 指标、上游 `elasticsearch/data` 对 OpenSearch 的兼容性、Jaeger 过滤参数行为、ITBench 现场版的判分方式与单场景耗时。
