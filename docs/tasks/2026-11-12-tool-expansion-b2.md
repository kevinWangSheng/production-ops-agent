# 工具面拓展 B2：告警规则、抓取目标与指标元数据

- 状态：待开始（B2-1 已裁决；开工说明已齐，未实现；在 A5 合并后开工）
- 更新日期：2026-11-12
- 依据：[工具面拓展任务记录](2026-11-12-tool-expansion.md)、[决定表 r3](2026-11-12-tool-expansion-contract.md)（r3 的 B2-1 至 B2-6）、C3 第 8 节；上游 HolmesGPT `a0009195` 的 `prometheus/prometheus.py` 与 `kubernetes.yaml`；Prometheus `v3.5.0` API 文档；F14 上游对照合同（[任务记录](2026-10-27-m3-f14-upstream-contract.md)）。issue #561。开工前预审在仓库外（A4、A5、B2 预审，2026-11-12）。
- 工作区：开工时另开 worktree，分支 `feature/tool-expansion-b2-prometheus-rules`，写入本节。

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
2. #561 关闭：租机后跑 `scripts/f7_authz_probe.py`，新放行的三条 200、其余拒绝项不变；真实 Run 里出现规则、目标或元数据的证据，报告引用；仓库里有 `summary.json` 与 LangSmith trace 链接。

## 没有真实实例时的已知风险

1. lab 抓取目标的标签里是否带 lab 私有值（未确认，取证时扫）。
2. Prometheus v3.5 的规则接口是否默认分页（文档写分组上限需显式传，不传不分页）。

## 全量探针

未跑。预计失败即上面列的 4 个钉 403 的测试文件。开工前按 A2 先例开草稿 PR 经 CI 补跑。

## 下一步

B2-1 已裁决并写入决定表；A5 合并后由实现会话接手。
