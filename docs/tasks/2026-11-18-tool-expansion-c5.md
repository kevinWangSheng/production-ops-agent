# 工具面拓展 C5：Loki / VictoriaLogs 日志检索

- 状态：C5a Loki 已合并（PR #724，`2d13f48a`，2026-11-18）；C5b VictoriaLogs 公开接口已定、实现中（分支 `feature/569-victorialogs`，worktree `../poa-569-vlogs`）；真实 Run 证据归租机（#671/#669）
- 更新日期：2026-11-19
- 依据：[B/C 批次记录](2026-11-16-tool-expansion-bc.md)、决定表 r8、r9（B1-U1、B1-U2 沿用）与 r10（C5-U1、R10-1、C5-0 至 C5-21）；C3 第 3、5、8、12 节；上游 HolmesGPT `a0009195` 的 `holmes/plugins/toolsets/grafana/`（Loki）与 `victorialogs/`。issue #569。预审全文在仓库外（`~/.agent/handoffs/opspilot-c5-prereview-2026-11-18.md`），细节以它和 r10 为准
- 工作区：C5a 实现 `../poa-569-loki`，分支 `feature/569-loki-logs`；C5a 测试 `../poa-569-loki-tests`，分支 `test/569-loki-logs`

## 目标

调查能在 Loki（C5a）与 VictoriaLogs（C5b）里检索日志。工具只进扩展档位 `otel-demo-ext`，且只有配置了对应地址时才出现；`otel-demo` 档位、未配置任何日志后端与仅配置 ES 的扩展档位字节不变。

## 行为（公开接口，C5a）

- 一个工具，名字照上游：`grafana_loki_query`，参数 `query`（LogQL，必填）、`start`、`end`（带时区的 RFC3339，两个都给或都不给；都不给取授权框末尾 1 小时）、`limit`（默认 100，最多 500）。
- 只读由工具保证：只发 GET，路径写死（`/loki/api/v1/query_range`），参数由工具拼进查询串；C5 不新增 POST。LogQL 里的 `offset`、超出查询窗的范围选择器、解析不了的时长在发请求前拒绝；求值起点后推，窗外的行不计入。第二道防线是只读网关（C5-U1）。
- 时间（B1-U1 沿用）：一律按事故时间窗过滤，模型不能关；越出授权框的请求不发。
- 显示（B1-U2 沿用）：只有设了 `OPSPILOT_LOKI_URL` 时工具才进入扩展档位、`data_sources.available` 才列出 `loki`、`tool_schema_revision` 才变；扩展档位按已配置后端集合计算（C5-14）。
- 凭据：凭据槽 `loki-ro`（`OPSPILOT_LOKI_URL`，Basic 用户名密码或 bearer token 二选一，`OPSPILOT_LOKI_CA_FILE`，租户 `OPSPILOT_LOKI_TENANT_ID` 设 `X-Scope-OrgID`），由运维配置，模型不选租户；不与其他后端互相回退；凭据不进视图、备注与 trace。lab 闸门加 `LOKI_NOT_LAB`。
- 大小与截断沿用 B1：原始读取 2 MiB、视图 25k token 超限整条拒绝；单值截到 2000 字符；到 limit 标不完整、保留最新的。
- 错误分类：参数或 LogQL 错误、无权限（含网关 403）、来源不可用、超时、无数据彼此区分（C5-11）。

## 行为（公开接口，C5b，2026-11-19）

- 五个工具，名字与参数照上游 `victorialogs.py`（C5-2）。`start`/`end` 全部同 Loki：带时区的 RFC3339，两个都给或都不给，都不给取默认查询窗（策略的 `default_query_window`，否则授权框末尾 1 小时），越出授权框报 `QUERY_OUT_OF_WINDOW` 且不发请求（C5-8）。

