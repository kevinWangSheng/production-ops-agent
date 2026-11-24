# #808 ES 集群只读工具（PR #823）：真实 Run

代码是 main `e199727e`，后端是真实 ES `cp1-es`（单节点），用产品只读 key。

- 夹具：索引 `cp1-orders`，1 个分片、1 个副本。单节点上副本无法分配，集群为 yellow。用完已删除。
- 问题：集群不是 green，要求用集群工具查状态、哪个索引有未分配分片及原因、节点资源（`q6-es-cluster.txt`）。
- Run `c0bf2ac2-3e58-505d-a948-4a69d173fda3`：completed，3 次模型请求；prompt 61,561（命中 52,480），completion 4,874。trace：https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/d9043906-3b89-466d-bd20-a0ceca5d77cf/r/00000000-0000-0000-5c5b-e5887d3cec43。
- 工具调用全部 ok：elasticsearch_cluster_health（`level: indices`）、elasticsearch_allocation_explain 2 次（一次不带参数，一次 `index: cp1-orders, shard: 0, primary: false`）、elasticsearch_nodes_stats、elasticsearch_cat 2 次（`api: nodes`、`api: shards`，参数名是 `api`），另有 elasticsearch_list_indices。5 个集群工具里用到了 4 个，没有用到 elasticsearch_index_stats。报告引用了这些视图。
- 第 7 项的驱动冒烟（题 183a）也调用了 elasticsearch_cluster_health 和 elasticsearch_cat（`api: nodes`），都是 ok，见 [809-driver-smoke](../809-driver-smoke/run.md)。
- 判定：通过。
