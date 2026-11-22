# C1 TodoWrite（OPSPILOT_TODO_WRITE=1）真实 Run（线 L，2026-11-20）

性质：有界真实运行（主机 opspilot-tool-1，main a5e70d38，#666 驱动 `round --trials 1 --trace lab`，DeepSeek 真调，宿主进程拓扑，线 L 端口 30200–30999）。每 Run 只有产品默认防循环上限 100 次模型请求。不是生产证明。不含凭据：证据目录扫描 11 个凭据值 0 命中（正对照 1 命中；LANGSMITH_WORKSPACE_ID 是 trace URL 里的组织 id，不算凭据，已排除）。

- 题 283（`l-c1-283`，按题打开）：Run `28e54202-ec68-5e7e-815f-74b0c438f60a` completed，4 次模型请求；prompt 53,805（命中 32,768），completion 4,793。工具面里有 TodoWrite（offered=true，run_tool_names 读取成功），但**模型没有调用它**（calls_ok 0）；4 次 kubernetes_jq_query 都 ok 并被引用。prompt_revision `prompt-replay-candidate-k8s-todo-38e7abad59c4`。trace：https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/8b6d79f2-aa0a-41e4-9502-001ced2d3dcd/r/00000000-0000-0000-e78d-c223eea8c9ee。namespaces_left 在 Run 前后都是 0；cluster_name 未设；history 为空，没有遇到 memories_generated。
- 补一次 280（`l-c1-todo-on-280`，`--todo on`，属于 C1-U2 hard 题，真实 ES）：Run `9b65a50e-509a-5bf0-bd2e-6ef9ac695374` completed，11 次模型请求；prompt 311,259（命中 289,664），completion 14,835。**TodoWrite 调用 3 次且 ok，最终清单 5/5 completed**，报告引用 18 条 ES 证据。trace：https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/a48d4fa8-93c7-4c9b-862f-31d4b3ca03f2/r/00000000-0000-0000-e383-a4d9383e9cf3。
- 工作台截图：用 280 那一遍的 PG 副本（trust）在 127.0.0.1:30980 起一个只读查看的 web，事故页显示 "Investigation plan · 5 of 5 done · the Agent's own task list, not evidence"，见 `c1-280-plan.png` 与 `c1-280-workbench.png`。
- summary：`holmes-evals-l-c1-283/.../summary.json`、`holmes-evals-l-c1-todo-on-280/.../summary.json`。
- 判分（2026-11-20 补判：换 key 后 `driver judge`，glm-5.3-flashx，1 次尝试）：283 不通过（B），280（--todo on）不通过（B）。
