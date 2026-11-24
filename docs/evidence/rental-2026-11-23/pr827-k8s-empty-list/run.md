# PR #827（#679）空 K8s 列表：真实 Run

代码是本地合并分支 `local/cp1-merge-2026-11-23` `47419bca`（main e199727e + #827 + #825 + #826），tool_schema_revision 为 `otel-demo-ext-f94273f95547`。

- 夹具：新建命名空间 `cp1-np-empty`，里面只有一个 nginx Deployment 和 Service，没有 NetworkPolicy。工程侧 `kubectl get networkpolicies -A` 的结果是 No resources found。
- 问题：问 `cp1-np-empty` 现在有没有 NetworkPolicy，并要求引用所执行的查询（`/home/opspilot/rental-logs/cp1/q/q1-netpol.txt`）。
- Run `01c47189-de6a-5072-87c2-7ab09f9df94c`：completed，4 次模型请求；prompt 61,751（命中 52,224），completion 2,776。trace：https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/d9043906-3b89-466d-bd20-a0ceca5d77cf/r/00000000-0000-0000-d679-2ecb398caf29。
- 判定：通过。报告通过校验并已发布（completed，conclusion supported）。第 1 条 fact 引用 `fedd703d…-t0`：kubernetes_jq_query，kind networkpolicies，scope.namespace `cp1-np-empty`，status ok，`total_items_processed: 0`，tool_version v2，citable_as_fact true。同时引用了一次全集群的 networkpolicies 读取（`45440afc…-t0`），同样 ok、0 条。view 的 `cannot_prove` 带有 #679 的那句限定。
- summary：`summary.json`。原始 ledger 在 `/home/opspilot/poa-cp1/tmp/lab-ledgers/cp1-pr827-netpol/`，未入库。
