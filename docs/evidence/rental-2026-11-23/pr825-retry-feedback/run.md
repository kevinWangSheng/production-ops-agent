# PR #825（#701）重试反馈列出每个 claim 的全部失败检查：真实 Run 与重放

代码同 [pr827](../pr827-k8s-empty-list/run.md)，即合并分支 `47419bca`。

## 真实 Run

- 问题：otel-demo 中 checkout Deployment 的副本、重启次数与近期事件（`q2-checkout.txt`）。
- Run `d414df91-7555-5847-815a-da2f0688366b`：completed，9 次模型请求；prompt 235,794（命中 187,648），completion 10,023。trace：https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/d9043906-3b89-466d-bd20-a0ceca5d77cf/r/00000000-0000-0000-484a-b5fe36637cee。
- 这次 Run 本身走到了重试路径。第 8 轮终答被拒，原因 REPORT_INVALID：10 个 claim 都是 `missing_target_refs`，每个 claim 只有一个失败检查。第 9 轮带着反馈重答后被接受并发布。反馈按每个 claim 一条列出，格式与 #825 一致。重放见 `/home/opspilot/rental-logs/cp1/replay-out/pr825-real-merge.jsonl`，未入库。
- 偏离：第一次冻结失败，原因是证据目录已存在（`lab_evidence.freeze` 用 `exist_ok=False`，而重放结果先写进了这个目录）。Run 已经结束，之后从该次 PG 的 trust 副本用 `scripts.run_freeze` 重新冻结，experiment 为 `cp1-pr825-real-refreeze`，副本用后已删除。summary 是 run_freeze 的形状，没有辅助脚本的 `cp1` 块。

## 重放 #701 里的两份被拒终答

- 来源是 r3 原始包 `~/rental-evidence/r3/r3-raw.tgz` 里的 ledger：S105 t2（lite-batch-r3，`6c65bcb9…`）和 S12 t2（lite-full-r3，`e5221ff1…`），sha256 与各自 summary 的 `raw_ledger.sha256` 一致。
- 方法：`/home/opspilot/rental-logs/cp1/cp1_replay.py`。它从 ledger 重建 `DurableStore.rebuild` 的快照，调用 `rebuild_transcript`，再把每个不带工具调用的 stop 回复送进 `validated_report`，调用方式与 r3 重放脚本相同。偏离：r3 的行是按 r3 的提示词计算哈希的，重建时 `_check_snapshot_hash` 报 INCOMPATIBLE_STATE，所以重放里把这项字节一致检查跳过了。校验只用到已送达的视图和证据上下文。同一脚本在 main `e199727e` 和合并分支上各跑一遍。
- 结果（只记检查码，见 `replay-701.json`；反馈原文不入库）：

| 终答 | main：每个 claim 的检查码 | 合并分支：每个 claim 的检查码 | 接受或拒绝 |
|---|---|---|---|
| S105 t2 第 15 轮 | 17 个 claim，都只有 unknown_evidence_id | 17 个 claim，都是 unknown_evidence_id 加 missing_target_refs | 两边都拒，REPORT_INVALID |
| S105 t2 第 16 轮 | 17 个 claim 都是 missing_target_refs | 相同 | 两边都拒 |
| S12 t2 第 15 轮 | 4 个 claim 都只有 unknown_evidence_id | claim 15 是 unknown_evidence_id 加 cites_non_ok_view，其余 3 个相同 | 两边都拒 |
| S12 t2 第 16 轮 | claim 15 是 cites_non_ok_view | 相同 | 两边都拒 |

- 判定：通过。两份首次终答（第 15 轮）中，此前被首个失败遮蔽、之后导致强制终答失败的检查，现在都在第一次反馈里列出。接受或拒绝的结论不变。只有一个失败的第 16 轮，反馈字节在两边相同（feedback_sha256 相等）。
