# 工具面拓展 C3：Kafka 只读（主题、消费组与滞后、broker）

- 状态：实现中（C6a 已合并，`origin/main` `0c0492f1` 已 merge 入本分支）；独立测试作者 tests-567 的合同与集成测试已 cherry-pick；Draft PR 待 CI
- 更新日期：2026-11-20
- 依据：issue #567；[B/C 批次记录](2026-11-16-tool-expansion-bc.md)；[决定表](2026-11-12-tool-expansion-contract.md) r8（R8-7、R8-9、R8-20、R8-25、R8-27）与 r11（C3-U1 = a、C3-U2 = a、C3-1 至 C3-14 可逆默认全部采纳）；C3 第 8、12 节。预审全文在仓库外：`~/.agent/handoffs/opspilot-c3-prereview-2026-11-19.md`（细节以它和 r11 为准）。上游 HolmesGPT `a0009195` `holmes/plugins/toolsets/kafka.py`（`kafka/admin`）
- 工作区：`../poa-567-kafka`，分支 `feature/567-kafka`，起点 `origin/main` `d28b4631`

## 目标

调查能看到 Kafka 的主题与分区、broker、消费组成员与按分区的消费滞后，不读消息内容。工具只进扩展档位 `otel-demo-ext`，且只在设了 `OPSPILOT_KAFKA_BOOTSTRAP_SERVERS` 时出现；`otel-demo` 与扩展档位所有现有 revision 字节不变。完成标志（C3-U2 = a）：自建场景里网页问「订单主题的消费滞后是多少」，报告引用的证据含总滞后数与组名 `order-processor`。

## 依赖（C3-1，依赖变更）

`confluent-kafka==2.16.1` 加进 `m0`（Dockerfile 只装 `m0`+`web`），`uv.lock` 随之更新。2026-11-20 核对 PyPI：2.16.1 发布于 2026-10-28（满三周；2.16.2 是 11-18 发的，不用）；许可证 Apache-2.0（分类器），内置 librdkafka 为 BSD-2；cp312 有 `macosx_11_0_arm64`、`manylinux_2_28_x86_64`/`aarch64` wheel（镜像 `python:3.12.13-slim` 的 glibc 高于 2.28，不需要 apt 包）；必需依赖只有 `typing-extensions; python_version < "3.11"`（本项目 3.12 不装），其余都在 extras 里，与现有依赖无交集【核实 PyPI 元数据；实际 `uv lock` 解析在实现时做】。

## 工具（C3-2，照上游 8 个，名字不改）

去掉上游所有工具的 `kafka_cluster_name`：一个部署只连一个集群，运维配置，模型不选目标。不搬 `consume_messages`、`list_kafka_clusters`。

| 工具 | 参数（必填加粗） | 只读调用 | `results` 每行 | `row_unit` | verb |
|---|---|---|---|---|---|
| `list_kafka_consumers` | 无 | `list_consumer_groups` | `{group_id, state, type, is_simple}`，按 `group_id` 排序 | `groups` | `list` |
| `describe_consumer_group` | **`group_id`**、`include_offsets`（默认 **true**，与上游不同） | `describe_consumer_groups`；`include_offsets` 时再 `list_consumer_group_offsets` 与 `list_offsets` | 一行组：`{group_id, state, type, coordinator, partition_assignor, members: [{member_id, client_id, host, group_instance_id, assignment: [{topic, partition}]}], partitions: [滞后行], lag_by_topic: {topic: 总滞后}}` | `groups` | `get` |
| `list_topics` | 无 | `list_topics()`（**不带主题参数**） | `{topic, partitions, internal}`，按名字排序，内部主题保留并标 `internal` | `topics` | `list` |
| `describe_topic` | **`topic_name`**、`fetch_configuration`（默认 false） | `describe_topics`（按名查，不用 `list_topics(topic)`）；`fetch_configuration` 时再 `describe_configs` | 一行：`{topic, topic_id, internal, partitions: [{partition, leader, replicas, isr, under_replicated, offline}], configs?}` | `topics` | `get` |
| `find_consumer_groups_by_topic` | **`topic_name`** | 先 `describe_topics` 确认主题存在；`list_consumer_groups` 后按组 `describe_consumer_groups` 与 `list_consumer_group_offsets`；`list_offsets` | `{group_id, state, member_count, assigned_partitions, committed_partitions, total_lag}`，按 `group_id` 排序（多一个 `total_lag`，与上游不同） | `groups` | `get` |
| `list_brokers` | 无 | `describe_cluster` | `{id, host, port, rack, is_controller}`，按 `id` 排序；视图另带 `cluster_id` | `brokers` | `list` |
| `describe_configs` | **`resource_type`**（`topic`/`broker`）、**`resource_name`** | `describe_configs` | `{name, value, source, is_default, is_read_only, is_sensitive}`，按 `name` 排序；`is_sensitive` 的只留名字，`value` 为 `null` | `configs` | `get` |
| `cluster_overview` | 无 | `describe_cluster`、`list_topics()` | 一行：`{cluster_id, controller_id, broker_count, topic_count, partition_count, under_replicated_partitions, offline_partitions, offline_broker_ids}`（口径同上游：ISR 少于副本数为副本不足，leader 为 -1 为离线，副本里出现但不在活跃 broker 列表的 ID 为离线 broker） | `summary` | `get` |

