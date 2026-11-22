# B1 Elasticsearch 真实 Run（线 L，2026-11-20）

性质：有界真实运行（主机 opspilot-tool-1，main a5e70d38，#666 驱动 `round --trials 1 --trace lab`，DeepSeek 真调，宿主进程拓扑，线 L 端口 30200–30999）。每 Run 只有产品默认防循环上限 100 次模型请求。不是生产证明。不含凭据：证据目录扫描 11 个凭据值 0 命中（正对照 1 命中；LANGSMITH_WORKSPACE_ID 是 trace URL 里的组织 id，不算凭据，已排除）。

- 后端：真实 ES 8.19.25（CI 同 digest），容器 `l-es` 在 127.0.0.1:30920，开安全认证；工程师 API key 交给上游 before_test 用，产品只读 key 由 `scripts/es_readonly_identity.py` 以 elastic 基本认证创建，通过 `--es-readonly-env` 传给驱动。
- 题 254（`round l-b1-254b`），Run `3564c6e0-61ac-5fc7-81a1-26bd2451cdb0` completed，7 次模型请求；prompt 138,657（缓存命中 131,584），completion 8,075。工具：elasticsearch_list_indices、mappings、search 都返回 ok，报告引用 2 条 elasticsearch_search 证据。K8s、Prometheus、Jaeger 的错误是驱动故意给的不可达地址（H-11）。tool_schema_revision `otel-demo-ext-4f7faf728706`，与 B1 记录里"仅 ES"的值一致。
- trace：https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/69bc3aa3-60f2-44d9-a4c0-c0c6820a5c69/r/00000000-0000-0000-9a12-6455bd72bd5d；summary：`holmes-evals-l-b1-254b/254_elasticsearch_dr_test_log_check/trial-1/summary.json`。
- 判分：judge_exhausted / JUDGE_HTTP_ERROR，原因是 rental.env 的 ZAI_API_KEY 失效（直接 curl 返回 401 "token expired or incorrect"）。
- 第一次尝试（`l-b1-254`）有两个问题：①驱动 `--es external` 不带 `--es-readonly-env` 时得到 ES_READONLY_IDENTITY_FAILED，因为真实 ES 拒绝用 API key 派生带权限的 key（"creating derived api keys requires an explicit role descriptor that is empty"）；②随后一次 Run 已经 published，但证据目录归 root，冻结时 PermissionError，这次 Run 没有冻结（DeepSeek 用量未计入上面的数字）。
- 未执行：OpenSearch 两题与只读用户直接写入被拒（B1-19）。驱动的只读身份写死了 `--backend elasticsearch`，而且主机没有拉取 OpenSearch 镜像。
- 判分（2026-11-20 补判：换 key 后 `driver judge`，glm-5.3-flashx，1 次尝试）：254 通过（A）。
