# 工具面拓展 A2：扩展档位与集群对象只读查询

- 状态：待开始（开工说明已齐，未实现）
- 更新日期：2026-11-12
- 依据：[工具面拓展任务记录](2026-11-12-tool-expansion.md)、[决定表 r3](2026-11-12-tool-expansion-contract.md)（r1 用户裁决，r2 的 A2-1 至 A2-15；r3 是后续各项对 A2 的要求）、SPEC 2026-11-12 段、C3 第 3、8、12 节；上游 HolmesGPT `a0009195` 的 `kubernetes/core`。issue #554。
- 工作区：开工时另开 worktree，分支 `feature/tool-expansion-a2-k8s-core`，写入本节。

## 目标

调查能只读查询被调查集群里的对象与事件。新增扩展档位 `otel-demo-ext`：`otel-demo` 的全部工具加两个新工具。`otel-demo` 档位的工具面与版本一个字节不变。

## 行为（公开接口）

- `kubernetes_jq_query(kind, jq_expr, namespace?, label_selector?, field_selector?)`：列出某类资源，在取回的对象上跑模型写的 jq 表达式，返回结果列表。
- `kubernetes_count(同样的参数)`：jq 过滤后计数，返回匹配总数与前 10 条预览。
- 名字与参数照搬上游。`kind` 是复数资源名，只认静态表里的；事件用 `kind=events` 查。
- `namespace` 不传就是整个集群。
- 问 Secret：直接拒绝，不向集群发请求，模型看到明确的拒绝原因。
- 对象是查询那一刻的状态，工具描述写明它不能证明事故发生时的状态；事件只返回授权时间框内的。
- 结果超过大小上限时整条拒绝，不返回部分结果。
- 配置只在 worker 读：`OPSPILOT_K8S_API_URL`、`OPSPILOT_K8S_TOKEN`、`OPSPILOT_K8S_CA_FILE`、`OPSPILOT_K8S_NAMESPACE`。worker 选了扩展档位但缺配置或找不到 jq 时启动失败。

细节（分页、jq 子进程的限制、错误分类、时间字段、原因码）以决定表 A2-1 至 A2-15 为准。

## 这个 PR 做什么

- 独立模块 `opspilot/tools/kubernetes_core.py`：两个工具的 schema 与注册（含五段模型可见描述）、配置、只发 GET 的 HTTPS 传输、静态资源表、jq 执行器。不往 `otel_demo.py` 里加。
- `profiles.py` 加档位；传输路由改成按工具分派；`registry.py` 允许登记可能含秘密的数据源；两个新原因码；LangSmith lab 闸门认新档位；runtime 镜像装 jq。
- 合同测试对着替身 API 响应写，由未参与实现的测试作者按本记录、决定表与 C3 编写，不看实现代码。
- 按用户裁决改写两处现有测试：`tests/test_m1_tool_registry.py` 里「可能含秘密的来源在登记时被拒绝」改为允许声明为真、非布尔值仍拒绝；只读安全验收里「每个出厂工具都是有界只读」那条加上扩展档位的注册。

## 后续各项对 A2 的要求

来自 A1、A3/A6、A4/A5 的开工前预审（决定表 r3）。不改 A2 的行为，只约束实现形状，免得后续各项返工：

- 传输路由按显式工具名分派，不按 `kubernetes_` 前缀匹配：`RoutedTransport` 现只有两路（`opspilot/tools/evidence_read.py:373-386`）；A3 的 `fetch_pod_logs` 不带这个前缀，前缀匹配会把它送到 otel-demo 传输。
- 配置、GET 传输、静态资源表、分页与 4 MiB 累计、错误映射、事件按授权框筛选做成可复用函数，A3（Pod 日志）与 A6（按列查询）直接调用；A6 要复用「取一类资源的全部对象（已筛事件）」这一步，事件筛选不共用会让按列查询绕过授权框（A6-1）。
- 定下 jq 超时报哪个结果类（A2-8 只写了超时用剩余时间）；A3、A6 的正则子进程沿用 jq 子进程的约束，超时报同一个类（A3-7、A6-4）。
- 新工具名不加进参与 `otel-demo` 版本哈希的集合：`PROMETHEUS_TOOLS`（`opspilot/tools/otel_demo.py:179`）进了 otel-demo 的 `data_sources` 哈希（`otel_demo.py:1047-1083`，A5-7）；Kubernetes 与后续工具名同理只进扩展档位。
- 容器里的 worker 拿集群凭据归 A1，不在 A2：部署入口脚本的变量放行名单只导出 `OPSPILOT_OTEL_*` 与 `LANGSMITH_*`（`deploy/docker-entrypoint.sh:54-60`），CA 文件要靠 lab 覆盖 compose 挂进只读的 worker 容器（`deploy/compose/compose.yaml:18-30,130-153,213-231`），重发 token 时要替换而不是保留旧值（`scripts/kind_lab.py:558-570` 的 `_merge_env`）（A1-7）。A2 只负责 worker 读这四个变量；新加的 API 地址 lab 检查放进 `check_lab_target`（`opspilot/tracing.py:181`），与 Prometheus、Jaeger 两项并列（A1-8）。
- 视图字段键名：A3 的 `lines_read` 等、A6 的 `total_items_processed` 要能与 A2 用到的通用视图字段并存，A2 落地后对照一次。

## 不做

按列查询（A6）、Pod 日志（A3）、提示词里的 Kubernetes 调查指导（A7）、扩展档位的评测回放、真实 Run 脚本的档位选项、`events.k8s.io` 组、watch、自定义资源、Pod 环境变量值脱敏（#574）、工作台专门视图、token 自动续发。

## 完成条件

1. 代码 PR：合同测试通过，CI 通过，`otel-demo` 档位的钉值测试与结构回放不变。这是功能 PR。
2. #554 关闭：租机后的真实集群上，worker 切到扩展档位、注入故障后提问，工作台证据里出现集群对象查询，报告引用了 Pod 状态或事件；问 Secret 看到明确拒绝；仓库里有 `summary.json` 与 LangSmith trace 链接。

第 1 条不需要集群，在本机完成。第 2 条与集群只读身份的拒绝取证（A1）等一次租机集中做。

## 没有真集群时的已知风险

替身响应按官方 Status 结构写，下列各项到真集群上取样核对，可能返工：

1. worker 连 API server 用的地址、证书名是否对得上，lab 闸门是否认这个地址。
2. 错误体形态：403、namespace 不存在、翻页 token 过期（410）。
3. 整个集群的列表体积是否碰到 4 MiB 与 2 MiB 上限。
4. 事件的时间字段常有空值。
5. 镜像里 jq 的版本。

## 本机约束

本机内存紧张：不跑 `make check`、全量 pytest，不起 Docker、colima、kind。定向测试用 `.venv/bin/python -m pytest <文件>`，全量交给 CI。

## 下一步

由实现会话接手：开 worktree，派测试作者与实现者，按 AGENTS.md 的功能 PR 流程走。
