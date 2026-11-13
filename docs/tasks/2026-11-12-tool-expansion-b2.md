# 工具面拓展 B2：告警规则、抓取目标与指标元数据

- 状态：实施中（代码与 lab 代理改动已在分支上；独立合同测试由另一位测试作者编写，钉 403 的测试待其改写）
- 更新日期：2026-11-13
- 依据：[工具面拓展任务记录](2026-11-12-tool-expansion.md)、[决定表 r3](2026-11-12-tool-expansion-contract.md)（r3 的 B2-1 至 B2-6）、C3 第 8 节；上游 HolmesGPT `a0009195` 的 `prometheus/prometheus.py` 与 `kubernetes.yaml`；Prometheus `v3.5.0` API 文档；F14 上游对照合同（[任务记录](2026-10-27-m3-f14-upstream-contract.md)）。issue #561。开工前预审在仓库外（A4、A5、B2 预审，2026-11-12）。
- 工作区：worktree `/Users/shenghuikevin/dev/AI/poa-b2-impl`，分支 `feature/tool-expansion-b2-prometheus-rules`（起点 `origin/main` 43c12203）。

## 用户裁决：B2-1（2026-11-12，选 a）

lab 的 Prometheus 只读代理现在**有意拒绝**告警规则与指标元数据接口：F14 的可逆默认 U2 定为「两边都没有，权限对齐」，并钉在测试里。B2 要放行告警规则、指标元数据、抓取目标三个只读接口，问题是对谁放开。用户 2026-11-12 裁决选 a：对所有账号放开，含 Holmes 基线账号。当时的两个选项：

- a（推荐）：对所有账号放开，含 Holmes 基线账号。两边权限再次对齐；代价是 Holmes 基线以后的 Run 比已公布的 F14 结果多两个能用的工具，旧结果按日期保留。
- b：只对 OpsPilot 的 investigator 账号与 lab 放开。已公布的对比条件不变；代价是 OpsPilot 比 Holmes 多权限，违反 U2 的理由。

两个选项都要改钉 403 的测试，其中一条是验收测试（意图是「不在放行面里」），按意图要改，拒绝样例换成仍拒的路径（如 `/api/v1/status/config`）。放宽权限并改验收测试的意图，按 AGENTS.md 由用户定，已按 a 裁决。Holmes 基线此后的 Run 比已公布的 F14 结果多这三个接口可用，旧结果按日期保留，新旧不混比。

## 目标

调查能看 Prometheus 的告警与记录规则（含当前在响的告警）、某个抓取任务的抓取目标状态、指标的类型与说明。三个新工具只进扩展档位 `otel-demo-ext`，`otel-demo` 档位不变。

## 行为（公开接口）

- `list_prometheus_rules(type?, rule_name?, rule_group?, file?, match?)`：列出告警规则或记录规则，可按名字、组、文件、选择器筛；`type` 只认告警与记录两种。不提供 jq 过滤（上游有）。
- `get_prometheus_target(target_name)`：按抓取任务名（job）取活跃的抓取目标，去掉体积大的服务发现原始标签。
- `get_metric_metadata(metric?)`：查指标的类型与说明，最多 100 条，满 100 条标不完整。
- 三个工具都没有时间窗：结果是查询那一刻的规则、告警与目标状态，描述写明不能证明事故发生时的状态。

参数、过滤方式与错误分类以决定表 B2-2 至 B2-6 为准。

## 这个 PR 做什么

- 在 A5 的 `opspilot/tools/prometheus_ext.py` 里加三个工具，凭据与错误分类同 A5；扩展档位加这三个名字。
- lab：只读代理放行三个 GET 接口，范围按 B2-1 裁决；按裁决改写钉 403 的测试：`tests/acceptance/test_396_proxy_contract.py:67-72`、`tests/test_kind_lab_readonly_proxy.py:210,446`、`tests/test_f7_authz_probe.py:277`、`scripts/f7_authz_probe.py:57-60`。代码可先于 lab 改动合并，lab 改动随同一 PR 或紧随其后。
- 合同测试对着替身 Prometheus 响应写，由未参与实现的测试作者按本记录、决定表与 C3 编写，不看实现代码。

