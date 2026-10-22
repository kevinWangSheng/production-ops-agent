# #304 强制最终轮重建漏 retry_feedback

- 状态：r1 已定，实现与独立验收测试进行中
- 更新日期：2026-10-22
- 依据：issue #304；C3 §7（人工控制与恢复）；[合同 r1](2026-10-22-304-forced-final-rebuild-contract.md)
- 工作区：实现 `/Users/shenghuikevin/dev/AI/poa-304`（`fix/304-forced-final-rebuild`）；独立验收测试 `/Users/shenghuikevin/dev/AI/poa-304-tests`（`fix/304-forced-final-rebuild-tests`）

## 目标

强制最终报告轮（前一轮无工具调用且报告未通过校验，`loop.py:787-798`）发出的消息为指令、retry_feedback、coverage（`loop.py:132-145`），重建（`context.py:1218-1227`）漏了 retry_feedback，`context.py:1229` 报 `INCOMPATIBLE_STATE`。影响：REPORT_INVALID 交接后 follow_up 被挡；强制最终轮报告合格但写 conclusion 前崩溃，重启后同样被挡。#59（`a3efdfa`）引入。

## 合同（r1）

预审：全新上下文 Claude Opus 子 Agent，只读，基线 `a7b296a`；根因经定向单测复现（4 例）。

- D-U1 A：重建时用上一轮已提交回复重算 retry_feedback，与 live 共用 `reports.py` 抽出的纯函数；不改数据合同、不 bump revision，旧行免迁移可恢复。（按推荐，用户 2026-10-17 常设指示；A 不改数据合同，非合同层取舍）
- D-U2：真实证据两部分：① 复制 `poa-f2-c/tmp/pg-f2c-live` 起临时 PG，对 Run `1b21375d` 只读重建，修复前 `INCOMPATIBLE_STATE`、修复后通过；② kind 最小真实 Run：不注入故障，单 Run 交接后一次 follow_up，同一 Run 恢复并调用模型，附 trace 与 summary.json。C4-A 断言 2 仍由 F2 PR-E 验证。（按推荐）
- D-R1–D-R7 可逆默认见合同第 4 节；D-R7：D-U2① 完成前保留 poa-f2-c worktree。

## 执行进展与证据

## 待决

无。