| 工具 | 参数（必填加粗） | 请求（只有 GET，路径写死，查询键固定） | 返回 `results` 每行 | `row_unit` | verb |
|---|---|---|---|---|---|
| `victorialogs_query` | **`query`**、`start`、`end`、`limit`（默认 100，1–500） | `/select/logsql/query`：`query`、`start`、`end`、`limit=<limit+1>` | VictoriaLogs 返回的整条 JSON 对象（`_time`、`_stream`、`_msg` 与其余字段），按 `_time` 降序，没有 `_time` 的行（如 `stats` 结果）按原序排在后面 | `entries` | `query` |
| `victorialogs_streams` | `query`（默认 `*`）、`start`、`end`、`limit`（默认 100，1–500） | `/select/logsql/streams`：`query`、`start`、`end`、`limit=<limit+1>` | `{stream, hits}`，按 `hits` 降序、再按 `stream` | `streams` | `get` |
| `victorialogs_field_names` | `query`（默认 `*`）、`start`、`end` | `/select/logsql/field_names`：`query`、`start`、`end` | `{field, hits}`，同上排序 | `fields` | `get` |
| `victorialogs_field_values` | **`field`**、`query`（默认 `*`）、`start`、`end`、`limit`（默认 100，1–500） | `/select/logsql/field_values`：`query`、`field`、`start`、`end`、`limit=<limit+1>` | `{value, hits}`，同上排序；视图带 `field` | `values` | `get` |
| `victorialogs_hits` | **`query`**、`start`、`end`、`step`（默认 `5m`）、`field` | `/select/logsql/hits`：`query`、`start`、`end`、`step`，给了 `field` 再加 `field` | 每个序列 `{fields, timestamps, values, total}`；`total` 为 0 的序列去掉 | `series` | `get` |

