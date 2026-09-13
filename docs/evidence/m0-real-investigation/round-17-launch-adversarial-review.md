# Round 17 round07 launcher 对抗式审查

日期：2026-09-13

审查范围：`scripts/m0_lab/round07/` 全部文件，重点为凭据处理、参数/证据绑定、失败路径记账。

- 凭据只在 packet、scenario、预算、运行时和 Docker 固定校验通过后读取；不进入 argv/environment。子进程 stdout/stderr/failure 写入 ledger 前按 key 脱敏。
- candidate HTTP 使用空 `ProxyHandler` 与拒绝重定向的 opener；endpoint 强制 HTTPS、`api.deepseek.com`、POST。upstream HTTP guard 也限制固定 host/path/method、无重试和不跟随重定向。
- packet 仅接受冻结 manifest 中的 canonical SHA256，并校验 packet scenario 与 CLI scenario；未知工具/参数/query/超额调用均在 replay 层受控拒绝。
- Holmes interpreter、checkout 和 Docker 使用固定路径及 SHA256；container image、network、mount、packet 和 runner 参数固定，拒绝调用方命令。
- ledger 预留使用 sidecar `fcntl` 锁与 `reserved_http`，覆盖 ID/cap 检查和写回；超时、异常、损坏结果均释放预留并记录失败，CLI 对失败状态返回非零。

结论：未发现 P0/P1/P2。未执行模型 HTTP、trace、PG 或容器。本审查不替代 GitHub bot review。
