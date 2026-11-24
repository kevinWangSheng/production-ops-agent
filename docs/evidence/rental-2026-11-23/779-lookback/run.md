# #779 回看窗口（PR #820）：真实 Run

代码是 main `e199727e`。web 与 worker 都设 `OPSPILOT_MAX_LOOKBACK=30d`，tool_schema_revision 为 `otel-demo-ext-0fbad8ac5bbb`。后端是真实 ES `cp1-es`，用产品只读 key。

- 夹具：索引 `cp1-billing-logs`。2026-11-20 15:15:11Z 至 16:13:41Z 之间有 40 条 ERROR，距提交约 72 小时；最近 1 小时内有 20 条 INFO。用完已删除。
- 问题：查 2026-11-20 15:15 到 16:15 UTC 的 billing ERROR（`q4-lookback.txt`）。两次 Run 问题相同，区别只在提交表单：
  - `run-30d`：表单 `lookback=30d`。Run `06967f3e-4d42-5388-9248-d3fee24c65bf`，completed，6 次模型请求；prompt 155,177（命中 129,920），completion 7,305。授权窗口为 2026-10-24T15:17:07Z 到 2026-11-23T15:17:07Z。被引用的 elasticsearch_search 视图状态 ok，窗口为 2026-11-20 15:15 到 16:15，返回 40 条。报告给出 40 条 ERROR 和失败内容。trace：https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/d9043906-3b89-466d-bd20-a0ceca5d77cf/r/00000000-0000-0000-4e30-b0256784b41c。
  - `run-default-24h`：表单不带 lookback，按产品默认 24 小时处理，部署上限仍是 30 天。Run `e5a4db30-61ba-5d08-9e5f-5bf0c696be35`，completed，4 次模型请求；prompt 74,940（命中 62,080），completion 3,793。授权窗口为 2026-11-22T15:17:07Z 到 2026-11-23T15:17:07Z。查 11-20 的那次 elasticsearch_search 被拒，INVALID_PARAMS / QUERY_OUT_OF_WINDOW，ledger 里有 3 处。报告说明该窗口超出授权，没有读到那天的文档。trace：https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/d9043906-3b89-466d-bd20-a0ceca5d77cf/r/00000000-0000-0000-15a7-661c56fc6a9a。
- 判定：两项都通过。