- 滞后行（C3-7）：`{topic, partition, committed_offset, end_offset, log_start_offset, lag, flags}`。`lag = end_offset - committed_offset`（条数）；分区集合 = 成员分配到的分区 ∪ 有提交位点的分区；没有提交位点 `lag=null`、`flags` 含 `no_committed_offset`；`committed > end` 时 `lag=0`、标 `committed_ahead_of_end`；`committed < log_start` 时标 `committed_below_log_start`。`end_offset` 用 `OffsetSpec.latest()`、`log_start_offset` 用 `OffsetSpec.earliest()`，`isolation_level` read_uncommitted。**提交位点上的 `metadata` 字符串不投影**。`lag_by_topic` 与 `total_lag` 只加非空的 `lag`。
- 时间（预审类别 1）：当前状态类，来源时间留空，视图带 `observed_at`（查询时刻）。工具描述的 `cannot_prove`（C3-11）：不能证明事故时间窗内的滞后；不能说明消息内容或积压原因；提交位点与末尾位点不是同一瞬间读的。只改工具描述，不改调查提示词。
- 参数校验（C3-8，发请求前拒绝，`INVALID_PARAMS`，码放 notes 开头）：`INVALID_TOPIC`（不是 `[A-Za-z0-9._-]{1,249}`）、`INVALID_GROUP`（空、超过 255 字符或含不可打印字符）、`INVALID_RESOURCE`（`resource_type` 不在两项里，broker 的 `resource_name` 不是非负十进制整数）、布尔参数类型错。
- 上限（C3-8，lead 2026-11-20 裁决修订）：`list_topics` 最多 1000 行、`list_kafka_consumers` 最多 800 行（排序后取前 N，标不完整）；每个主题或组最多 500 个分区行（同）。按实测每行 token 数定，避免满上限仍被 25k 视图上限整条拒绝；`find_consumer_groups_by_topic` 最多扫描 200 个组（按名字排序取前 200，标不完整并写扫了多少、共多少）；所有字符串值截到 2000 字符；原始结果 2 MiB、视图 25k token，超限整条 `RESULT_TOO_LARGE`。`may_contain_secrets=True`，`source="otel-demo"`，挂在现有 target 下，引导地址放 selector `kafka_bootstrap`，`result_path` 为 `results`。
- 错误分类（C3-10）：`_TRANSPORT`、`_ALL_BROKERS_DOWN`、`_RESOLVE`、`NOT_COORDINATOR`、`COORDINATOR_NOT_AVAILABLE`、`COORDINATOR_LOAD_IN_PROGRESS` → `SOURCE_UNAVAILABLE`；`_TIMED_OUT`、`REQUEST_TIMED_OUT` 或 30 秒期限到 → 超时（沿用 C5 的超时分类）；`*_AUTHORIZATION_FAILED`、`SASL_AUTHENTICATION_FAILED`、`_AUTHENTICATION` → `SOURCE_FORBIDDEN`（只带码，不回显）；`UNKNOWN_TOPIC_OR_PART`、`UNKNOWN_TOPIC_ID` → `no_data`，notes `TOPIC_NOT_FOUND`；`GROUP_ID_NOT_FOUND` 或组状态 `DEAD` → `no_data`，notes `GROUP_NOT_FOUND`；`INVALID_*` → `INVALID_PARAMS`；其余 → `SOURCE_ERROR`，回显截短、脱敏后的错误名与说明。不重试。