## 不做

`/api/v1/alerts`、`/api/v1/targets/metadata`、jq 过滤、Alertmanager 接口、评测回放。

## 完成条件

1. 代码 PR：合同测试与 CI 通过，`otel-demo` 钉值不变。这是功能 PR，且改权限与验收测试意图，走用户门。
2. 代码 PR 合并后 #561 关闭。真实证据归租机集中取证（#580）：租机后跑 `scripts/f7_authz_probe.py`，新放行的三条 200、其余拒绝项不变；真实 Run 里出现规则、目标或元数据的证据，报告引用；仓库里有 `summary.json` 与 LangSmith trace 链接。

## 没有真实实例时的已知风险

1. lab 抓取目标的标签里是否带 lab 私有值（未确认，取证时扫）。
2. Prometheus v3.5 的规则接口是否默认分页（文档写分组上限需显式传，不传不分页）。

## 全量探针

骨架提交 `4df603ab`（三个工具名进扩展档位、代理放行三条）交 lead 推草稿 PR 经 CI 补跑。本机定向跑的红项：`tests/test_kind_lab_readonly_proxy.py:210-211`、`tests/acceptance/test_396_proxy_contract.py:67-72`，另有两处预审没列的：`tests/test_399_isolation_impl.py:183` 与 `tests/acceptance/test_399_isolation.py:188-189`——Holmes 隔离检查的代理日志放行集合直接取 `kind_lab.READ_PATHS`（`scripts/holmes_baseline/isolation.py:148`），按 B2-1 = a holmes GET 这三条不再算违规，测试意图随裁决翻转。`tests/test_f7_authz_probe.py:277` 仍绿（holmes 对 rules、metadata 的表单 POST 仍 403）。

## 实施记录

自行决定的可逆技术细节：

- 代理：三条路径加进 `kind_lab.READ_PATHS`，与现有读路径同一 `location =` 精确匹配，GET/HEAD、丢弃请求体；`/api/v1/targets/metadata` 等子路径不匹配仍落到 `@denied`。理由：复用已审过的规则，不新写分支。
- 探针：新增仍拒的 GET `/api/v1/alerts`、`/api/v1/targets/metadata`、`/api/v1/status/config`，对四个账号与 holmes 预期 403；targets 探测带 `?state=active` 与工具一致。理由：把「其余不放宽」变成可测的行。
- 规则：结果路径 `data.groups`，原样字节入证据；`data.groupNextToken` 出现即标不完整（对应风险 2）。rule_name、rule_group、file 只校验非空、≤2000 字符、无控制字符，以 `[]` 形式转发。理由：照上游参数，挡掉空串与换行。
- 目标：结果是投影记录（`targets` 加原始响应 sha256 与字节数），视图另给 `jobs_seen`（全部活跃目标的 job 名，最多 100 个）与 `active_targets_total`，便于名字没匹配时改正。理由：`scrapePool` 不一定等于 job，模型需要看到可用 job 名。
- 元数据：Prometheus 返回的是「指标名 → 条目列表」的对象，执行器要求结果是列表，所以投影成每条 `{metric, type, help, unit}` 一行；不完整按指标个数满 100 判定。指标名按经典格式 `[A-Za-z_:][A-Za-z0-9_:]*`、≤200 字符校验。
- 三个工具 `may_contain_secrets=False`、超时 30 秒、1 MiB 读取上限、视图 25k，同 A5（B2-6）；空结果是执行器的 `no_data`。

偏离与已知限制：

- 规则与目标的返回里有 annotations、标签和 scrapeUrl，lab 私有值未确认（风险 1），只靠共用的凭据脱敏。
- Prometheus v3 允许 UTF-8 指标名，元数据的名字校验只认经典格式，带点号等字符的名字会被拒。
- HEAD 在三条新路径上预期 405 沿用现有读路径的实测结论，未在真实实例确认。

## 下一步

独立合同测试合入后让其通过；CI 全量；租机取证（#580）跑 `scripts/f7_authz_probe.py` 与真实 Run。
