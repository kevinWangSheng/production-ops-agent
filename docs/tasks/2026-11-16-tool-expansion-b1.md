# 工具面拓展 B1：Elasticsearch/OpenSearch 日志检索

- 状态：预审完成、裁决完成，待开工（不含实现）
- 更新日期：2026-11-16
- 依据：[B/C 批次记录](2026-11-16-tool-expansion-bc.md)、[决定表](2026-11-12-tool-expansion-contract.md) r1–r8 与 r9（本项 B1-U1、B1-U2、B1-1 至 B1-22）、C3 第 8、12 节；上游 HolmesGPT `a0009195` 的 `elasticsearch/data`（`elasticsearch.py`）。issue #560。预审全文在仓库外（`~/.agent/handoffs/opspilot-b1-prereview-2026-11-16.md`），对照 `main` `2f7ec758`。
- 工作区：开工时填（建议 worktree `../poa-560-es`，分支 `feature/560-es-logs`，起点最新 `origin/main`）。

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

## 不做

- 集群管理类工具；按需工具搜索；OpenSearch 只读角色的实测（留到 #671）。
- 评测题的时间戳平移（属 #666，已在 #666 记录）。

## 待决

无。
