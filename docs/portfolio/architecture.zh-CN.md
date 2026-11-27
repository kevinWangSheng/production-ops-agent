# 架构

[English](architecture.md) | 简体中文

运行着什么、各自负责什么，以及一次事故如何在其中流转。按 2026-11-10 的 main 核对（发布 `v0.1.1`；这里链接的证据是在 `v0.1.0` 上记录的）。权威合同是 [C3 技术方案](../design/technical-proposal-2026-09-07.md)；本页与 C3 不一致时以 C3 为准，「代码与 C3 的出入」一节列出的差异是已知并在跟踪的。

## 进程

一个产品镜像（`Dockerfile`，Python 3.12）在控制主机上以 Docker Compose 下的多个独立容器运行（[deploy/compose/compose.yaml](../../deploy/compose/compose.yaml)）。产品不部署在被调查的集群里；没有 Helm chart（C3 §12）。

| 容器 | 入口 | 做什么 | 持有的凭据 |
|---|---|---|---|
| `postgres` | 官方 `postgres:17.9`，按 digest 钉住 | 全部业务记录：事故、Run、步骤、证据、控制、观察、复盘、知识、trace outbox。不映射主机端口。 | owner 密码 |
| `migrate`、`init-roles` | `python -m opspilot.schema migrate`、`python -m opspilot.deploy.roles` | 一次性任务：在所有运行时停止的情况下执行 Alembic 迁移，然后创建四个运行时登录 | 仅 owner 登录 |
| `web` | `python -m opspilot.web` | 工作台（FastAPI + Jinja + SSE）、告警/事件/版本变更接收、人工控制、复盘审核。一个后台循环在合格结论后授权恢复观察，并执行发布授权与异常检查。只发布在 `127.0.0.1` 上。 | `opspilot_web_app` 数据库登录、UI 密码哈希、接收 token 的摘要 |
| `worker` | `python -m opspilot.worker_main` | 认领排队中的 Run 并运行调查循环（模型 + 只读工具），执行截止时间清扫和积压检查，另有一个起草复盘的线程和一个 trace-outbox 线程 | `opspilot_worker_app` 数据库登录、模型 key、目标读取凭据（`OPSPILOT_OTEL_*`） |
| `observer` | `python -m opspilot.observer` | 按带版本的 HealthProfile 做确定性的恢复与发布采样；从不调用模型 | `opspilot_observer_app` 数据库登录、它自己的只读 Prometheus 账号 |
| `retention` | `python -m opspilot.retention --days 30`（profile `maintenance`，手动运行） | 默认 dry-run；`--apply` 在事故关闭 30 天后删除原始 payload（#244） | `opspilot_retention_app` 数据库登录 |

每个运行时都在容器端口 9464 上提供 `/healthz`、`/readyz` 和 `/metrics`（`opspilot/health.py`）。

## 调查路径

1. **接收。** Alertmanager 带 bearer token 向 `POST /intake/alertmanager` 投递。web 对投递去重，新建事故或挂到已有事故上，并按标签精确匹配从 `target_identities.json` 解析目标。匹配不到：事故交给人处理，不启动 Run。告警文本作为不可信上下文进入 Run，不作为指令（M1-04）。
2. **认领。** 一个 worker 槽位带租约和执行 epoch 认领 Run。每次写入都以该租约和当前的人工控制代数作为栅栏，因此第二个 worker、过期的尝试或已被推翻的决定都无法更新事故（`opspilot/worker_main.py` docstring；C3 §6–7）。
3. **循环。** 手写的循环（比较过 LangGraph 并推迟采用，[ADR-0004](../adr/0004-langgraph-orchestration.md)）通过 OpenAI 兼容客户端调用 DeepSeek `deepseek-flash`。每一轮模型调用和每个工具结果都在下一步开始前提交，因此被杀掉的 worker 能从已存的行恢复（[ADR-0003](../adr/0003-business-state-recovery-authority.md)）。上限在 `opspilot/investigation/limits.py` 中（例如每个 Run 100 次模型请求、2 小时墙钟时间）。
4. **工具。** 工具集取决于 `OPSPILOT_TOOL_PROFILE`（`opspilot/tools/profiles.py`）。默认的 `fixture` profile 注册一个只读工具 `metrics_range_query`，作用于固定样本（`opspilot/tools/fixture.py`）。`otel-demo` profile 注册五个：`metrics_range_query`、`get_metric_names`、`get_label_values`（Prometheus），`traces_search`（Jaeger），以及 `read_evidence`（读取 Run 已保存的超限结果的行切片）（`opspilot/tools/otel_demo.py`）。`otel-demo-ext` profile（`opspilot/tools/otel_demo_ext.py`）固定注册 19 个，每配置一个后端再加一组，最多再加 47 个，合计最多 66 个：
   - 固定（19）：五个 `otel-demo` 工具；Prometheus `execute_prometheus_instant_query`、`get_series`、`get_all_labels`、`list_prometheus_rules`、`get_prometheus_target`、`get_metric_metadata`；Jaeger `traces_search_by_tags`、`traces_query_trace_by_id`、`traces_list_services`、`traces_list_operations`；Kubernetes `kubernetes_jq_query`、`kubernetes_count`、`kubernetes_tabular_query`、`fetch_pod_logs`。
   - Elasticsearch/OpenSearch，`OPSPILOT_ES_URL`（8）：`elasticsearch_search`、`elasticsearch_mappings`、`elasticsearch_list_indices`、`elasticsearch_cat`、`elasticsearch_cluster_health`、`elasticsearch_index_stats`、`elasticsearch_allocation_explain`、`elasticsearch_nodes_stats`。
   - Loki，`OPSPILOT_LOKI_URL`（1）：`grafana_loki_query`。
   - VictoriaLogs，`OPSPILOT_VICTORIALOGS_URL`（5）：`victorialogs_query`、`victorialogs_streams`、`victorialogs_field_names`、`victorialogs_field_values`、`victorialogs_hits`。
   - Kubernetes 实时用量，`OPSPILOT_K8S_LIVE_METRICS=1`（2）：`kubectl_top_pods`、`kubectl_top_nodes`。
   - Helm，`OPSPILOT_HELM_RELEASES=1`（3）：`helm_list`、`helm_history`、`helm_status`。
   - Argo CD，`OPSPILOT_ARGOCD=1`（4）：`argocd_app_list`、`argocd_app_get`、`argocd_app_history`、`argocd_app_resources`。
   - 已批准的 runbook，`OPSPILOT_KNOWLEDGE_TOOL=1`（1）：`fetch_skill`。
   - SQL（PostgreSQL、MySQL/MariaDB、ClickHouse、SQL Server），`OPSPILOT_SQL_URL`（3）：`database_sql_query`、`database_sql_list_tables`、`database_sql_describe_table`。
   - Kafka，`OPSPILOT_KAFKA_BOOTSTRAP_SERVERS`（8）：`list_kafka_consumers`、`describe_consumer_group`、`list_topics`、`describe_topic`、`find_consumer_groups_by_topic`、`list_brokers`、`describe_configs`、`cluster_overview`。
   - MongoDB，`OPSPILOT_MONGODB_URL`（6）：`mongodb_find`、`mongodb_aggregate`、`mongodb_list_collections`、`mongodb_collection_schema`、`mongodb_server_status`、`mongodb_current_op`。
   - Redis/Valkey，`OPSPILOT_REDIS_URL`（6）：`redis_info`、`redis_config_get`、`redis_scan_keys`、`redis_key_info`、`redis_get_value`、`redis_slowlog`。

   `OPSPILOT_TODO_WRITE` 还会给模型一个 `TodoWrite` 任务清单，由循环自己应答；它不是数据源工具。所有注册的工具都经过 `ReadOnlyToolExecutor`（`opspilot/tools/executor.py`），由它负责目标范围、绝对时间窗、预算、结果大小上限、暂停检查和凭据脱敏。每个后端使用各自的只读凭据；SQL、MongoDB 和 Redis 拒绝连接 OpsPilot 自己的数据库地址。没有 shell 或写入工具。
