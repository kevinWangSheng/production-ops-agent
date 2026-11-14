# 工具面拓展（对标 HolmesGPT，M3 之后）

- 状态：进行中（决定表 r7，2026-11-13）。已合并：A0 #575、A1 #589、A2 #588、A5 #593、A4 #594、A3 #598、A6 #600；ITBench-Lite 转换 #586、判分 #587、假 Kubernetes API server #603、逐题驱动 #606；租机直接取样脚本 #607。B2 #597 审查已过，等 CI 后合并。本机剩余：A7 #559 本机部分（提示词变体、钉值测试、四类证据判定与驱动）与 #605 分档、词表生成器，均已开工。之后一次租机跑完 #580 集群取证、A7 主路径 Run 与 Lite 35 题 #571（A7 合并后才跑 Lite，见 r7 A7-14；手册 [租机手册](2026-11-13-rental-runbook.md)）
- 更新日期：2026-11-13
- 依据：用户 2026-11-11 方向「对标上游拓宽调查数据面、只保自动化主路径」，2026-11-12 补充标注（下文「范围与状态」「范围原则」）；C3 第 8 节；`PRODUCT-CONSTRAINTS.md:12`；上游 HolmesGPT `a0009195`（工具集清单见 `scripts/holmes_baseline/config.py:48-98`，共 48 个）。完整计划与 Kubernetes 预审在仓库外，本记录已收录其中决定所需的内容。
- 工作区：各实施项开工时另开 worktree 并写入各自的任务记录。

## 目标与范围

让调查能看到集群对象、Pod 日志、更细的 trace 与指标，作为新的扩展档位加入；现有 `otel-demo` 档位字节不变，已有验收证据不重跑。目标之一是能跑 ITBench。总跟踪项 #307 保留，剩余部分已拆到下表各项。

范围与状态（2026-11-12 标注，权威高于计划前文）：

- 这是计划与预审材料，不是实现报告。没有下载或安装 benchmark，没有回放 adapter、真实 Run 或评分接线；不能把计划中的工具称为已可用。
- ITBench-Lite SRE（35 个离线快照）与 ITBench 现场版（公开表 28 行、仓库目录 26 个场景）都可以做，但分轨、分证据、分结果；「ITBench 适配完成」必须注明轨道。
- 完整的生产级脱敏延期到产品化。原型只用合成 lab 或受控快照，不接真实秘密数据，不宣称生产就绪。
- 工具按需发现（search/catalog）未决定：现在只记录工具规模与上下文成本问题，固定注册表、档位与 `tool_schema_revision` 的语义不变。

## 前提与完成条件

- 前提：代码与合同测试在本机做，不需要集群；集群取证与真实 Run 需要一台能起 kind lab 的主机（8 核 / 32 GB amd64），用户 2026-11-12 决定现在不租，攒到需要时租一次集中做。
- 完成条件：各项以对应 issue 的「完成标志」栏为准；阶段 A 整体以 A7 的端到端主路径 Run 为准：报告同时引用指标、trace、集群对象、日志四类证据，仓库里有 `summary.json` 与 LangSmith trace 链接。

## 范围原则

1. **共同执行合同，分开语义实现**：Kubernetes 对象、Pod 日志、Prometheus、trace 共用只读执行器的超时、大小、审计和证据边界，各自拥有后端传输、参数校验和投影。任务清单（TodoWrite）与 runbook/skill 属调查行为或知识工具，不与数据源工具归为一类，也不默认计入同一能力分数。
2. **上游是参考，不是全量复制清单**：上游有、目标环境有数据、产品范围允许且有可观察收益才实现；没数据、违反约束或只是名称相似的，记录排除理由。上游没有的工具不承担对齐义务。
3. **优先适配真实语义**：本地后端与上游不同时（如 Jaeger 对 Tempo），按本地可验证接口实现对应子集，名称相同不等于行为等价。确需大改时按合同做完整 adapter，不为少改保留错误语义。
4. **模型上下文是后续评估维度**：schema 总量、选错工具、漏调用、额外请求与成本在实际运行中测量，是否做 catalog/search 等有数据再定。

## 阶段与工作项

