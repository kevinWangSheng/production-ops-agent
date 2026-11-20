# 工具面拓展 B/C 批次与第三方评测

- 状态：对齐完成（用户 2026-11-16 裁决，[决定表 r8](2026-11-12-tool-expansion-contract.md#r82026-11-16bc-批次与评测)）；B1 已合并（#705）；C5a、C5b 已合并（#724、#731）；#666 PR-A/B/C 已合并（#721、#726、#733）；#668 准备已合并（#732）；#500 已合并（#730）；B3、C1、C2、C3、C4、C6、B4 与 #667 预审裁决完成（[r11](2026-11-12-tool-expansion-contract.md#r112026-11-19b3c1c2c3c4c6b4-与-667-预审裁决)，2026-11-19），B3、#667 PR-A 开工
- 更新日期：2026-11-19
- 依据：[阶段 A 任务记录](2026-11-12-tool-expansion.md)与决定表 r1–r7（Q1–Q8、D1–D3、N4 继续有效，r8 改 Q6、Q7/N4 两处）；SPEC 2026-11-16 段；C3 第 8 节；HolmesGPT `a0009195`；预审材料在仓库外（HolmesGPT 评测集题单、第三方基准调研与 SREGym/ITBench-AA 核查，2026-11-16）
- 工作区：本记录分支 `chore/tool-expansion-r8`；各工具开工时另开 worktree 写入下表

## 问题

阶段 A 让调查能看到指标、trace、集群对象与 Pod 日志，但 Elasticsearch/OpenSearch、Loki、数据库、Kafka、Helm、ArgoCD 等常见数据源还没有工具；剩余项原计划「目标环境有数据才做」，现有 lab 与 ITBench-Lite 都没有这些数据，于是无法验收，也排不进日程。

## 方案

用现成的公开基准当验收题，在按小时租用的主机上直接搭题目环境，不依赖 otel-demo 的 kind lab；没有公开题的项用自建场景做功能验收，排在队尾但不推迟。评测分两轨，分数不合并：

- **中立第三方**：ITBench-Lite（离线，已有）；SREGym Lite 17 题，只跑诊断段（#667）。
- **竞品自评集**：HolmesGPT 公开评测集（#666），最后一次集中跑时同题同模型对照 Holmes（#668）。对外称「HolmesGPT 公开评测集」，写明是对方自出题。

## 用户故事

1. 作为值班工程师，我希望 OpsPilot 能检索 Elasticsearch/OpenSearch 与 Loki 里的日志，这样日志在这两类存储里的事故也能被调查。
2. 作为值班工程师，我希望调查能看到 Pod 与节点的实时 CPU、内存用量，这样资源打满、限额过低的事故能被点名。
3. 作为值班工程师，我希望调查能按需取已审核的 runbook，这样团队的排查经验进入调查，且来源与审核状态可见。
4. 作为值班工程师，我希望长调查有任务清单，这样多步排查不漏步。
5. 作为值班工程师，我希望调查能对数据库执行只读查询，这样慢查询、缺索引类事故能被定位。
6. 作为值班工程师，我希望调查能看到 Kafka 主题、消费组滞后与 broker 状态（不读消息内容），这样消费积压类事故能被定位。
7. 作为值班工程师，我希望调查能看到 Helm 发布历史与 ArgoCD 应用同步状态，这样「哪次变更引起的」有直接证据。
8. 作为面试官或评审者，我希望看到 OpsPilot 在中立第三方基准上的分数，这样能判断它不是只在自己出的题上表现好。
9. 作为面试官或评审者，我希望看到 OpsPilot 与 HolmesGPT 在同题同模型下的对比，并知道两边工具面的差异，这样对比可信。
10. 作为项目负责人，我希望每个新工具都有真实 Run 证据证明它被调用且证据被报告引用，这样「支持某数据源」的说法有据可查。
11. 作为项目负责人，我希望新工具不改变 otel-demo 档位，这样已有验收证据不用重跑。

## 实现决定

完整裁决见决定表 r8，要点：

- **顺序**：B1 → C5 → B3 → C2 → C1 → C6 → C3 → B4 → C4。每个工具一个 PR、开工前合同预审；全部合并后集中租一次机（#669）。
- **档位**：新工具全部进扩展档位，`otel-demo` 档位字节不变。
- **入口**：题面原文经网页人工提问交给 OpsPilot，不编告警。
- **判分**：照抄上游 judge 提示词；judge 用仓库已有接收方，二值判分，注明 judge 与上游不同；每题 3 遍，结果摇摆的题补跑。
- **B1**：正式评测用 Elasticsearch 8，工具只用两家共有的读接口；另在 OpenSearch 上冒烟 2 题。
- **硬断言**：6 道带「必须推荐 skill」硬断言的题去掉这一条，只判主回答，并披露；Holmes 对照用同一口径。
- **C2**：上游 runbook/skill 作为新增知识来源「导入的 runbook」入库，有独立的提交、批准、驳回、撤销流程，来源标「HolmesGPT 评测夹具」；复盘知识不变（r11 C2-U1 修改 R8-14）。
- **C6**：做；评测时给题目数据库加只读账号。
- **B4 Helm**：在扩展档位、合成环境内（闸门 `HELM_NOT_LAB`）读取 Helm 发布记录那一类 Secret（`helm.sh/release.v1`）。在工具内解码，只返回发布历史元数据，values 与 manifest 不出工具；网关只放行带固定查询参数的 list，其他 Secret 请求不发出。残余风险：身份可读取合成环境内全部 Secret 的内容，只靠网关收窄到 Helm 记录，CI 用同一 token 直接 list secrets 证明（r11 修正 R8-18 措辞）。
- **C4 ArgoCD**：扩展档位只读 ArgoCD 应用类自定义资源（`argoproj.io`），其他自定义资源照旧不授权。
- **C3 Kafka**：只读主题、消费组与滞后、broker，不读消息内容；lab 自带的 Kafka 不可用时在租机上自装。
- **权限**：题目环境由评测脚本用工程师凭据部署，产品只持有只读身份；SREGym 接入只用 OpsPilot 自己的只读工具。
- **数据流**：租机上由公开基准夹具搭的题目环境按合成 lab 处理，新数据源只在本记录登记；这次不新增接收方。
- **Agent 自定**：去掉每 Run 工具调用次数的旧上限（#500）；核实 worker 能否并行跑多个 Run；题目分诊、端口冲突分批、相对时间题每轮重新部署。

## 测试决定

- 验收入口仍是外部 `IncidentScenario -> IncidentOutcome`，只断言可观察的证据、引用、权限与最终状态。
- 工具完成标准：本机代码与合同测试通过，独立审查完成；真实 Run 调用了该工具，报告引用了它的证据；通过率照实报告，不设分数门槛。
- 合并前集成测试（R8-25）：每个数据源工具起真实后端容器、写合成数据、用工具真实查询，断言返回、截断、错误分类与只读拒绝；不调模型；本机内存不够放 CI 服务容器。单测与替身不替代这一步。
- 分阶段真实验证：B1 与 #666 合并后提前短租冒烟，跑通整条链路（R8-26，#671）；集中租机先每个工具冒烟 1–2 题过检查点再全量（R8-27）。
- 合同测试沿用阶段 A 的先例：每个工具对照只读边界、大小与超时上限、脱敏与证据投影写测试，由未参与实现的 Agent 编写（参照 A2–A6、B2 的测试）。
- 答案隔离：写工具、写提示词的 Agent 不读题目的预期结论；跑完后由 lead 在隔离上下文里分析失败原因。
- 自建场景（C3、B4、C4）只作功能验收，标「自建功能验收，不计分」；SREGym 里的 Kafka 题照常计分。

## 范围外

- ITBench-AA（这一批不跑；下次跑 ITBench-Lite 时核实判分方式，并补上多出的 5 题）。
- 把 Holmes 限成同一工具面的对照。
- 向上游提 issue 或 PR（要用户另行授权）。
- 人工介入与新工具的组合验收（沿用 SPEC 2026-11-12 段）。
- 事故页显示结构化根因（#652）、F1 第 5 步重校准（#295）。

## 工作项

| 序 | 项 | issue | 验收题来源 | 分支 / worktree |
|---|---|---|---|---|
| 1 | B1 Elasticsearch/OpenSearch 日志 | #560 | HolmesGPT 评测集 30 题 | 代码已合并（PR #705，`4052ab4f`，[B1 记录](2026-11-16-tool-expansion-b1.md)）；真实 Run 归 #671 |
| 2 | C5 Loki / VictoriaLogs | #569 | HolmesGPT 评测集 9 题 | C5a Loki、C5b VictoriaLogs 已合并（#724、#731） |
| 3 | B3 Pod 与节点实时用量 | #562 | SREGym 相关题与直接取样 | 预审与裁决完成（r11），实现中（[B3 记录](2026-11-19-tool-expansion-b3.md)，分支 `feature/562-live-usage`，worktree `../poa-562-live`） |
| 4 | C2 按需取 runbook | #566 | HolmesGPT 评测集 6 题 | 预审与裁决完成（r11），按序待开工 |
| 5 | C1 任务清单 | #565 | HolmesGPT 评测集 1 题，加开关对照 | 预审与裁决完成（r11），按序待开工 |
| 6 | C6 数据库只读查询 | #570 | HolmesGPT 评测集 7 题 | 预审与裁决完成（r11），按序待开工 |
| 7 | C3 Kafka 只读 | #567 | 自建，加 SREGym Kafka 题 | 预审与裁决完成（r11），按序待开工 |
| 8 | B4 Helm 变更历史 | #563 | 自建 | 预审与裁决完成（r11），按序待开工 |
| 9 | C4 ArgoCD 只读 | #568 | 自建 | 预审与裁决完成（r11），按序待开工 |
| — | HolmesGPT 评测集适配 / SREGym 适配 / Holmes 对照 | #666 / #667 / #668 | — | #666 PR-A/B/C 已合并（#721、#726、#733），[#666 记录](2026-11-18-holmes-evals-666.md)；#667 预审与裁决完成（r11），PR-A 开工；#668 准备已合并（#732） |
| — | 调查提示词注入本地集群名（r10 H-U1） | #722 | 10 道多集群题 | 预审后开工 |
| — | 提前短租冒烟（B1 与 #666 合并后） | #671 | ES 2–3 题、SREGym 1–2 题 | |
| — | 集中租机（先冒烟检查点再全量） | #669 | 全部 | |

#671 被 #560、#666 阻塞；#669 被上表全部工具项，以及 #666、#667、#668、#671、#500 阻塞（GitHub 阻塞关系已设）。

## 新数据源登记

按 Q8 与 R8-8，每个新数据源落地时在此加一行：返回什么、发往哪里。

- B1（#560，只在 `otel-demo-ext` 且设了 `OPSPILOT_ES_URL`）：租机或 lab 上的 Elasticsearch 8 / OpenSearch 2 的 `GET /_cat/indices/{pattern}?format=json`、`GET /{index}/_mapping`、`POST /{index}/_search`（唯一的 POST，请求体由工具拼，禁用脚本等构造），用独立凭据槽 `es-ro` 的只读 API key 或只读用户。返回：索引列表行、字段映射、按事故时间窗过滤的文档（`_index`、`_id`、`_source` 或 `fields`、`sort`，每个字符串值截到 2000 字符，最多 500 条）与聚合、总数、分片与超时信息。`may_contain_secrets=True`：投影后的响应存入证据库（PG），视图经现有脱敏后进入 Run 所选模型；lab 内随 LangSmith trace 导出（lab 闸门要求 ES 地址是 lab 主机，`ELASTICSEARCH_NOT_LAB`）。jq 在 worker 本机隔离子进程里跑，不外发。不新增接收方。
- C5a（#569，只在 `otel-demo-ext` 且设了 `OPSPILOT_LOKI_URL`）：租机或 lab 上 Loki（直连地址，经 nginx 只读网关）的 `GET /loki/api/v1/query_range`（唯一请求，查询串由工具拼，始终带事故查询窗，`offset`、`@` 与超出查询窗的范围在发请求前拒绝），用独立凭据槽 `loki-ro` 的网关只读账号（Basic 或 bearer），租户由运维配置（`X-Scope-OrgID`）。返回：查询窗内的日志行（`timestamp`、`labels`、`line`，最新在前，最多 500 条，每个字符串值截到 2000 字符）或指标查询的序列（`metric`、`values`）。`may_contain_secrets=True`：投影后的响应存入证据库（PG），视图经现有脱敏后进入 Run 所选模型；lab 内随 LangSmith trace 导出（lab 闸门要求 Loki 地址是 lab 主机，`LOKI_NOT_LAB`）。不新增接收方。
- C5b（#569，只在 `otel-demo-ext` 且设了 `OPSPILOT_VICTORIALOGS_URL`）：租机或 lab 上 VictoriaLogs（直连地址，经 nginx 只读网关）的 `GET /select/logsql/{query,streams,field_names,field_values,hits}`（只有这五条 GET，查询串由工具拼，始终带事故查询窗，`options`、`join`、`union`、`stream_context` 与子查询在发请求前拒绝），用独立凭据槽 `vlogs-ro` 的网关只读账号（Basic 或 bearer），租户由运维配置（`AccountID`/`ProjectID`）。返回：查询窗内的日志条目（VictoriaLogs 返回的整条 JSON，最新在前，最多 500 条，每个字符串值截到 2000 字符）、流与字段名、字段取值及命中数、按 step 分桶的计数。`may_contain_secrets=True`：投影后的响应存入证据库（PG），视图经现有脱敏后进入 Run 所选模型；lab 内随 LangSmith trace 导出（lab 闸门要求地址是 lab 主机，`VICTORIALOGS_NOT_LAB`）。不新增接收方。
- B3（#562，只在 `otel-demo-ext` 且 web 与 worker 都设了 `OPSPILOT_K8S_LIVE_METRICS=1`）：租机或 lab 集群 API server 的 `GET /apis/metrics.k8s.io/v1beta1/pods`（或 `/namespaces/{ns}/pods`）、`GET /apis/metrics.k8s.io/v1beta1/nodes`、`GET /api/v1/nodes`（只取 allocatable），路径写死，用现有 `k8s-ro` 只读身份（A1 清单已授权 `metrics.k8s.io` 的 pods、nodes，RBAC 不改）。返回：每个 Pod（含容器明细）或节点此刻的 CPU 毫核、内存字节、节点占可分配量的百分比、采样时刻与采样窗口；来源时间留空（当前状态类，B3-U1）。`may_contain_secrets=True`：投影后的响应存入证据库（PG），视图经现有脱敏后进入 Run 所选模型；lab 内随 LangSmith trace 导出（地址受现有 `KUBERNETES_NOT_LAB` 闸门约束）。不新增接收方。
- C2（#566，C2a 来源与审核已合并 #737；C2b 只在 `otel-demo-ext` 且 web 与 worker 都设了 `OPSPILOT_KNOWLEDGE_TOOL=1`）：产品自己的 PostgreSQL 里本 Run 目标下当前有效的知识——导入的 runbook（租机上来源为「HolmesGPT 评测夹具」，由评测账号经工作台审核入库）与审核过的复盘知识。worker 运行角色只读（`opspilot_runbook_revisions` 与状态视图，及原有复盘知识读取），只读快照事务；建 Run 时 web 运行角色另读出本目标的目录（名称、说明、版本、新鲜度，最多 50 条、8 KB）写进输入快照 v7。返回：一条知识的名称、说明、正文（runbook 全文最长 32 KiB；复盘知识的旧证据 ID 换成计数）、来源、审核状态、审核人与时间、新鲜度。`may_contain_secrets=True`：视图经现有脱敏后进入 Run 所选模型，并作为证据存入 PG；目录里的说明写进快照前脱敏；lab 内随 LangSmith trace 导出。不新增接收方，不调外部服务。

## 待决

无。实施中再冒出的不可逆项或合同层取舍，追加到 r8。