## 只读保证

- 第一层，工具（C3-3，SREGym 题里唯一的一层）：产品代码只调 `AdminClient` 的 8 个方法：`list_topics`、`describe_topics`、`describe_cluster`、`list_consumer_groups`、`describe_consumer_groups`、`list_consumer_group_offsets`、`list_offsets`、`describe_configs`。这些都不返回消息内容。不照抄上游以目标组 `group.id` 新建 `Consumer` 算滞后的写法（上游 `kafka.py:520-532`、`:781-803`），整个产品没有消费者或生产者对象。
- 「不读消息内容」与「不写」的确定性检查：
  1. ruff `banned-api`（TID251，已有）加 `confluent_kafka.Consumer`、`confluent_kafka.Producer`、`confluent_kafka.DeserializingConsumer`、`confluent_kafka.SerializingProducer`、`confluent_kafka.aio`；`tests/**`、`scripts/**` 已整体豁免，夹具与场景脚本可以写数据。
  2. AST 合同测试：`opspilot/` 下只有 `opspilot/tools/kafka_admin.py` import `confluent_kafka`，且只 import `confluent_kafka.admin` 的客户端与请求类型、`KafkaError`/`KafkaException`、`TopicPartition`、`TopicCollection`、`ConsumerGroupTopicPartitions`、`OffsetSpec`、`ConfigResource`、`ResourceType`、`IsolationLevel`；对客户端对象的方法调用只在上述 8 个名字里；`list_topics` 调用不带位置参数和 `topic=` 关键字（按名查元数据会触发自动建主题，见下）。
  3. 客户端配置写死 `allow.auto.create.topics=false`。
- 自动建主题防护（最高风险）：模型查拼错的主题名，不得在被调查的集群里建出主题。按名字查主题只用 `describe_topics`（不发带主题名的元数据请求），并显式关客户端侧自动建主题；`find_consumer_groups_by_topic` 先用 `describe_topics` 确认主题存在，不存在直接 `no_data`/`TOPIC_NOT_FOUND`。CI 在 broker 打开 `auto.create.topics.enable=true` 的无认证监听上实测（断言见下）。
- 第二层，身份（C3-5，只在有认证的环境：CI、自建场景）：SASL 账号，broker 开 `StandardAuthorizer`、`allow.everyone.if.no.acl.found=false`；只读账号 ACL：Topic `*` 的 `DESCRIBE`、`DESCRIBE_CONFIGS`，Group `*` 的 `DESCRIBE`，Cluster 的 `DESCRIBE`、`DESCRIBE_CONFIGS`。**不给 Topic `READ`**（拉消息需要它，代码出错也读不到内容），不给 WRITE、CREATE、DELETE、ALTER、ALTER_CONFIGS、IDEMPOTENT_WRITE。各接口实际所需权限在 CI 实测（尤其 `list_consumer_group_offsets` 是否要 Group `READ`；若要，记录实测并作为新问题报 lead，不自行扩权）。
- SREGym 题（C3-U1 = a）：broker 无认证，匿名连，只读只由第一层保证；结果页照实披露「该题 Kafka 无认证，只读由工具保证」。

## 配置与凭据（C3-4）

