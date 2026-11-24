# C2b runbook 导入（OPSPILOT_KNOWLEDGE_TOOL=1）真实 Run（线 L，2026-11-20）

性质：有界真实运行（主机 opspilot-tool-1，main a5e70d38，#666 驱动 `round --trials 1 --trace lab`，DeepSeek 真调，宿主进程拓扑，线 L 端口 30200–30999）。每 Run 只有产品默认防循环上限 100 次模型请求。不是生产证明。不含凭据：证据目录扫描 11 个凭据值 0 命中（正对照 1 命中；LANGSMITH_WORKSPACE_ID 是 trace URL 里的组织 id，不算凭据，已排除）。

- 题 94（`l-c2b-94`，`--k8s-env-file`）：驱动通过真实 web 的审核接口导入 SKILL.md 夹具 database-troubleshooting（导入账号自己提交、自己批准，得到 revision 1），Run 的目录里能看到该条目（run_catalog ok），Run 之后撤销，active_after_revoke 为空。披露 RUNBOOKS_IMPORTED_REVIEWED。
- Run `679b29cb-fae5-5137-b7fc-7549e52ce187` completed，15 次模型请求；prompt 540,892（命中 507,264），completion 19,154。fetch_skill 调用 1 次且 ok；kubernetes_jq_query 17 次 ok（产品只读身份能读到 app-94 的 Pod）；报告引用 15 条证据（K8s 与日志）。prompt_revision `prompt-replay-candidate-k8s-41f6ba6568f7`，tool_schema_revision `otel-demo-ext-70c2f18484d7`。
- trace：https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/aac4e265-60dd-447b-a3fa-5bf071e20494/r/00000000-0000-0000-d4d6-4adf32cabb54；summary：`holmes-evals-l-c2b-94/94_skill_transparency/trial-1/summary.json`。
- 核对清单（#745）：①app-94 Pod 可读 → 通过；④真实 web 上导入账号自审自批 → 通过；⑤带 --k8s-env-file → 是；②162 的写法、③96 删 CRD → 未执行。
- 判分（2026-11-20 补判：换 key 后 `driver judge`，glm-5.3-flashx，1 次尝试）：94 通过（A）。
