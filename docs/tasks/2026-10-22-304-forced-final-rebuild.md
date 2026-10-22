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

- 实现（D-R1–D-R5）：`reports.validated_report` 纯函数，`InvestigationLoop._validated_report` 改为调用它（函数体除参数改名外与 a7b296a 逐行相同）；`rebuild_transcript` 对同代际、无工具、非最终、无 rejected_plan 的模型行用该函数重算，不合格时把 feedback 带到下一条模型行（跨 accepted 压缩行与 conclusion 行，未接受的压缩行清空），`_check_snapshot_hash` 按 instruction → feedback → coverage 插入；不 bump revision。
- 本地验证：自测 `tests/test_304_forced_final_rebuild_impl.py` 8 例（含压缩跨越、finish_reason 篡改仍 `INCOMPATIBLE_STATE`、代际不符仍阻塞），去掉 context.py 修复时 4 例失败；相关单测文件共 352 passed + alignment C 20 passed。临时 PG 17.9（55714，自建自停）：`tests/integration/test_304_forced_final_rebuild_impl_postgres.py` 2 例 + resume/309 PG 文件共 34 passed，去修复时 2 例均为 `blocked(INCOMPATIBLE_STATE)`。ruff check/format、mypy（三个改动模块）通过。D-U2 真实证据未做。
- D-U2 ① 已完成（[离线重建](../evidence/304-forced-final-rebuild/offline-rebuild.md)）：pg-f2c-live 副本上只读读取 1b21375d，确认 round-8 是无工具、非 final、`REPORT_INVALID` 的回复，round-9 是强制最终轮；main a7b296a 在 round-9 报 `INCOMPATIBLE_STATE`，修复 8af441e 能重建整个 Run，round-9 带 feedback 重算的哈希与库里记录一致。副本和临时 PG 已删除、已停止。D-U2 ② 未做。
- D-U2 ② 未完成（[kind 运行](../evidence/304-forced-final-rebuild/live/run.md)）：kind 上 3 次自然真实 Run（f3dd8fc4、ca946e79、c9f7f63d，`m1_live_runner --follow-up`，不注入故障）都合格发布，没有 `REPORT_INVALID` 交接，follow_up 未执行；按约定 3 次后停止，未改代码或提示硬凑。kind 已 release，临时 PG 55741 已停。
- 附带：ca946e79、c9f7f63d 自然走了强制最终轮且合格发布；对其真实行只读重建，main a7b296a 均 `INCOMPATIBLE_STATE`，修复 2fc953b 均通过（崩溃变体的重建一侧，未走 worker 重新领取）。费用：余额差 0.32 CNY（上界）。

- 决定（2026-10-22，顾问 Codex gpt-6-sol，选 a）：D-U2 ② 未达成——3 次自然真实 Run 均合格发布、无 REPORT_INVALID 交接，follow_up 未执行；以 ① 离线重建 + 3 个真实 Run（trace、summary.json）+ 其中 2 个强制最终轮的离线重建（main INCOMPATIBLE_STATE、修复通过）+ PG 集成测试作为 #304 真实证据；follow_up 真实路径由 F2 PR-E 在 kind 上验证（C4-A 断言 2，F2 翻 passes 前提）。理由：现有证据已覆盖修复点，再碰运气概率低，改 runner 是新取舍。

## 待决

无。