- `OPSPILOT_KAFKA_BOOTSTRAP_SERVERS`：逗号分隔的 `host:port`，不带协议前缀，端口 1–65535，最多 10 项；web 与 worker 都只看它是否设置来决定工具面（同 B1）。
- `OPSPILOT_KAFKA_SECURITY_PROTOCOL`：`PLAINTEXT`（默认）、`SSL`、`SASL_PLAINTEXT`、`SASL_SSL`；`OPSPILOT_KAFKA_SASL_MECHANISM`：`PLAIN`、`SCRAM-SHA-256`、`SCRAM-SHA-512`（`SASL_*` 时默认 `PLAIN`）；`OPSPILOT_KAFKA_USERNAME`、`OPSPILOT_KAFKA_PASSWORD`；`OPSPILOT_KAFKA_CA_FILE`（只在 `SSL`/`SASL_SSL`）。不做 mTLS 客户端证书。
- 启动码（加进 `opspilot/tools/profiles.py` 的启动码集合）：`KAFKA_CONFIG_MISSING`（协议是 `SASL_*` 却缺用户名或口令）；`KAFKA_CONFIG_INVALID`（地址格式错、带协议前缀；协议或机制取值不合法；非 `SASL_*` 却设了用户名或口令或机制；非 TLS 却设了 CA；没设地址却设了任何其他 `OPSPILOT_KAFKA_*`）。凭据**可选**：匿名（`PLAINTEXT` 不带凭据）是合法配置，对应 SREGym 题。
- 凭据槽 `kafka-ro`，与其他槽不互相回退（R3）；只有 worker 读凭据，web 不读；口令变量以 `_PASSWORD` 结尾，trace 侧按后缀擦除；口令不进视图、notes、selector 与 trace。`deploy/docker-entrypoint.sh` 放行 `OPSPILOT_KAFKA_*`。
- lab 闸门（C3-6）：设了地址时，每个 `host:port` 拼成 `kafka://host:port` 交 `_is_lab_host`，有一个不是回环、`localhost` 或 `*.svc` 就报 `KAFKA_NOT_LAB`。已知限制：客户端拿到元数据后改连 broker 通告的地址，闸门只能查引导地址；`list_brokers` 返回的通告地址在证据里可见。
- 客户端生命周期（C3-9）：每次工具调用 `with AdminClient(conf)` 建短命客户端，`client.id=opspilot-ro`；每个请求传 `request_timeout=剩余秒数`，`future.result(timeout=剩余秒数)`，`socket.connection.setup.timeout.ms=10000`；librdkafka 日志接 Python logging，不进证据。客户端经可注入的工厂创建：`KafkaTransport(config, *, client_factory=None, request_timeout_seconds=30.0)`，`client_factory(conf: dict[str, str]) -> 管理客户端`；合同测试用替身工厂记录收到的配置与每次方法调用。`confluent_kafka` 只在 transport 里延迟 import，web 计算工具面不加载 librdkafka。

## 模块与扩展档位（C3-12）

- 新模块 `opspilot/tools/kafka_admin.py`：`KafkaConfig`（`from_env(env)`，错误抛带上述启动码的异常）、`KafkaTransport`、`TOOL_SCHEMAS`、`TOOL_NAMES`（8 个）、`kafka_registrations(max_window_seconds)`、`ENDPOINT_SELECTOR = "kafka_bootstrap"`。
- `otel_demo_ext.py`：按 C6a 合并后的写法追加参数 `kafka: bool = False`，8 个工具接在当时所有后端之后（C6a 的 SQL 之后），`data_sources.available.kafka` 为 8 个名字排序；`tool_schema_revision(..., kafka=False)` 时所有现有值字节不变；新常量 `KAFKA_TOOL_SCHEMA_REVISION`、`KAFKA_TOOL_NAMES`；含 Kafka 的值实现后实测写进本记录。worker 把 8 个工具路由到自己的 `KafkaTransport`（`kafka-ro` 槽）；web 与 worker 对是否设地址不一致时，Run 在认领时被拦（同 B1/C5）。一个 PR（C3-13）。

## 合同测试（C3-14，未参与实现的 Agent 按本记录写，替身工厂，本机跑）