- 时间一律用带 `Z` 的 RFC3339 发出，模型去不掉（B1-U1）。来源时间：`victorialogs_query` 取返回行 `_time` 的最早与最晚（夹进查询窗；没有可解析的 `_time` 时取查询窗）；其余四个取查询窗本身（C5-9），`cannot_prove` 写明「只说明这个窗口里有这些流、字段、取值或计数，不能说明每一条日志的内容」。hits 的桶时间按 `step` 对齐，第一个桶的标记可能早于查询窗起点，但只计窗内的日志，视图 notes 写明。
- 截断：带 `limit` 的三个工具多要一条；返回超过 `limit` 时保留前 `limit` 行（query 为最新的），标不完整，原因写「已到 limit，窗口里还有更多没有返回」。所有字符串值截到 2000 字符，行内 `_truncated_fields` 列出。原始读取 2 MiB、视图 25k token，超限整条 `RESULT_TOO_LARGE`。`result_path` 为 `results`，`may_contain_secrets=True`，`source="otel-demo"`，地址在 selector `victorialogs_endpoint`。
- 发请求前的拒绝（码放 notes 开头，都归 `INVALID_PARAMS`）：`INVALID_QUERY`（必填的查询为空；超过 4000 字符；含换行、制表符或不可打印字符；引号未闭合）、`INVALID_LIMIT`、`INVALID_STEP`（`step` 不是 `<1–999999>(s|m|h)`，或查询窗按 `step` 分桶超过 1440 个）、`INVALID_FIELD`（`field` 不是 1–200 个 `[A-Za-z0-9_.:-]`）、`QUERY_OUT_OF_WINDOW`、`QUERY_CONSTRUCT_NOT_ALLOWED`（C5-7，见下）。
- LogsQL 扫描（C5-7，fail-closed）：字符串字面量是 `"…"`（反斜杠转义）与 `` `…` ``（无转义）；字面量之外出现 `'` 拒绝（与 Loki 相同，免得工具与 VictoriaLogs 对引号的理解不一致）；`#` 不当注释剥掉，整串都扫。字面量之外、不分大小写的整词 `options`、`join`、`union`、`stream_context` 拒绝；`in`、`contains_any`、`contains_all` 后跟括号、且括号里（字面量之外）出现 `|` 或 `:` 按子查询拒绝。查询里写 `_time:` 放行（只与 start/end 取交集）；`delete`、`rename` 等管道放行。要搜这些词本身，写成带引号的短语。
- 错误分类（C5-11）：400 报 `INVALID_PARAMS`，回显截短、脱敏的响应文本；401、403（含网关）报 `SOURCE_FORBIDDEN`，只带状态；404 报 `SOURCE_ERROR`；408、429、5xx 报 `SOURCE_UNAVAILABLE`；30 秒期限超时；连不上为来源不可用；成功但 0 行为 `no_data`。
- 配置（C5-4，worker 读）：`OPSPILOT_VICTORIALOGS_URL`；`_USERNAME` 加 `_PASSWORD` 或 `_BEARER_TOKEN` 二选一（必填）；`_CA_FILE`（只配 https）；`_ACCOUNT_ID`、`_PROJECT_ID`（可选，十进制无符号 32 位，分别设 `AccountID`、`ProjectID` 头，模型无租户参数，C5-0）。凭据槽 `vlogs-ro`，存完整 `Authorization` 值，不与其他槽回退。启动码 `VICTORIALOGS_CONFIG_MISSING`（有地址无凭据）、`VICTORIALOGS_CONFIG_INVALID`（两种凭据都配或半对、地址带 userinfo/查询串/片段或非 http(s)、无地址却配了其余变量、http 配 CA、租户值不合法）；地址可带路径前缀。web 只看地址是否设置。lab 闸门：设了地址就须是 lab 主机，否则 `VICTORIALOGS_NOT_LAB`。入口脚本放行 `OPSPILOT_VICTORIALOGS_*`。
- 扩展档位（C5-14）：后端固定顺序 Elasticsearch、Loki、VictoriaLogs；只有设了 `OPSPILOT_VICTORIALOGS_URL` 时五个工具加到工具数组末尾、`data_sources.available` 出现 `victorialogs`（五个名字排序）、revision 变化。`tool_schema_revision(elasticsearch=, loki=, victorialogs=)`；无后端 `c63f058afb18`、仅 ES `4f7faf728706`、仅 Loki `dd942a2f1292`、ES 加 Loki `f11fc2ce4d18` 四个现有值字节不变；含 VictoriaLogs 的四种组合的值实现后实测写进本记录。新常量 `VICTORIALOGS_TOOL_SCHEMA_REVISION`、`VICTORIALOGS_TOOL_NAMES`（仅 VictoriaLogs）。
- 只读网关（C5-U1 第二层）：`scripts/loki_readonly_gateway.py` 加 `--backend victorialogs`（默认 `loki` 行为与字节不变），只放行上表五条路径的 GET，丢请求体与客户端 `Authorization`，透传 `AccountID`/`ProjectID`，其余 403、未登录 401；租机 `start` 写 `OPSPILOT_VICTORIALOGS_*`，容器名与默认端口和 Loki 网关分开。
- CI（R8-25，C5-16 至 C5-20 的 VictoriaLogs 部分）：在现有 job `log-backends` 里加一个 `victoriametrics/victoria-logs` v1.x（镜像钉 digest）和网关的 VictoriaLogs 服务端口；新文件 `tests/integration/test_tool_expansion_c5b_victorialogs.py`，合成数据经 `/insert/jsonline` 直写后端（`_stream_fields`、`_msg_field`、`_time_field` 同 257 题），相对当前时间，含窗内、窗外、未来、5000 字符长行、`password=` 行、600 条截断用数据、两个租户。断言：五个工具都有真实返回；默认 1 小时窗与显式窗；窗外与未来的行不返回；框外与禁用构造的拒绝不发请求（网关访问日志核实）；到 limit 标不完整且保留最新；VictoriaLogs 的返回顺序与 `limit` 取舍（记录实测）；2 MiB 与视图超限；长值截断与脱敏；hits 的 `step`、分组与桶上限；400 语法错误、401 密码错、403 网关拒绝路径、后端不可达；租户隔离；两层只读：工具只发上述 GET，只读账号经网关 `POST /insert/jsonline`、`/insert/loki/api/v1/push`、`/internal/force_flush`、`/internal/force_merge` 与对允许路径的 POST 都 403，写入前后条数不变；子查询泄漏探针直连后端记录结果（C5-7 是否放开的证据，工具侧仍拒绝）。不调模型。

## 这个 PR（C5a）做什么

- 新模块 `opspilot/tools/loki_logs.py` 与传输、工具与模型可见描述（只改工具描述，不改调查提示词）、扩展档位按后端集合计算、lab 闸门、入口脚本放行 `OPSPILOT_LOKI_*`；在 B/C 记录登记数据源。
- 合同测试（C5-15 清单的 Loki 部分）由未参与实现的测试作者按本记录、r10 与预审编写，不看实现代码；本机用 `http.server` 替身。
- 接真实后端的集成测试（R8-25，C5-16 至 C5-20 的 Loki 部分）：CI 新 job `log-backends`，Loki 2.9.0 与最新 3.x、nginx 只读网关（C5-18），合成数据相对当前时间，断言真实返回、时间过滤、截断、错误分类与两层只读拒绝（经网关写入 403、数据条数不变）；不调模型。
- 交给 #671/#669：环境变量清单、网关配置与切换脚本（预审「交给 #671 / #669 租机冒烟」节）。

