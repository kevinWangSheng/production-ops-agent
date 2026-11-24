# C5 VictoriaLogs 真实 Run（线 L，2026-11-20）

性质：有界真实运行（主机 opspilot-tool-1，main a5e70d38，#666 驱动 `round --trials 1 --trace lab`，DeepSeek 真调，宿主进程拓扑，线 L 端口 30200–30999）。每 Run 只有产品默认防循环上限 100 次模型请求。不是生产证明。不含凭据：证据目录扫描 11 个凭据值 0 命中（正对照 1 命中；LANGSMITH_WORKSPACE_ID 是 trace URL 里的组织 id，不算凭据，已排除）。

- 题 257（`l-c5-vl-257`，`--victorialogs docker`），VL 镜像 digest ee4e820c…，网关 127.0.0.1:30533 → 30528。Run `fc9f886e-2928-5df3-bc0c-57883ca150c3` completed，4 次模型请求；prompt 76,713（命中 67,968），completion 2,740。victorialogs_query、streams、field_names、field_values 都返回 ok，报告引用 5 条 VL 证据。tool_schema_revision `otel-demo-ext-840df01f3bce`。
- trace：https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/e00ae69c-fd2f-4d61-bda9-5168aae9715c/r/00000000-0000-0000-01d6-3fb1b0ce93a0；summary：`holmes-evals-l-c5-vl-257/257_victorialogs_log_search/trial-1/summary.json`。
- 判分（2026-11-20 补判：换 key 后 `driver judge`，glm-5.3-flashx，1 次尝试）：257 通过（A）。