5. **报告。** 最终报告把事实、假设和缺口分开，引用证据 id，并在发布前校验（校验失败的报告会带逐条反馈强制重试一次，`opspilot/investigation/reports.py`）。结论为 `supported`、`partial` 或 `inconclusive`；无法完成的 Run 带原因码交给人处理（[ADR-0005](../adr/0005-handoff-and-deadline-terminal.md)）。
6. **恢复观察。** 合格结论之后（或人登记了自己的修复之后），观察被授权。Observer 对目标的 HealthProfile（`opspilot/observer/profiles/otel-demo-checkout.json`）采样，只有在持续健康窗口之后才记录 `recovery_confirmed`；缺失的信号不能确认恢复。
7. **复盘与知识。** worker 起草复盘，其中每条论断都必须引用有效证据。只有人在工作台批准后，它才成为后续 Run 可用的知识；已批准的条目可以撤销。

发布观察沿用同样的模式，只是对象不同：一次版本变更事件会开启一个 `ReleaseObservation`；出现回归则成为新事故或关联到已有事故。OpsPilot 从不批准或阻止发布。

人工控制（全局或按目标暂停、取消、接手、关闭、重新打开、追问）都是会递增控制代数的条件更新；每次认领、预算预留、工具调度和结果提交都会检查它（C3 §4）。

## OpsPilot 自身的遥测

- 每个运行时的 Prometheus 指标（`opspilot_queue_depth`、`opspilot_model_calls_total`、`opspilot_model_consecutive_failures`、build info 等）和结构化 JSON 日志（`opspilot/logs.py`）。
- 可选的 trace 导出（`OPSPILOT_TRACE=outbox`）：worker 把 id、版本、token 数和固定代码写入 outbox 表，由一个线程投递到 LangSmith（[ADR-0006](../adr/0006-trace-evidence-and-backlog.md)）。默认关闭。

## 构建所依据的 lab

全部开发和测量都针对同一个目标：kind 集群上的 OpenTelemetry Demo（Helm chart `0.37.8`），连同它的 Prometheus、Jaeger、kube-state-metrics、一个 Alertmanager，以及 Prometheus 前面的只读代理，由 [scripts/kind_lab.py](../../scripts/kind_lab.py) 创建。流量来自 demo 自带的 load generator；故障是用 `kind_lab.py fault inject` 切换的 flagd feature flag（例如 `paymentFailure`）。见[限制](limitations.zh-CN.md)。

## 代码与 C3 的出入

- **没有独立的 Controller 和 Tool Gateway（C3 §3）。** worker 进程同时持有模型 key 和目标读取凭据，并在进程内运行工具执行器。C3 要求这些角色在凭据、数据库权限和网络可达性上相互隔离；目前只有数据库权限按运行时分开。已作为已知限制关闭（[#197](https://github.com/kevinWangSheng/production-ops-agent/issues/197)，C3 §3）。
- **没有 TLS 代理（C3 §9）。** C3 规定在 Basic Auth 工作台前放置 Caddy TLS。Compose 安装只在 `127.0.0.1` 上发布工作台，远程访问经 SSH 隧道；不附带 TLS 代理。