① AST 与 ruff 禁用项（见「只读保证」）；② 客户端配置含 `allow.auto.create.topics=false`，按名查主题走 `describe_topics`，`list_topics` 不带主题；③ 滞后口径：四种标记、`lag_by_topic`、`total_lag`、`metadata` 不出现；④ 参数校验在发请求前拒绝（替身零调用）；⑤ 上限与不完整标记（1000/800 行、500 分区行、200 组、2000 字符、2 MiB、25k）；⑥ 敏感配置值不出工具；⑦ 错误码映射表；⑧ `kafka-ro` 不与其他槽回退，口令不进视图、notes、trace；⑨ 启动码与 web 不读凭据；⑩ `otel-demo` 字节不变，未设地址时扩展档位已有组合 revision 字节不变；⑪ lab 闸门（多个引导地址有一个不合规即拒）。

## 接真实 Kafka 的 CI 集成测试（R8-25，C3-15 至 C3-20）

- 新 job `kafka-backend`（`ubuntu-24.04`，`timeout-minutes: 15`），与 `search-backends`、`log-backends` 并行；`docker run` 起 broker（同 `log-backends` 写法），等 `kafka-broker-api-versions.sh` 成功再开测；测试 `OPSPILOT_KAFKA_BACKEND=1` 门控，本机跳过。新文件 `tests/integration/test_tool_expansion_c3_kafka.py`（测试作者）与 `_impl.py`（实现者）。
- 镜像 `apache/kafka:4.3.2@sha256:852a0d49fc4e656b022e7f23785411802c9a088c78a7c5e39f33969de9c227a7`（2026-10-20，amd64/arm64，2026-11-20 从 Docker Hub 读取）。不用 4.4.0（10-22 发布，已满两周，但是新次版本的 .0；4.3.2 是补丁版，预审默认），SREGym 题里的 broker 版本在 #669 冒烟时记录。单节点 KRaft，`KAFKA_HEAP_OPTS=-Xms384m -Xmx384m`、`--memory=768m`、`--cpus=1`。
- 监听：`SASL_PLAINTEXT://:9092`（有认证入口）、`NOAUTH://:9094`（PLAINTEXT，模拟 SREGym）、`CONTROLLER://:9093`；通告地址都是 `localhost`；超级用户 `User:admin;User:ANONYMOUS`；**`auto.create.topics.enable=true`**（故意打开）；JAAS 静态用户 `admin`、`opspilot`、`nobody`（无 ACL）；测试口令用明显的低熵串，进 gitleaks 允许名单，推送前跑 `make secrets-branch`。
- 合成数据（夹具用 `admin`）：`orders` 3 分区各 1000/800/600 条；`payments` 1 分区；1200 分区的大主题；`order-processor` 用 `alter_consumer_group_offsets` 设 400/800/100 → 滞后 600/0/500 共 1100，无成员（Empty）；`order-audit` 由测试线程里真实消费者追平（Stable，有成员）；`stale` 组提交后 `delete_records` 推高起始位点 → `committed_below_log_start`；某分区不提交 → `no_committed_offset`；210 个空组测 200 上限；`orders` 设非默认 `retention.ms`；给 `opspilot` 建上述 ACL。
- 断言：8 个工具在有认证入口都返回真实数据；`order-processor` 每分区与总滞后精确为 1100；`order-audit` 成员、分配与 0 滞后；三种标记；`find_consumer_groups_by_topic("orders")` 返回这些组与各自 `total_lag`，200 组上限生效；`list_brokers`/`cluster_overview` 的 broker 数、分区数、副本不足为 0；`describe_configs` 读到 `retention.ms`、敏感项无值；1000 行截断；错误分类用真实响应（`nobody` → `SOURCE_FORBIDDEN`，错口令 → `SOURCE_FORBIDDEN`，不可达端口 → `SOURCE_UNAVAILABLE` 或超时，不存在的主题与组 → `no_data`）。**两层只读**：工具层——在无认证入口（匿名即超级用户、broker 开着自动建主题）上 `describe_topic("does-not-exist")`、`describe_configs("topic","does-not-exist")`、`find_consumer_groups_by_topic("does-not-exist")` 之后，admin 看到的主题列表不变；全部工具跑完后主题数、各组提交位点、各分区末尾位点不变。身份层——用 `opspilot` 账号在测试代码里建普通客户端：生产 → `TOPIC_AUTHORIZATION_FAILED`；以某组消费 `orders` → 授权失败（拿这个身份读不到消息）；建主题、删主题、改配置、删组、改位点都被拒，数据不变；另记各只读接口所需 ACL 的实测。不调模型、不跑题。估计 job 约 3 分钟。

