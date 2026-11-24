# #780 真实证据：带常驻指令的人工提问 Run（租机 2026-11-24）

主机 `opspilot-tool-1`（Vultr，8 vCPU / 32 GiB）。kind otel lab 从 main 检出 `179c1e30` 起，web 与 worker 从 PR 分支 `e81d5f23` 起，档位 `otel-demo-ext`，`OPSPILOT_KNOWLEDGE_TOOL=1`，`OPSPILOT_TRACE=lab`，模型 `deepseek-flash`，lab 无故障注入（flagd 开关全 off）。私有 PG 17 在该检出的 `tmp/m0-b/postgres`（55431），`make migrate` 升到 `0029_standing_instructions`，web/worker 用 `runtime-roles` 建的两个登录角色。时间 UTC。

## 过程

- 18:11:53 前：经 `POST /runbooks` 提交 `kind=standing_instructions`、目标 `m0-otel-20260909`、三行 `* check ...`（checkout 错误率并与前一小时比较；otel-demo 近期部署、rollout 与 Pod 重启；checkout 的 payment 依赖有无错误或延迟升高），再经 `/runbooks/submissions/<id>/review` 批准：entry `484e6b93`，revision 1。
- 18:11:53：`/intake/ui` 提问 “Is checkout healthy right now? Please check the checkout service and report anything unusual.”。事故 `16132b39-1f65-524d-a4ca-fdfcc71735d3`，Run `9cefea0a-b7e6-5e4b-a9ca-f69986c4db20`。
- 18:14:09：Run `completed`，13 轮，结论 `supported`。18:15:55：`run_freeze` 退出 0，`report_present` true，trace 回读 `found`。
- trace：[LangSmith](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/aeaaaae7-959a-475c-8ccb-beea1658bea8/r/00000000-0000-0000-29c5-52c17c52bbb7)（项目 `opspilot-lab-780-rental-1124`）。冻结摘要 [summary.json](summary.json)；原始 ledger 只留在主机，sha256 `dd4c03b0…17e5`。

## 检查

| 检查点 | 结果 | 依据 |
|---|---|---|
| Run 输入是 v9，带 `standing_instructions` fact | 是 | PG `opspilot_runs.input`：`version` 为 `opspilot-investigation-input-v9`；`scope_facts.standing_instructions` 带 entry_id、revision 1、`approved_by`、`policy_version: opspilot-standing-instructions-v1` 与三行原文 |
| trace 里常驻指令消息在 runbook 列表之前 | 部分：常驻指令消息存在且位置正确，但本 Run 没有 runbook 列表消息可比 | 第一次模型调用的消息依次是 system、问题、常驻指令边界消息加原文（751 字符，开头 “The text below is the reviewed standing instructions…”），然后是 data_sources 消息。该目标没有已批准的 runbook，`runbook_catalog.entries` 为空，所以没有 runbook 列表消息 |
| 报告逐条回应三项指令 | 是 | 错误率：告警规则比值为 0，24 小时 span 增量 ERROR 2.04 对 UNSET 692.24，两条错误 trace 出现在 17:32–17:33 启动期；“前一小时比较”做不到，因为 lab 在 17:31 重建，17:36:53 之前没有样本，报告在 gaps 里写明。部署与重启：Deployment 1/1，restartCount 0，并指出 17:31–17:33 全 namespace 重建，触发方因没有变更历史无法确定。payment：rpc 状态码只有 0、span 时延、交易指标与日志都正常，并写明 no_data 不等于没有错误。gaps 另有一条逐项列出三项都已执行 |
| prompt_revision | Run 的 `versions`（claim 校验）与 trace 根元数据是 `prompt-replay-candidate-k8s-566b5c603da3`；结论 step 记录的是 `prompt-replay-candidate-k8s-8d01461bc500` | 在同一检出上，`prompt_revision_versions("replay-candidate-k8s", knowledge_tool=True)` 算出 566b5c603da3，加 `standing_instructions=True` 算出 8d01461bc500。8d01 只出现在 ledger 的 `steps/13/response/conclusion/prompt_revision`，`summary.json` 里没有 |

工具调用 31 次：`metrics_range_query` 9（ok 7、no_data 2），`kubernetes_jq_query` 6 ok，`get_label_values` 5 ok，`fetch_pod_logs` 4（ok 2、no_data 2），`traces_search_by_tags` 2（ok 1、no_data 1），`traces_search` 1 次 `RESULT_TOO_LARGE`；`execute_prometheus_instant_query`、`list_prometheus_rules`、`get_all_labels`、`get_metric_names` 各 1 次 ok。

## 费用

token（冻结摘要）：prompt 712,668（缓存命中 646,400，未命中 66,268），completion 24,392。按 [tool-expansion 记录](../../tasks/2026-11-12-tool-expansion.md) 用过的 deepseek-flash 单价（每百万 token：命中 $0.003、未命中 $0.15、输出 $0.60；本次没有重新读定价页，当前价格未核实）算：$0.0019 + $0.0099 + $0.0146 ≈ **$0.027**。调用时刻是北京时间周二 02:11–02:14，属闲时。

## 失败与环境

- worker 第一次启动后退出：`OPSPILOT_HEALTH_BIND=127.0.0.1:9464 cannot be bound (errno=98)`，因为 web 已经占用了默认的健康端口。worker 改设 `OPSPILOT_HEALTH_BIND=127.0.0.1:9465` 后重启成功。这是环境问题，代码没有改。在提交事故之前就已重启。
- 凭据扫描：用三份 env 文件里每个变量值、UI 口令、哈希与会话密钥逐个搜索本目录。只有 `LANGSMITH_WORKSPACE_ID` 命中 1 次，就是上面 trace 链接里的组织 ID，以前的证据也用同样的链接格式。其他都是 0 命中。
- web、worker 与私有 PG 已按记录的 PID 停止，kind lab 留着运行（otel-demo 22 个 Pod Running）。
