# ITBench 现场版全量前验证（#866，2026-11-25 轮 `prep-1125`）

结果标签：ITBench 现场版 SRE（自托管，自测，不与官方榜单并列）。本轮只验证评测设施加固（PR #869）和评测侧 namespace 标签补丁，不计入全量结果。

## 主机与版本

- 主机 `opspilot-tool-1`（8 vCPU / 32 GB）；ITBench `5ed31cc4`；评测器 `ITBench-Evaluations@14f026fc`，judge Z.ai；模型 `deepseek-flash`。
- 新建 kind 集群（1 控制面 + 2 worker）。`opspilot-m1` 已停止，保留，所以加了 `--allow-other-clusters`。
- S30 跑在 `f0bf3d98`，S31 跑在 `9a96b1bb`。之后的 `2460c174` 只加了磁盘保护，并把连续失败门槛改为 3。

## 补丁可行性（手工，部署后不注入故障）

- operator 拥有的 ServiceMonitor 设 `honorLabels` 后，5 秒内被改回。这条路不可行。
- Prometheus CR 加默认 scrape class，只改应用网关 job 的 `namespace`：30 秒内新样本带上 `namespace=otel-demo`。cert-manager 等其他 job 的标签保持上游原样。`undeploy` 会删除 CR 和 CRD。
- 补丁后、注入前观察 20 分钟：只有一条请求延迟告警持续 firing（补丁后约 1 分钟开始）。4 个服务的错误率告警约每 10 分钟进一次 pending，始终没有到 firing。

## 逐题（UTC）

| 题 | 部署 | 稳定等待 | 告警到达（自注入开始） | Run | 官方 root_cause_entity_f1 | 状态 |
|---|---|---|---|---|---|---|
| S30 | 433 s | 6 分 | 约 4 分（HighRequestErrorRate） | 95 s，completed | 1.0 | completed |
| S31 第 1 次 | 部署重试 1 次 | 等待中发 SIGTERM | — | — | — | 退出码 143；清理完成、端口释放、无遗留进程、无 summary |
| S31 续跑 | 412 s | 6 分 | 约 4 分（HighRequestErrorRate） | 170 s，completed | 0.667 | completed；旧目录自动移开（`PREVIOUS_ATTEMPT_SET_ASIDE`） |

- S30：Run `226108ef-9719-5654-9d05-124d047970fa`，[LangSmith trace](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/9be812bc-c01d-4a24-8588-fc1741ec98a8/r/00000000-0000-0000-268c-3a934a5f9d35?poll=true)。
- S31：Run `b77ba528-5ce0-51a3-a31e-d49f610e5557`，[LangSmith trace](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/9be812bc-c01d-4a24-8588-fc1741ec98a8/r/00000000-0000-0000-05ac-366ffeb6780f?poll=true)。
- 两题之前都没有入口告警（`NO_ENTRY_ALERT`，#866）。加补丁后都能进入调查。
- #859：真实 Run 中 `traces_list_services` 和 `traces_search_by_tags` 返回 ok（Jaeger v3）。S30 有一次 `traces_search` 因结果过大被产品的大小保护拒绝（`RESULT_TOO_LARGE`）。
- 每题耗时约 26–27 分钟（部署到清理结束）。
