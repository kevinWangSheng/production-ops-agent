# B3 实时用量真实 Run（线 L，2026-11-20）

性质：有界真实运行（主机 opspilot-tool-1，main a5e70d38，#666 驱动 `round --trials 1 --trace lab`，DeepSeek 真调，宿主进程拓扑，线 L 端口 30200–30999）。每 Run 只有产品默认防循环上限 100 次模型请求。不是生产证明。不含凭据：证据目录扫描 11 个凭据值 0 命中（正对照 1 命中；LANGSMITH_WORKSPACE_ID 是 trace URL 里的组织 id，不算凭据，已排除）。

- 拓扑：驱动里没有 B3 题，所以用线 L 自写的辅助脚本（`/home/opspilot/rental-logs/L/l_b3_run.py`，没有改动仓库代码）。它复用驱动的 PG、web_env、worker_env、/intake/ui 提问和 run_freeze；web 与 worker 都设 `OPSPILOT_K8S_LIVE_METRICS=1`，worker 使用 kind lab 的真实只读身份（tmp/m1-kind-lab/k8s-investigator.env）；Prometheus 和 Jaeger 指向不可达地址。
- 问题："Right now, which pods in the otel-demo namespace are using the most CPU and memory, and how loaded is the cluster's node? ..."
- Run `4bc182d1-4eef-54d8-a1c8-c2abfcb4a697` completed，2 次模型请求；prompt 29,830（命中 25,600），completion 3,007。kubectl_top_pods 与 kubectl_top_nodes 都返回 ok，报告引用了这 2 条证据。tool_schema_revision `otel-demo-ext-4730c27997c9`，与 B3 记录里"仅开关"的值一致。
- trace：https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/22e5924b-c48c-4e79-9391-ec4dcc7436b0/r/00000000-0000-0000-f666-3b657a58e988；`summary.json`（形状是 run_freeze 的 run 块加 trace，不是驱动 schema）。
- R8-21 直接取样：`scripts/live_metrics_sample.py --namespace otel-demo` 退出码 0，`agree: true`；Pod 22 行、节点 1 行，行集合一致，同一时间戳的行没有不一致；metrics-server 为 v0.8.1，digest 与记录一致。结果见 `sample.json`。
- 判分：B3 不是驱动题，不判。
