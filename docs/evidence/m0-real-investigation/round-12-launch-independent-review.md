# Round 12 launcher 独立复核

日期：2026-09-13

- `reserve_run()` 使用 ledger sidecar lock，在重复 Run ID、`http_count + reserved_http` 上限检查与写回之间保持独占；完成 Run 时再次加锁并释放预留额度。
- 并发回归以两个线程竞争 1-request allocation，结果稳定为一个成功预留、一个上限拒绝，ledger 只保留一个 Run 和一个预留。
- candidate 的 `validated_tool_calls()` 拒绝非 list、非 dict 元素和非 dict `function`；`run()` 将其写成 `TOOL_PAIRING_INVALID` 的受控失败并持久化 `result-business.json`。

结论：未发现 P0/P1/P2。该复核不替代 GitHub bot review；当前提交需等待 CI 与最新 bot 覆盖。