## 自建功能验收（C3-U2 = a，不计分，租机上跑 1 遍）

脚本 `scripts/kafka_scenario.py`（评测侧，`scripts/**` 可用生产者与消费者），用 CI 同款镜像与监听配置，容器 `opspilot-kafka-scenario` 只绑 `127.0.0.1`：

- `up --port 29592 --state-dir D`：起单节点 broker（SASL 监听通告 `localhost:29592`，开 authorizer），等就绪；随机生成 `admin`、`opspilot` 口令，存 `D/admin.env`（0600）。
- `seed --state-dir D --env-out F`：用 admin 建 ACL（同 CI）、主题 `orders`（3 分区，1000/800/600 条）、停住的 `order-processor`（位点 400/800/100，总滞后 1100，无成员）、追平的 `order-audit`；把产品要的 `OPSPILOT_KAFKA_*`（`SASL_PLAINTEXT`、`PLAIN`、`opspilot` 账号）写进 `F`（0600，不回显）；把期望 `{"group": "order-processor", "topic": "orders", "total_lag": 1100}` 写进 `D/expected.json`（评测侧，不进产品、不进题面）。
- `check --state-dir D`：只读核对期望仍成立（滞后未漂移），退出码 0/1。
- `down --state-dir D`：停并删本脚本起的容器（按容器名与 label 核对，不碰别的容器）。
- 判定：网页提问原话「订单主题的消费滞后是多少」；真实 Run 调用了 Kafka 工具，报告引用的证据里有 `1100` 与 `order-processor`；附 LangSmith trace 与冻结 `summary.json`；另记身份层拒绝的实测。与 SREGym 接法不同时跑，切换时 web 与 worker 一起重启（revision 只看地址是否设置，不变）。

## SREGym Kafka 题的连接（交 #667 驱动，另开 PR）

题目 `kafka_poison_pill_hol_block` 的 broker 是应用 namespace 里的 `kafka:9092`（SREGym 自己的管理脚本这样连、无认证，`sregym/service/kafka_health.py:131-134`）；broker 通告名【未确认，冒烟时用 `list_brokers` 看，按 OTel Demo 惯例推断是 `kafka:9092`】。客户端会改连通告地址，所以本地端口必须与通告端口相同，且通告名要在租机上解析到 127.0.0.1。驱动侧要改的（`scripts/sregym_eval/`，本 PR 不改）：

1. `ports.py`：加 `kafka: int = 9092`（必须等于通告端口，不能随意挪），进 `per_trial()` 的空闲检查。
2. `agent_entry.py`：在 `/get_app` 的应用 namespace（不是 `observe`）加一个 port-forward `svc/kafka 127.0.0.1:9092:9092`（工程师 kubeconfig，同现有 `FORWARDS` 的起法）；起不来时地址改指 `unreachable`、记 `observability["kafka"] = "missing"`。web 与 worker 都设 `OPSPILOT_KAFKA_BOOTSTRAP_SERVERS=127.0.0.1:9092`，`PLAINTEXT`，不设凭据；只读身份检查（`check_product_env`）照常过。
3. `driver.py` `prepare`：核对租机 `/etc/hosts` 有把通告名（先按 `kafka`，冒烟后按实测改）指向 `127.0.0.1` 的一行，缺了报 `KAFKA_HOSTS_ENTRY_MISSING` 并停在 prepare，不由脚本改系统文件（租机手册加这一行）。
4. summary：`sregym_eval.data_plane.kafka` 记引导地址、协议、`auth: "none"`；结果页披露码 `KAFKA_NO_AUTH_TOOL_LAYER_ONLY`（「该题 Kafka 无认证，只读由工具保证」）。
5. 答案隔离照旧：只读题面与环境元数据，不读 `inject_kafka.py`、`rca_checklists.yaml` 与 oracle。注意 SREGym 的健康检查会自己建删 `healthchecks-*` 主题，题目环境里主题数会变，所以「主题数不变」只在 CI 断言，不在 SREGym 题里断言。

