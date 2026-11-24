# #696 managedFields 摘要（PR #822）：真实 Run

代码是 main `e199727e`。

- 夹具：命名空间 `cp1-mf`。ConfigMap `app-config` 先由 `kubectl create` 创建（管理者 kubectl-create，15:17:52Z），65 秒后由 `kubectl patch --field-manager=cp1-release-bot` 修改（15:18:57Z）。用完已删除。
- 问题：问 app-config 的当前设置、最后由哪个管理者在何时修改、由谁创建（`q5-managed.txt`）。
- Run `060637a5-b20a-571c-ac8c-4d317169710e`：completed，4 次模型请求；prompt 68,566（命中 61,568），completion 4,075。trace：https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/d9043906-3b89-466d-bd20-a0ceca5d77cf/r/00000000-0000-0000-2ea5-a3dd6412f988。
- 判定：通过。被引用的 kubernetes_jq_query 视图（ok）里有 `managedFieldsSummary`，两条，分别是 kubectl-create Update 15:17:52Z 和 cp1-release-bot Update 15:18:57Z，`managedFieldsSummaryDropped` 为 null。报告据此说明修改方与时间，并引用了 view 的限定：只有每个管理者最后一次写入的时间，不说明改了哪些字段。