估时为估计：一个执行 Agent 加独立测试作者加独立审查的工作日，只含主路径与红线。阶段 A 串行、一个工具集一个 PR。顺序调整（用户 2026-11-12）：先在本机做 A2 起各项的代码与合同测试，A1 的集群取证和各项的真实 Run 攒到一次租机；各项的 issue 在代码 PR 合并时关闭，真实证据统一归租机集中取证（#580）；issue 之间的先后用 GitHub 的阻塞关系表示。

| 序号 | 标题 | 对标上游 | 前置 | 估时 | issue |
|---|---|---|---|---|---|
| A0 | SPEC 有界开放，C3 第 3、8、12 节与 PRODUCT-CONSTRAINTS 修订（用户门） | — | 已裁决 | 0.5 天 | #552 |
| A1 | [lab 只读集群身份与拒绝取证](2026-11-12-tool-expansion-a1.md) | chart 默认 RBAC | A0 | 0.5–1 天 | #553 |
| A2 | [扩展档位与集群对象只读查询](2026-11-12-tool-expansion-a2.md) | `kubernetes/core` | A1 | 2–3 天 | #554 |
| A3 | [Pod 日志](2026-11-12-tool-expansion-a3.md) | `kubernetes/logs` | A2 | 1–1.5 天 | #555 |
| A4 | [trace 按标签过滤、按 ID 取整条、列服务与操作](2026-11-12-tool-expansion-a4.md) | `grafana/tempo` 子集，落在 Jaeger | A2（N3 已有结论） | 1–1.5 天 | #556 |
| A5 | [Prometheus 即时查询、序列与全部标签](2026-11-12-tool-expansion-a5.md) | `prometheus/metrics` | A2 | 0.5–1 天 | #557 |
| A6 | [按列查询集群对象](2026-11-12-tool-expansion-a6.md) | `kubernetes_tabular_query` | A2 | 0.5–1 天 | #558 |
| A7 | Kubernetes 调查指导 + 端到端主路径 Run | `generic_ask.jinja2` K8s 段 | A3–A5 | 0.5–1 天 | #559 |
| B1 | OpenSearch 日志检索 | `elasticsearch/data` | A2、开 OpenSearch 的环境 | 2–3 天 + 环境 0.5 天 | #560 |
| B2 | [告警规则、抓取目标与指标元数据](2026-11-12-tool-expansion-b2.md) | Prometheus 余下工具 | A5、代理放行 | 1 天 | #561 |
| B3 | Pod 与节点实时用量 | `kubernetes/live-metrics` | A2、metrics-server | 0.5–1 天 | #562 |
| B4 | Helm 变更历史（Q7：先不读 Helm 记录，降级） | `helm/core` | A2 | 1.5–2 天 | #563 |
| B5 | ITBench 现场版接入预审 | — | 阶段 A | 1 天 | #564 |
| C1 | 调查任务清单工具 | `core_investigation` | A7 | 1 天 + 对照 0.5 天 | #565 |
| C2 | 按需取已审核 runbook 与复盘知识 | `skills` | 无 | 1.5–2 天 | #566 |
| C3 | Kafka 只读（不读消息内容） | `kafka/admin` 子集 | 开 Kafka、另批出口 | 2 天 | #567 |
| C4 | ArgoCD 只读 | `argocd/core` 子集 | 目标环境有 ArgoCD | 1.5 天 | #568 |
| C5 | Loki 或 VictoriaLogs | `grafana/loki`、`victorialogs` | 目标环境用其一 | 各 1.5 天 | #569 |
| C6 | 数据库只读查询 | `database/sql`、MongoDB | 目标环境有 SQL 数据库（lab 没有） | 未估 | #570 |
| — | 租机集中取证与真实 Run | — | A1–A6、B2 的代码 | 机时约 8–10 小时 | #580 |
| N1 | ITBench-Lite 快照回放适配（[开工说明](2026-11-12-itbench-lite-adapter.md)） | — | 假 server 部分依赖 A2、A3 | 约 8 天 + 机时 1–1.5 天（估计） | #571、#581–#583 |
| — | 网页 UI 重做（设计稿已有，排在工具拓展之后） | — | 阶段 A | 未估 | #572 |

合计（估计）：A 6.5–10.5 天、真实 Run 7–9 次、kind 约 8–10 小时；B 5–7 天加一份预审；C（不含 C6）约 7.5–8.5 天，C4、C5 有条件。