## 新数据源登记（草稿，合并时写进 B/C 记录）

- C3（#567，只在 `otel-demo-ext` 且设了 `OPSPILOT_KAFKA_BOOTSTRAP_SERVERS`）：租机上 SREGym 题目自带的 Kafka（匿名 PLAINTEXT，经 port-forward，只读由工具保证）或自建场景 Kafka（凭据槽 `kafka-ro` 的 SASL 只读账号，ACL 只有 Describe 类）。只调 `AdminClient` 的 8 个管理接口：主题与分区元数据、broker 列表与集群描述、消费组列表与描述（成员 client_id、主机、分配）、提交位点（不含 metadata 字符串）、分区起止位点、主题与 broker 配置（敏感项去值）。不读消息内容：产品代码没有消费者，只读账号没有 Topic READ。返回上述元数据与按分区、按主题的滞后条数，最多 1000 行，字符串截到 2000 字符。`may_contain_secrets=True`：投影后的响应存入证据库（PG），视图经现有脱敏后进入 Run 所选模型；lab 内随 LangSmith trace 导出（lab 闸门要求引导地址是 lab 主机，`KAFKA_NOT_LAB`）。不新增接收方。

## 实现决定（实现者自定，可逆）

- `list_topics` 不带主题参数这一条进 AST 检查。理由：带主题名的元数据请求正是自动建主题的触发路径，只靠配置项不够确定。
- CI 镜像留 4.3.2，不换 4.4.0。理由见 CI 节。
- `describe_topic` 保留上游的 `fetch_configuration`（默认 false），走 `describe_configs`。理由：照上游参数面。
- `find_consumer_groups_by_topic` 先确认主题存在。理由：不存在时不扫组，直接给出 `TOPIC_NOT_FOUND` 这个事实。

- `list_offsets` 里任何分区级错误（`UNKNOWN_TOPIC_OR_PART`、`LEADER_NOT_AVAILABLE`、`NOT_LEADER_FOR_PARTITION` 等）只让该分区的起止位点为空：`describe_consumer_group` 该行 `lag` 为空、标 `end_offset_unavailable`，`find_consumer_groups_by_topic` 的 `total_lag` 不计该分区，notes 写错误名与次数；调用级错误（超时、不可达、授权或登录失败、`_DESTROY_BROKER`）仍让整次调用失败。理由：分区离线正是典型事故，整条失败会让整个组看不了（独立审查阻塞项，PR #748，原实现只放过了「主题不存在」一种）。
- 已知限制（独立审查，不改）：消费组成员列表不设行数上限（靠 2 MiB/25k 整条拒绝兜底）；`find_consumer_groups_by_topic` 对扫描的每个组各发一次 `list_consumer_group_offsets`，最多 200 次串行往返，受 30 秒期限约束；扫描中任一组返回授权失败会让整次调用 `SOURCE_FORBIDDEN`。
- `_DESTROY_BROKER`（「Broker handle destroyed without termination」）时，同一客户端、同一期限内把本次调用的只读请求重跑一次，再失败报 `SOURCE_UNAVAILABLE`。理由：CI 实测（PR #748 第 7 轮）新建客户端发出的第一个请求会撞上 librdkafka 用首份元数据替换 bootstrap 句柄，`describe_topic` 与 `find_consumer_groups_by_topic` 偶发失败；这是客户端内部句柄被回收，不是 broker 拒绝，重跑只读请求无副作用。C3-10 的「不重试」对其余错误照旧（合同测试断言单次请求的错误码都不重试）。
- 登录失败（错口令）：librdkafka 不让请求失败，只经 `error_cb` 报 `_AUTHENTICATION` 并一直重试，请求最终超时（CI 实测）。客户端配置挂 `error_cb` 记下错误名；等待结果时每 0.5 秒 `poll(0)` 一次，看到登录失败即报 `SOURCE_FORBIDDEN`，不等到 30 秒超时；超时时也再查一次。理由：错口令是权限问题，报成超时会误导排查。
- 不带 Kafka 错误名的异常（例如客户端库自身的类型错误）归 `SOURCE_ERROR`，notes 只写异常类型名；worker 日志只记类型名，不记异常文本。理由：异常文本可能带请求内容。
- `coordinator` 投影为 broker ID（整数）。理由：host/port 已在 `list_brokers`。
- 扩展档位 revision（本分支实测）：仅 Kafka `otel-demo-ext-96b75ab08269`；SQL（postgresql）加 Kafka `otel-demo-ext-4ffb41544b2a`；不设 Kafka 地址时现有值不变（无后端 `otel-demo-ext-c63f058afb18`，合同测试钉的 8 个值全过）。
- CI `kafka-backend`：`CLUSTER_ID` 用 Kafka 快速入门的 22 字符示例值；两个集成文件分两步顺序跑，第二步在第一步失败时也跑；观测写 `$RUNNER_TEMP/kafka-probe.jsonl`（`KAFKA_IT_PROBE_OUT`），最后一步打印。

