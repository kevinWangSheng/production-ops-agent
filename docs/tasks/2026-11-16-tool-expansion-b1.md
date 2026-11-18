# 工具面拓展 B1：Elasticsearch/OpenSearch 日志检索

- 状态：实施中（实现已推送草稿 PR #705；独立合同测试、真实后端集成测试与 CI job 由测试作者另写，待合入；独立审查未做）
- 更新日期：2026-11-18
- 依据：[B/C 批次记录](2026-11-16-tool-expansion-bc.md)、[决定表](2026-11-12-tool-expansion-contract.md) r1–r8 与 r9（本项 B1-U1、B1-U2、B1-1 至 B1-22）、C3 第 8、12 节；上游 HolmesGPT `a0009195` 的 `elasticsearch/data`（`elasticsearch.py`）。issue #560。预审全文在仓库外（`~/.agent/handoffs/opspilot-b1-prereview-2026-11-16.md`），对照 `main` `2f7ec758`。
- 工作区：worktree `../poa-560-es`，分支 `feature/560-es-logs`，起点 `origin/main` `09c1a68b`。

## 目标

调查能在 Elasticsearch 8 与 OpenSearch 2 里检索日志、查看索引与字段映射。工具只进扩展档位 `otel-demo-ext`，且只有配置了 ES 地址时才出现；`otel-demo` 档位与未配置 ES 的扩展档位字节不变。

## 行为（公开接口）

- 三个工具，名字照上游：`elasticsearch_search`（检索文档，可带聚合）、`elasticsearch_mappings`（某索引的字段映射）、`elasticsearch_list_indices`（列索引）。上游 `elasticsearch/cluster` 的 5 个集群管理工具不搬。
- 只读由工具保证：方法与路径写死，模型只提供校验过的索引名；只有 `POST /{index}/_search` 一条路径可以 POST。脚本、`runtime_mappings`、`wrapper`、`global` 聚合、`pit`、`scroll` 等写法在发请求前拒绝。只读 API key 是第二道防线。
- 时间（用户裁决 B1-U1）：索引映射里有时间字段时，工具一律按事故时间窗过滤，模型没有关闭的选项；只有映射里根本没有时间字段的索引不加过滤，结果的来源时间留空，`cannot_prove` 写明不能证明属于事故时间窗。
- 显示（用户裁决 B1-U2）：只有设置了 `OPSPILOT_ES_URL` 时，三个工具才进入扩展档位、`data_sources.available` 才列出 `elasticsearch`、`tool_schema_revision` 才随之变化；没设时扩展档位字节不变。B/C 其余 8 个工具沿用同一规则。
- 大小与截断：每次最多 500 条；原始读取 2 MiB、视图 25k token，超限整条拒绝并提示缩小；单个字符串值截到 2000 字符并标出；`total` 大于命中数、超时、分片失败都标「不完整」并写原因。
- 凭据：独立凭据槽 `es-ro`（`OPSPILOT_ES_*`），API key 与用户名密码二选一，不与其他后端互相回退；凭据不进视图、备注与 trace。
- 错误分类：参数或 DSL 错误、索引不存在、无数据、无权限、来源不可用、超时彼此区分。

参数、路径白名单、DSL 禁用清单、投影字段、错误码等细节以决定表 r9 的 B1-1 至 B1-15 为准。

## 这个 PR 做什么

- 新模块与传输、三个工具与模型可见描述、扩展档位按配置显示、lab 闸门加 ES 地址检查、入口脚本放行 `OPSPILOT_ES_*`；在 B/C 记录登记数据源（返回什么、发往模型、PG、LangSmith）。
- 合同测试（B1-14 清单）由未参与实现的测试作者按本记录、决定表与 C3 编写，不看实现代码；本机用 `http.server` 替身。
- 接真实后端的集成测试（R8-25，B1-16 至 B1-22）：CI 新 job `search-backends`，ES 8 与 OpenSearch 2 服务容器（钉 digest），灌合成数据，断言真实返回、时间过滤、截断、错误分类与两层只读拒绝；不调模型。
- 交给 #671 冒烟的东西：环境变量清单、创建只读 key/用户的脚本（写 600 权限文件、不回显）、冒烟证据要求（见决定表 r9「R8-26」行）。

## 开工前检查

- 全量验收探针未跑（预审时本机 swap 13.0/14.3 GB）：开工后先开草稿 PR 让 CI 跑 `tests/acceptance`，清单外的失败追加进决定表。预期 0 失败、A7 钉的扩展档位 revision 不变（推断，未核实）。
- 镜像具体版本开工时查当前最新补丁并钉 digest。