不加的上游工具集（`database/sql` 按 D3 裁决移到 C6 后剩 31 个），理由分六类：让模型自拼命令或自选地址（`bash`、`internet`、`connectivity_check`），需要宿主或节点特权（`docker`、`cilium`、`inspektor-gadget`），厂商或平台专属（Datadog、New Relic、Coralogix、AKS、OpenShift、Robusta、ServiceNow、Confluence、Notion、Slab），lab 没有该组件（RabbitMQ、Hubble、Crossplane、KubeVela），不是数据源或是给人看的（Grafana 面板、`opensearch/query_assist`、`elasticsearch/cluster`），多租户（`prometheus/multi-tenant-metrics`，产品约束排除）；逐项核数以 `scripts/holmes_baseline/config.py:48-98` 为准。

## 合同（r1）

用户 2026-11-12 裁决，全表与可逆默认见[决定表](2026-11-12-tool-expansion-contract.md)。r2 为 A2 开工前预审追加的可逆默认；r3 为 A1、A3–A6、B2 开工前预审追加的可逆默认与 N3 结论，唯一新的用户裁决项是 B2-1，用户 2026-11-12 裁决为 a（对所有账号放开）。各项范围与接口见阶段表里链接的开工说明。

| 编号 | 裁决要点 |
|---|---|
| X1 / Q1 | SPEC「产品不持有 Kubernetes 凭据」直接改；扩展档位持有一份只读集群凭据 |
| N4 | 整个集群只读（含 nodes、事件、kube-system），Secret 不可读；不走单 namespace |
| Q2 | 模型可写 jq，关在无环境变量、有超时与输出上限的子进程里 |
| Q3 / N5 | 可能含秘密的数据源可以登记，脱敏不挡加工具；只接合成 lab 或受控快照；环境变量值脱敏延后（#574） |
| Q4 | 集群当前状态记为查询时刻，写明不能证明事故窗内状态 |
| Q5 | 注入开关 ConfigMap 可读并披露，开关类场景标出 |
| Q6 | Chaos Mesh 等自定义资源先不授权，留到 B5 |
| Q7 | 先不读 Helm 记录，B4 降级 |
| Q8 | 合成 lab 内新数据源不再逐个报批，在本记录登记返回什么、发往哪里 |
| D3 | 模型自写 SQL 不在策略层禁止，只读由工具保证；C6 排第三批 |

## 新数据源登记

按 Q8，每个新数据源落地时在此加一行：数据源、返回什么、发往哪里。

- A4（#556，只在 `otel-demo-ext`）：lab Jaeger 1.53.0 查询 API 的 `/api/traces`（按服务、标签、操作、时长）、`/api/traces/{id}`、`/api/services`、`/api/operations`，经现有 Jaeger 传输 GET、匿名直连；返回按 `traces_search` 同一白名单投影的 span 行（其他标签值不出），以及服务名、操作名清单（脱敏、截短）；去向与 `traces_search` 相同：模型上下文与证据库，LangSmith 只在 lab 闸门放行时导出。
- A5（#557）lab Prometheus 的 `/api/v1/query`、`/api/v1/series`、`/api/v1/labels`（沿用现有只读代理与只读账号）：某一时刻的 PromQL 结果、匹配选择器的序列标签集（≤100 条）、标签名（≤100 条）。原样响应存入证据库；视图经现有脱敏后进入 Run 所选模型；lab 内随 LangSmith trace 导出（`otel-demo-ext` 已在 lab 闸门允许名单）。不新增接收方。
- B2（#561，只在 `otel-demo-ext`）lab Prometheus 的 `/api/v1/rules`、`/api/v1/targets?state=active`、`/api/v1/metadata`（只读代理按 B2-1 对所有账号放行这三个 GET，含 Holmes 基线账号；沿用 A5 的传输与只读账号）：查询那一刻的告警与记录规则（含在响的告警）、某个抓取任务的活跃抓取目标（去掉 `discoveredLabels`）、指标类型与说明（≤100 个指标）。规则响应原样存入证据库，目标与元数据存投影记录并附原始响应 sha256；三个工具登记 `may_contain_secrets=True`（注解、标签、scrapeUrl 可能含未命名的私有值，脱敏识别不了裸值）；去向同 A5：模型上下文与证据库，lab 内随 LangSmith trace 导出。不新增接收方。
- A3（#555，只在 `otel-demo-ext`）：lab 集群 API server 的 `GET /api/v1/namespaces/{ns}/pods/{pod}`（取容器名）与其 `log` 子资源（每容器当前与 `previous` 各一次，`timestamps=true`、`sinceTime`、`limitBytes`），经 A2 的只读 GET 传输与 `k8s-ro` token；返回窗内日志行（时间戳、容器、实例、正文，每行截到 2000 字符，默认 100 行、最多 1000 行）与计数、备注。`may_contain_secrets=True`：原样响应存入证据库，视图经现有脱敏后进入 Run 所选模型；lab 内随 LangSmith trace 导出（lab 闸门已含 Kubernetes API 地址检查）。正则过滤在 worker 本机的隔离子进程里跑，不外发。不新增接收方。
- A6（#558，只在 `otel-demo-ext`）：没有新数据源、没有新出口。复用 A2 的同一读取（lab 集群 API server 资源表内各类的 list GET、`k8s-ro` token、4 MiB 读取上限、事件按授权框筛选），在 worker 本机按模型给的列把对象抽成表：返回表头、每对象一行单元格、行数、范围与备注。去向同 A2：原样表格存入证据库，视图经现有脱敏后进入 Run 所选模型，lab 内随 LangSmith trace 导出。`filter_pattern` 在 A3 的隔离正则子进程里跑，不外发。不新增接收方。