## 执行进展与证据

- 2026-11-20 本机（`.venv` 装了 confluent-kafka 2.16.1）：ruff、ruff format、mypy strict 干净；扩展档位、lab 闸门、入口脚本相关的 12 个现有测试文件 1833 passed、1 skipped；`uv lock` 只新增 confluent-kafka 一项。
- 独立合同测试 `tests/test_tool_expansion_c3_kafka_contract.py`：194 个中 179 过、15 失败，测试断言未改，已报 lead：
  1. 夹具 `Models.tp` 给真包的 `TopicPartition` 传 `metadata=None`，真包报 `TypeError`（9 个失败）；
  2. 夹具 `_sasl_env(**overrides)` 重复关键字参数（3 个失败）；
  3. 接口内部冲突（4 个失败）：1000 行上限与 25k token 视图上限放不下同一张表——1000 个组 26,432 token、1000 个 topic 分区行 38,470、1000 个滞后行 34,528（`list_topics` 1000 行放得下）。lead 裁决按工具定上限（800/500/500/1000），测试作者 `74438dfa` 修夹具并改数字。
  临时副本只修 1、2 两处夹具时 190 过，只剩 3 的 4 个。
- 2026-11-20 CI `kafka-backend`（PR #748，`apache/kafka:4.3.2`）：实现者 `_impl.py` 4 passed——只有 Describe 类 ACL（无 READ）的 `opspilot` 账号 8 个工具全部 ok（`list_consumer_group_offsets` 不需要 Group READ）；`impl-stopped` 组滞后精确为 40；匿名监听、broker 开着自动建主题时，随机拼错的名字经 `describe_topic`（含 `fetch_configuration`）、`find_consumer_groups_by_topic`、`describe_configs` 都是 `no_data`/`TOPIC_NOT_FOUND`，3 秒后主题列表里没有它；每次调用新建客户端约 0.06–0.9 秒，低于 C3-9 的 1 秒阈值，保持短命客户端。独立作者的集成文件在夹具阶段失败（新建主题后立即 `list_offsets` 得 `NOT_LEADER_FOR_PARTITION`，测试夹具未等分区 leader），已报 lead。
- CI 实测 broker 刚启动时 `list_consumer_groups` 偶发「1 个 broker 未应答」且 `valid` 为空，原实现会报 `no_data`（等于声称没有组）；已改为：一个组都没列出时按第一个错误分类，部分列出时标不完整并在 notes 写错误名。

## 下一步

等测试作者的集成夹具修复（等分区 leader、逐个检查提交结果的分区错误）；cherry-pick 后重跑 CI，CI 跑 `kafka-backend` 与 `tests/acceptance`；`list_consumer_group_offsets` 是否需要 Group READ 以 CI 观测为准，照实报。