## 实现决定

r9 没定死、由实现者按 A 批先例取可逆项（每行一条）：

- 模块 `opspilot/tools/elasticsearch_logs.py`，`ElasticsearchTransport` 按工具名路由；扩展档位加 `ES_TOOL_SCHEMAS`、`ES_TOOL_SCHEMA_REVISION`，原有 `TOOL_SCHEMAS`、`TOOL_SCHEMA_REVISION`、`TOOL_NAMES` 不变；`otel_demo_ext_face`/`otel_demo_ext_versions` 加关键字参数 `elasticsearch`，默认 False 即今天的字节。web 与 worker 都只看 `OPSPILOT_ES_URL` 是否非空来选工具面。
- 拒绝码怎么让模型看到（B1-6「notes 里写固定码」）：结果类仍是 `INVALID_PARAMS`；执行器原先的拒绝视图不带适配器字段，改为按需带：源状态响应的 `view_fields` 里有 `notes` 字符串列表时，拒绝视图加 `notes`（脱敏、每条截到 1024 字符、最多 4 条）。其他工具的拒绝响应不设 `view_fields`，视图不变。`notes[0]` 以固定码开头，如 `QUERY_CONSTRUCT_NOT_ALLOWED: ...`。
- 固定码清单：发请求前拒绝的 `INVALID_INDEX`、`INVALID_SIZE`、`INVALID_FROM`、`INVALID_TIME_FIELD`、`INVALID_JSON`（JSON 解析失败或类型不对）、`QUERY_CONSTRUCT_NOT_ALLOWED`、`AGGREGATION_NAME_RESERVED`、`QUERY_OUT_OF_WINDOW`、`INVALID_COLUMNS`、`INVALID_SORT`、`INVALID_HEALTH`、`INVALID_BYTES`、`INVALID_EXPAND_WILDCARDS`、`INVALID_JQ`；读过映射后拒绝的 `TIME_FIELD_NOT_FOUND`、`TIME_FIELD_REQUIRED`；jq 的 `JQ_COMPILE_ERROR`、`JQ_RUNTIME_ERROR`（`INVALID_PARAMS`）与 `JQ_FAILED`（`SOURCE_ERROR`）。
- `INDEX_NOT_FOUND`：ES 真返回 404 `index_not_found_exception`，走现有 `source_error` 对象（`http_status` 404、`error_type`、文本以 `INDEX_NOT_FOUND:` 开头）。其他 400/404 回显 `error.type` 和 `reason`（加 `root_cause[0]`），由执行器脱敏截短。
- 时间字段判定：search 先 `GET /{index}/_mapping`，把 `properties` 递归展开成点号路径，类型为 `date`/`date_nanos` 的算时间字段。没给 `time_field` 且匹配索引都没有 `@timestamp` 但有别的日期字段时，拒绝 `TIME_FIELD_REQUIRED` 并列出日期字段（不擅自挑一个，也不放过过滤）；给了 `time_field` 但没有任何索引把它映射成日期时，拒绝 `TIME_FIELD_NOT_FOUND`；所有匹配索引都没有日期字段时才不加过滤（给了 `time_field` 则在 notes 里写明被忽略）。
- 混合索引（模式同时匹配有、无该时间字段的索引）：过滤照加，无该字段的索引文档进不了结果，notes 列出这些索引名并提示单独查。比「无字段索引不过滤」更严，取 fail-closed。
- 时间过滤写法：`bool.filter` 里放 `range`（`gte`/`lte` 为查询窗 ISO 时间，`format: strict_date_optional_time`，避免自定义日期格式解析失败），模型的 `query` 放进 `bool.must`；隐藏聚合名 `opspilot_source_time_min`/`max`，模型的聚合重名时拒绝 `AGGREGATION_NAME_RESERVED`；来源时间夹在查询窗内防舍入。
- 三个工具的 `result_path` 统一为 `results`。search 在 `size>0` 时行是文档（`row_unit` `documents`），聚合放视图字段 `aggregations`；`size=0` 时只有一行 `{total, aggregations}`（`row_unit` `summaries`），让只计数或只聚合的查询是 `ok`、可引用（否则执行器按 0 行判 `no_data`）。mappings 不带 jq 时每个索引一行 `{index, mappings}`（`indices`），list_indices 每个索引一行（`indices`）；带 jq 时行是 jq 输出（`results`），jq 作用在 ES 原始 JSON 上，与上游 `filter_result` 一致。
- 不完整：`size>0` 且 `total.value` 大于显示条数（含 `from_offset` 跳过的），或 `relation` 不是 `eq`；`timed_out`；`_shards.failed>0`。理由写在 `incomplete_reason`。
- 长值截断：命中行与聚合里所有字符串值截到 2000 字符；每行 `_truncated_fields` 列出被截的点号路径，视图 `long_values_cut` 计数并写 notes。
- 通配符没匹配到索引（映射返回 `{}`）时不发 search，直接返回 0 行（`no_data`）并在 notes 说明；`size=0` 也一样。search 已发出但 ES 回 `_shards.total == 0`（映射读后索引消失）时同样 0 行、`no_data`。只有实际搜过至少一个索引时，`size=0` 才返回那一行 `ok` 的 `{total, aggregations}`（`total` 可以是 0）。
- 超时：注册 30 s；映射读取与 search 共用一个期限，请求体 `timeout` 取 search 发出前剩余整秒（至少 1 s）。原始读取映射与 search 各自 2 MiB 上限。
- 认证与 TLS：`es-ro` 槽存完整 `Authorization` 值（`ApiKey <encoded>` 或 `Basic ...`）；HTTPS 未设 `OPSPILOT_ES_CA_FILE` 时用系统根证书校验（不关校验），设了只信该 CA；http 地址配 CA 文件报 `ES_CONFIG_INVALID`。用户名密码只给一半、地址带路径/查询串/userinfo 也是 `ES_CONFIG_INVALID`。
- list_indices 的 `columns`/`sort` 列白名单照上游（`index`、`health`、`status`、`pri`、`rep`、`docs.count`、`docs.deleted`、`store.size`、`pri.store.size`、`creation.date`、`creation.date.string`），`sort` 可带 `:asc`/`:desc`；`expand_wildcards` 允许逗号组合。
- 索引名校验另拒绝空段和第二个前导 `-`，整个参数最长 1000 字符。
- Compose 未接 `OPSPILOT_ES_URL`：入口脚本已放行 `OPSPILOT_ES_*`，但 Compose 的 web 拿不到 worker_env 里的地址，两边版本会不一致（Run 在认领时被拦）。#671 与租机用宿主进程拓扑，两进程同一环境，不受影响；Compose 要用 ES 时另开 issue。
- `profile=true` 时 ES 的 profile 原样放进视图字段 `profile`（上游原样返回）。

