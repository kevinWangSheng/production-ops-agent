# Round 10 launcher 独立复核

日期：2026-09-13

- candidate/upstream arm 现在必须使用冻结 packet 根目录内的显式 `--packet`，并拒绝额外 `runner_args`，不能覆盖可信 packet。
- upstream checkout 通过 `--holmes-root`/`M0_HOLMES_ROOT` 传入并要求存在 `holmes/` 子目录；不再依赖开发者绝对路径。
- candidate 对 `tool_calls` 先校验为 list 且元素为 dict；畸形响应写入受控失败结果，不让异常逃逸丢失审计工件。
- container arm 仍固定 digest image、网络、mount 与 runner 参数；所有拒绝检查发生在 `read_key()` 之前。

结论：未发现 P0/P1/P2。该复核不替代 GitHub bot review；当前变更仍需 bot 覆盖最终 HEAD。
