# A7 主路径 Run：前后对照（#559，租机 2026-11-14）

类别：集成运行（真实 kind lab、真实 DeepSeek、宿主进程拓扑），不是生产证明。驱动 `scripts/a7_main_path.py` 从主 checkout（提交 P `e6be89ad`）运行；场景 flagd `paymentFailure` 100%（A7-9 a）；LangSmith 项目 `opspilot-lab-a7-main-path-20261114`（longlived）。时间 UTC。

拓扑：web 与 worker 在部署 checkout 里以宿主进程起（`OPSPILOT_TOOL_PROFILE=otel-demo-ext`，`OPSPILOT_TRACE=lab`），web 绑 kind 网关 `172.18.0.1:8080`，lab 以 `OPSPILOT_LAB_HOST_ADDRESS=172.18.0.1` 重渲染 webhook，ufw 放行 172.18.0.0/16 → 8080；宿主 PG 17.12 在 55431。

| 侧 | 部署 checkout | 版本 | 注入 → 开单 | Run 结束 | 退出 | 判定 |
|---|---|---|---|---|---|---|
| before-1 | `dc9156b7`（`replay-candidate`，`prompt-replay-candidate-bef70649a3a8`） | `version_problems` 空 | 19:47:27 → 19:53:04 | `terminal`，completed | 0 | 四类都引用；`human_free` 真；11 轮，530,500 prompt（396,800 缓存命中）+ 26,033 completion |
| after-1 | `e6be89ad`（`replay-candidate-k8s`，`prompt-replay-candidate-k8s-0282ee7d0e7a`） | `version_problems` 空 | 20:14:34 → 20:20:04 | `terminal`，completed | 0 | 四类都引用；`human_free` 真；12 轮，637,269 prompt（553,216）+ 25,247 completion |

- `compare.json`：`after_reached` 1/1，`done` true（每边一次，不下因果结论，A7-11）。补跑没有发生。
- 引用类别（结论类别 → 条数）：before 指标 7、trace 7、集群对象 7、日志 5；after 指标 14、trace 5、集群对象 7、日志 8。
- 注入期间工程侧探一次（4c.1，19:55）：`payment` 近 10 分钟 32 行日志中 14 行含 error/fail/exception；`checkout` 20 行中 0 行。
- 每次 Run 结束后：驱动恢复 `paymentFailure` 为 off 并复读、冷却 900 秒；`ALERTS{alertname="CheckoutPlaceOrderErrorRatioHigh"}` 为空后在工作台关闭事故（before 20:13:34、after 20:38:14）。
- trace：[before-1](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/bdfc6724-66c5-43b6-8bc3-f4d1b224a5da/r/00000000-0000-0000-b4d1-6c27a129022c)、[after-1](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/bdfc6724-66c5-43b6-8bc3-f4d1b224a5da/r/00000000-0000-0000-ba56-f1dd20c8dc27)。原始 ledger 在主机 `tmp/lab-ledgers/`，不入库。
- 4b 冒烟告警留下的 handoff 事故 `7e947c61`（测试告警名、无 Run、无目标）工作台按设计不能关闭（`HANDOFF_ONLY`），驱动开跑前检查不受影响。

## lead 决定（2026-11-14，检查点 4）

发现 (a) `traces_search`/`traces_search_by_tags` 在 #628 之后仍有 `RESULT_TOO_LARGE`；(b) 模型参数错误（`kubernetes_jq_query` INVALID_PARAMS、猜错 Pod 名 `POD_NOT_FOUND`）；(c) 模型读 ConfigMap（Q5 已知风险）：**只记录，不改提示词也不改工具**。理由：此时任何改动都会触发 A7-14 停点、要求在最终提交上重跑 A7 后一次；三项都不阻塞。
