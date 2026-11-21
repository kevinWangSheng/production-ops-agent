# C5 Loki 真实 Run（线 L，2026-11-20）

性质：有界真实运行（主机 opspilot-tool-1，main a5e70d38，#666 驱动 `round --trials 1 --trace lab`，DeepSeek 真调，宿主进程拓扑，线 L 端口 30200–30999）。每 Run 只有产品默认防循环上限 100 次模型请求。不是生产证明。不含凭据：证据目录扫描 11 个凭据值 0 命中（正对照 1 命中；LANGSMITH_WORKSPACE_ID 是 trace URL 里的组织 id，不算凭据，已排除）。

- 题 101（`l-c5-loki-101`，`--loki gateway --k8s-env-file`）：**失败，未产生 Run**。3 次机会都在平移阶段报 SHIFT_IO_FAILED "GET /loki/api/v1/labels HTTP 400"，归类 env_broken。原因可以复现：`scripts/holmes_evals/shift.py` 中 `FIXED_READ_SPAN = 730 天`，`LokiAdmin.label_names` 用 [setup−730d, setup−24h] 去查，而 Loki 2.9 默认 max_query_length 为 30d1h。单独起 grafana/loki:2.9.0 实测：跨度 730d 返回 400 "the query time range exceeds the limit (query length: 17496h0m0s, limit: 30d1h)"，30d 和 29d 返回 200。受影响的是 LOKI_FIXED_TIME 里的三题：100a、101、259_loki_docker。summary：`holmes-evals-l-c5-loki-101/.../summary.json`（status missing）。
- 题 102_loki_label_discovery（相对时间，不平移；`l-c5-loki-102`）：Run `989ebb50-b6c4-57a4-a6a1-0e2b61ec4d37` completed，7 次模型请求；prompt 145,529（命中 134,272），completion 5,513。grafana_loki_query 结果为 ok 1、no_data 2、TOOL_TIMEOUT 1（一次 24h 宽查询超时，模型缩到 10 分钟后重试），报告引用 1 条 Loki 证据加 K8s 证据。经只读网关（127.0.0.1:30431 → port-forward 3102），switch 之后读路径放行。tool_schema_revision `otel-demo-ext-dd942a2f1292`，等于"仅 Loki"。
- trace：https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/3bccf32a-a581-4219-8c3d-4b0f0ed016a6/r/00000000-0000-0000-7ca7-734db7f4fd2f。
- 核对清单（#733）：labels 带 start-end 取 2025 标签 → **失败**（见上）；乱序推回与去重 → 未验证（101 在推回之前就失败了）；网关 switch 后读路径 → 通过（102）；102a/102b 的 latest 镜像跨遍卷、100a 删 ns 耗时 → 未执行。
- 判分（2026-11-20 补判：换 key 后 `driver judge`，glm-5.3-flashx，1 次尝试）：102 通过（A）；101 没有 Run，不判。
