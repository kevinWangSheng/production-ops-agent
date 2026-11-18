# 工具面拓展 C5：Loki / VictoriaLogs 日志检索

- 状态：预审与裁决完成（[决定表 r10](2026-11-12-tool-expansion-contract.md)，2026-11-18）；C5a Loki 开工，C5b VictoriaLogs 在 C5a 合并后
- 更新日期：2026-11-18
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

## 这个 PR（C5a）做什么

- 新模块 `opspilot/tools/loki_logs.py` 与传输、工具与模型可见描述（只改工具描述，不改调查提示词）、扩展档位按后端集合计算、lab 闸门、入口脚本放行 `OPSPILOT_LOKI_*`；在 B/C 记录登记数据源。
- 合同测试（C5-15 清单的 Loki 部分）由未参与实现的测试作者按本记录、r10 与预审编写，不看实现代码；本机用 `http.server` 替身。
- 接真实后端的集成测试（R8-25，C5-16 至 C5-20 的 Loki 部分）：CI 新 job `log-backends`，Loki 2.9.0 与最新 3.x、nginx 只读网关（C5-18），合成数据相对当前时间，断言真实返回、时间过滤、截断、错误分类与两层只读拒绝（经网关写入 403、数据条数不变）；不调模型。
- 交给 #671/#669：环境变量清单、网关配置与切换脚本（预审「交给 #671 / #669 租机冒烟」节）。

## 证据

（实施后填写）

## 待决

无。实施中再冒出的不可逆项或合同层取舍追加到 r10。
