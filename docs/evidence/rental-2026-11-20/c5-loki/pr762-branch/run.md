# PR #762 分支验证：C5 Loki 101（线 L，2026-11-20）

**在 PR 分支上验证，不是 main**：worktree /home/opspilot/poa-762，HEAD a6052971（fix/holmes-loki-shift-span），用该提交自带的驱动跑 `round --questions 101 --loki gateway --k8s-env-file --trials 1 --trace lab`，judge 开启，端口 32000 段。

- 结果：**仍然失败，没有产生 Run**。3 次机会都报 SHIFT_IO_FAILED，但错误已经换成 "GET /loki/api/v1/query_range TimeoutError"（第 1 次在 21:49:43Z，第 2 次 21:52:05Z，第 3 次 21:54:24Z），归类 env_broken。原来的 labels 400 不再出现，说明分段读取解决了 labels 的跨度问题。
- 平移耗时：拿不到完整数字，因为平移没有跑完。每次机会从 port-forward 就绪到失败大约 60 秒（例如 chance-1：port-forward 日志 21:48:30，after_test 21:49:30），和 `LOKI_TIMEOUT_S = 60` 一致，说明是某一次 query_range 单独用满了 60 秒。
- 对照：单独起一个空的 grafana/loki:2.9.0，一个 30 天窗口的 query_range（`{job=~".+"}`，limit 5000，forward）耗时 1.6 秒（730 天前和 400 天前各测一次，都返回 200）。所以变慢和题目夹具里的 Loki（kind 内的部署和 2025-08 的数据）有关，具体是哪个窗口或哪一页，需要修复者加上按窗口的计时才能定位【未确认】。
- summary：`holmes-evals-l-pr762-loki-101/101_loki_historical_logs_pod_deleted/trial-1/summary.json`（status missing）。
