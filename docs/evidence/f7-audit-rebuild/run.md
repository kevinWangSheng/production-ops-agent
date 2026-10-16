# F7 第 5 步：真实库审计重建（R3）

- 时间：2026-10-16T16:22Z。离线，不连 kind、不调模型，无费用。
- 数据：F12 live 库（[F12 真实运行](https://github.com/kevinWangSheng/production-ops-agent/pull/257) 的 PG，复制自 M1 干净重跑库后加 F12 场景）。用户 2026-10-16 批准复制；协调者复制到本 worktree `tmp/f12-live-pg`，原件 `production-ops-agent/tmp/archive/f12-live/live/pg` 未启动。副本以 `default_transaction_read_only=on` 在 55596 启动，schema head `0017_web_suspension_grants`，跑完已停。
- 命令：`OPSPILOT_LAB_DSN=<55596> .venv/bin/python scripts/f7_audit_rebuild.py --out docs/evidence/f7-audit-rebuild/summary.json <4 个事故>`（投影在只读事务内运行）。执行提交 `f8fefe9`，工作区干净。
- 冻结摘要：[summary.json](summary.json)，sha256 `ec156a0c…696086dd3`；只含计数、首末时间、缺失类别、条目 sha256，不含原始 payload。gitleaks 0 命中。

| 事故 | 来源 | input | query | evidence | state_change | human_correction | recovery_observation | postmortem_review | missing | undated |
|---|---|---|---|---|---|---|---|---|---|---|
| `002c3fba` | M1 干净重跑阶段 A（恢复确认、复盘批准） | 8 | 14 | 13 | 28 | 1 | 18 | 7 | 无 | 无 |
| `ae7a0387` | 干净重跑阶段 B（追问） | 17 | 26 | 24 | 50 | 2 | 6 | 0 | postmortem_review | 无 |
| `e1ac6e74` | F12 场景 A（纠正、暂停、接管、关闭/重开） | 27 | 54 | 50 | 109 | 14 | 8 | 4 | 无 | input_rounds 2 |
| `a7eeac26` | F12 场景 B | 8 | 18 | 16 | 35 | 4 | 2 | 0 | postmortem_review | input_rounds 1 |

## 判据结论

`002c3fba` 与 `e1ac6e74` 各自从审计记录重建出全部七类（输入、查询、证据、状态变化、人工纠正、恢复观察、复盘审核）；另两个事故没有复盘，按 R2 显式列为缺失而不报错。`undated` 是找不到数据库时间锚的输入轮次，计数可见、未丢弃。

保留：postmortem_review 的计数包含 `opspilot_postmortem_versions`、`opspilot_f13_audit` 行，取决于待决 D1；条目内同一事务的先后取决于待决 D2（见[任务记录](../../tasks/2026-10-16-f7-authz-audit.md)）。D1 选 b 时需按新实现重跑本摘要。