## 实现决定（lead 与实现者自定，可逆，2026-11-18）

- 设了 `OPSPILOT_LOKI_URL` 就必须配凭据（Basic 用户名密码或 bearer，二选一），否则启动报 `LOKI_CONFIG_MISSING`；两个都配报 `LOKI_CONFIG_INVALID`。产品的只读身份就是网关账号（C5-U1）。
- 地址可以带路径前缀（网关或 Grafana 数据源代理的基址），请求发往 `<前缀>/loki/api/v1/query_range`；带用户信息、查询串、片段的地址报 `LOKI_CONFIG_INVALID`；lab 闸门只看主机。
- C5a 不支持自定义请求头认证。
- 每次请求的查询键固定为 `query`、`start`、`end`、`limit`、`direction=backward`；时间用带 `Z` 的 RFC3339。指标查询（有范围选择器）另带 `step=<n>s`，`n = max(floor(R/250), 1)`，`R = 查询窗终点 − (查询窗起点 + 回看)` 的秒数，在取整之前算；推后的起点向上、终点向下取整到 `n` 的纪元整数倍（Loki 自己会把起点向下、终点向上对齐到 step，独立审查实测曾让第一个点早于起点 3 秒），对齐后起点晚于终点则发请求前报 `QUERY_OUT_OF_WINDOW`；`lookback_start_at` 含义不变，是任一点读到的最早时刻，等于对齐后的起点减回看（总不早于查询窗起点）；视图的 `window` 起点同此值。日志查询不带 `step`。
- LogQL 的字面量扫描按 Loki 词法从左到右走一遍：`"…"`（含转义）与 `` `…` ``（无转义）是字符串；字符串之外出现 `'`、`/*`、`//` 拒绝（`QUERY_CONSTRUCT_NOT_ALLOWED`，Loki 会把它们当字符字面量或注释跳过，引号藏在里面能让检查与 Loki 读到的不一致，独立审查实测绕过）；`#` 起注释到查询末尾，检查前去掉。
- LogQL：字符串字面量之外的 `offset`、`@` 拒绝；`[a:b]` 与孤立的 `]` 拒绝（LogQL 没有子查询）；范围时长按 Prometheus 语法（含 `1h30m`、`500ms`），向上取整到秒后与查询窗比较；转义引号留在字面量内；含换行或制表符的查询报 `INVALID_QUERY`。
- 租户含 `|`、空白或超过 150 字符报 `LOKI_CONFIG_INVALID`；没设地址却设了租户、CA 或凭据报 `LOKI_CONFIG_INVALID`；http 地址配 CA 也报 `LOKI_CONFIG_INVALID`。
- 只有 400 回显 Loki 的错误文本（截短、脱敏）；401、403、404 只带状态。
- 扩展档位 revision：无后端 `otel-demo-ext-c63f058afb18`、仅 ES `otel-demo-ext-4f7faf728706` 不变；仅 Loki `otel-demo-ext-dd942a2f1292`，ES 加 Loki `otel-demo-ext-f11fc2ce4d18`（审查修复加入 step 后的值，main `2d13f48a` 实测）。

## 实现决定（C5b，实现者自定，可逆，2026-11-19）