## 交给 #671 短租冒烟

- worker 与 web 环境：`OPSPILOT_TOOL_PROFILE=otel-demo-ext`；两进程都设 `OPSPILOT_ES_URL=http://127.0.0.1:<端口>`（web 只读地址，用来选工具面；只有一边设会因版本不一致拦住 Run）。worker 另设 `OPSPILOT_ES_API_KEY`（OpenSearch 用 `OPSPILOT_ES_USERNAME`/`OPSPILOT_ES_PASSWORD`），HTTPS 时 `OPSPILOT_ES_CA_FILE`。只设凭据不设地址、两种凭据都设、都不设，worker 启动即退出（`ES_CONFIG_INVALID`/`ES_CONFIG_MISSING`）。扩展档位仍需 `OPSPILOT_K8S_*` 与 Prometheus/Jaeger 地址。lab 闸门要求 ES 地址是回环、`localhost` 或 `*.svc`，否则 `ELASTICSEARCH_NOT_LAB`。
- 只读身份：`scripts/es_readonly_identity.py --backend elasticsearch|opensearch`。工程师凭据只从环境读（`ELASTICSEARCH_URL` 加 `ELASTICSEARCH_API_KEY`，或 `ELASTICSEARCH_USERNAME`/`ELASTICSEARCH_PASSWORD`，与上游 `before_test` 同名）；ES 建 B1-5 的 API key（默认 7 天，`--expiration` 可改），OpenSearch 建 `readall_and_monitor` 内部用户（随机口令）；把 `OPSPILOT_ES_*` 写进 `tmp/m1-kind-lab/es-readonly.env`（600，`--out` 可改），只打印 key id 或用户名，不回显秘密。OpenSearch 的角色映射写法（`opendistro_security_roles`）未实测，冒烟时核实。
- 冒烟证据：至少一次真实 Run 调用了 `elasticsearch_*`，已发布报告引用其证据；附 LangSmith trace 链接与冻结的 `summary.json`（lab 闸门通过，项目名以 `opspilot-lab-` 开头）；OpenSearch 跑 2 题，记录里单列 OpenSearch 只读用户的直接写入被拒（B1-19）。

## 不做

- 集群管理类工具；按需工具搜索；OpenSearch 只读角色的实测（留到 #671）。
- 评测题的时间戳平移（属 #666，已在 #666 记录）。

## 待决

无。