## 待决

| 编号 | 问题 | 状态 |
|---|---|---|
| N1 | Lite 快照如何映射到各查询端点 | 探针已做，映射见[开工说明](2026-11-12-itbench-lite-adapter.md)；未实现 |
| L6 | ITBench-Lite 官方 LLM judge 用哪个模型、发给哪家（已有接收方，或照官方示例新增 OpenRouter） | 已裁决（用户 2026-11-13：a，用仓库已有接收方，默认 Z.ai，结果注明与官方示例不同），见[决定表 r4](2026-11-12-tool-expansion-contract.md) L6 行 |
| N2 | 现场版与 Lite 同号场景是否逐题一致 | 未确认，不据此合并分数 |
| N3 | Jaeger 能否提供标签过滤、按 ID 取整条、操作发现 | 已有结论（读 v1.53.0 源码）：支持标签与时长过滤、按 ID 取整条、列服务与操作，不支持标签名发现；未在真实实例取样的项见[决定表 r3](2026-11-12-tool-expansion-contract.md)「N3」 |
| N6 | 工具 search/catalog 值不值得做 | 未决定，不改固定档位、注册表与 schema revision |

## 下一步与交接

实现顺序 A2 → A5 → A4 → A3 → A6，各按开工说明由实现会话开 worktree、走功能 PR 流程；A1 的脚本与单元测试可与 A2 并行写，真实取证和各项真实 Run 等一次租机集中做；B2 在 A5 合并后开工。A3–A6 与 B2 的全量探针未跑，A2 合并后各开草稿 PR 经 CI 补跑（预计见决定表 r3）。

ITBench-Lite 的集群对象与日志探针已做（报告在仓库外 `~/.agent/handoffs/itbench-lite-k8s-logs-probe-2026-11-12.md`）：可用只读的假 API server 回放集群对象，日志部分可回放；用户 2026-11-12 已对齐四点（报告由评测侧确定性转换成答案格式、产品报告格式不变；官方 LLM judge 分与确定性实体匹配分都报；35 题全留分母并按数据覆盖分档；第一轮只跑 OpsPilot、标为单边结果），适配的[开工说明](2026-11-12-itbench-lite-adapter.md)已写，只剩 judge 接收方（L6）待用户裁决。

未确认项：控制侧容器能否按 kind 证书里的名字访问 API server、lab 里 Helm 记录的实际存储形态、metrics-server 与 cAdvisor 指标、上游 `elasticsearch/data` 对 OpenSearch 的兼容性、Jaeger 标签值字符串化与整条 trace 体积、ITBench 现场版的判分方式与单场景耗时。