- 凭据与地址规则照 C5a：设了地址必须配凭据（`VICTORIALOGS_CONFIG_MISSING`），两种都配或半对、地址带 userinfo/查询串/片段、无地址却配了其余变量、http 配 CA 报 `VICTORIALOGS_CONFIG_INVALID`；地址可带路径前缀。理由：与 Loki 一致，产品的只读身份就是网关账号（C5-U1）。
- `AccountID`、`ProjectID` 各自可选、只接受十进制 0 到 2^32−1；只配一个就只发一个头（VictoriaLogs 缺省为 0）。理由：两者在 VictoriaLogs 是独立的头。
- streams、field_names、field_values 的 `limit` 上限同 query（1–500），field_names 照上游不带 `limit`；hits 不加 `fields_limit`，分组过多由 2 MiB 与 25k 上限整条拒绝。理由：不比上游多参数。
- 元数据行按 `hits` 降序再按取值排序，截断保留命中最多的；query 行按 `_time` 降序，`_time` 解析不了的行按原序排后。理由：结果确定，便于测试。
- hits 去掉 `total` 为 0 的序列，全无序列为 `no_data`。理由：空桶不是证据。
- LogsQL 扫描不把 `#` 当注释、字面量外拒 `'`、未闭合字面量报 `INVALID_QUERY`；禁用词按整词、不分大小写，要搜它们须加引号。理由：fail-closed，避免工具与 VictoriaLogs 对引号或注释理解不一致而漏检。
- 发请求的 HTTP 读取与期限逻辑抽成 `loki_logs.http_get`，Loki 与 VictoriaLogs 共用，Loki 行为与字节不变（C5-13）。
- 只读网关：`scripts/loki_readonly_gateway.py` 加 `--backend victorialogs`，不另起脚本；默认 `loki` 渲染的配置逐字节不变（本机对照旧版输出 diff 为空）。VictoriaLogs 网关容器 `opspilot-victorialogs-gateway`、默认端口 53103、状态目录 `tmp/m1-kind-lab/victorialogs-gateway`、设置文件 `tmp/m1-kind-lab/victorialogs-readonly.env`，`start` 支持 `--account-id`/`--project-id`。
- CI：`log-backends` job 加 `victoriametrics/victoria-logs:v1.54.0`（digest `sha256:ee4e820c…`，2026-11-19 从 Docker Hub 读取）与第二个 nginx 网关容器，新步骤跑 `tests/integration/test_tool_expansion_c5b_victorialogs*.py`（测试作者的同名文件与实现者的 `_impl.py`），子查询探针结果写入 job 日志。
- hits 另带 `extra_filters=_time:[start, end)`（lead 2026-11-19 批准，PR #731；理由：start/end 与桶标记不变、整窗都计，备选「自己把窗口缩到整步」会丢两端各至多一步并拒绝大步长）：VictoriaLogs v1.54.0 在带 `step` 时把 start 向下、end 向上对齐到 step 再过滤（源码 `app/vlselect/logsql/logsql.go` `parseCommonArgsExt` 与 `alignStartEndToStep`），CI 实测窗外一条被计入；同一窗口作为额外过滤条件在对齐之后取交集，计数只含窗内，start/end 仍为查询窗。
- CI 实测（PR #731 第二轮）：select 五条路径接受 GET；`limit=N` 返回最新 N 条且按 `_time` 降序；`in(子查询)` 受 start/end 约束（不泄漏）；`options(ignore_global_time_filter=true)` 读到窗外数据（泄漏），C5-7 继续拒绝；`/internal/force_flush` 只接受 POST。
- LogsQL 扫描再收紧（lead 2026-11-19，PR #731 审查）：字面量之外 `|` 后（跳过空白）紧跟带引号的 token 一律拒绝 `QUERY_CONSTRUCT_NOT_ALLOWED`。理由：VictoriaLogs 是否把 `| "join" …` 当管道名未核实，fail-closed；`_impl` 集成探针直连后端记录它的实际处理（只记录）。
- 元数据截断说明改为「只显示其中一部分（limit 条），不一定是命中最多的」。理由：VictoriaLogs 在 `limit` 下返回哪些取值不保证按命中数。
- 已知差异（不改）：VictoriaLogs 的 HTTP `end` 是开区间，query/streams/field_* 发出的是查询窗终点本身（恰在终点的条目不计），hits 的 `extra_filters` 也写成半开 `[start, end)`，两者一致于 VictoriaLogs 语义，但与视图里「窗口终点」的闭区间写法差 1 ns；扫描的 `"…"` 只按反斜杠跳一个字符，不校验 `\x`、`\u` 转义的长度，畸形转义由 VictoriaLogs 报 400。
- 扩展档位 revision 随描述更新（模型可见说明加了引号管道一句）：仅 VictoriaLogs `otel-demo-ext-840df01f3bce`；ES 加 VictoriaLogs `otel-demo-ext-381e74cfe8d0`；Loki 加 VictoriaLogs `otel-demo-ext-cbd3b6aaa6b8`；三者都有 `otel-demo-ext-da924862e562`。不含 VictoriaLogs 的四个值不变（本分支实测）。

## 证据

- 本机：独立作者合同测试 `tests/test_tool_expansion_c5a_contract.py` 113 passed（作者未看实现，对自写替身做 23 处变异全部检出）；B1 合同与 tracing 222 passed；ruff、mypy 干净。全量与真实 Loki 集成测试交 CI（`log-backends`）。

## 待决

无。实施中再冒出的不可逆项或合同层取舍追加到 r10。
