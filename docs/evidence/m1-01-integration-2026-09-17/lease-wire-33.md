# 租约续期接线 7a（PR #33 `feature/m1-01-progress-ui`）执行报告

- 日期：2026-09-17
- 工作区：`/Users/shenghuikevin/dev/AI/production-ops-agent-m1-progress-ui`；接手先 `git pull --ff-only`（吸收另一 agent 的 `eec8dca`/`da3537d` settle_budget 转发）。
- PR：https://github.com/kevinWangSheng/production-ops-agent/pull/33（描述已追加「租约续期接线」小节，写明 **#35 先合**）
- 未向调度者提问；费用 0（无模型调用）；未 force-push/rebase/amend/reset/stash；未碰 main；未合并；worktree 干净；PG lab 已 stop。

## 1. 提交（普通 push）

| sha | 内容 |
|---|---|
| `8d3277d` | `IncidentStore.renew_lease` 可选能力（`DurableIncidentStore` 以 `getattr(store, "renew_lease")` 转发，缺失返回 None）；`_EmittingCommitter._renew()` 在 `reserve_budget`/`settle_budget`/`begin_round`/`assert_current`/`commit_step`/`commit_tool` 之前续期；`LEASE_SECONDS = int(MODEL_REQUEST_TIMEOUT_SECONDS) + 60`（420，取自 `opspilot/investigation/limits.py` 常量，不硬编码）；`run_once` 默认短租约；内存替身 `MemoryIncidentStore.renew_lease` 镜像 #35 栅栏（running / owner / epoch / 事故代际 / lease_until 非空未过期 / 未过 deadline；`LEAST(GREATEST(...), deadline)`）；4 条确定性测试 + 2 条 PG 测试（无 #35 时 skip） |
| `4609aa6` | 审查 P2-1：`IncidentStore.renewal_supported`；能力缺失时 `run_once` 保持 Run wall 租约（行为与合并前完全一致）；测试改为可判别（捕获 claim 时租约、续期计数）；`monkeypatch`；`LEASE_SECONDS` 注释记录 per-socket 超时 caveat |
| `f6decd6` | 任务记录 + 无能力测试改用 `monkeypatch` |

**续期被拒语义**：`_renew()` 遇 `CONTROL_DENIED` 不截断而是继续转发，由存储层同一栅栏拒绝并（`commit_step`/`commit_tool`）把迟到结果记为 `late_result` 历史；其它 `PersistenceError` 转 `StepStoreError` 停机。与现有 `_lease_revoked` 语义一致。`publish`/`abandon` 由 `run_once` 直接调用，自身受栅栏，且 `publish` 紧随最后一次续期后的 `commit_step`。

## 2. 测试

确定性（`tests/test_m1_web_workbench.py`，27 passed）：
- `test_default_lease_is_short_so_takeover_after_a_hard_kill_waits_lease_not_wall`：claim 时租约 == now + 420 s（< run wall）；租约内接手被静默拒绝；420 s 后以 epoch 2 接手完成。
- `test_without_the_capability_the_lease_spans_the_run_wall_as_before`：能力缺失时租约 == now + run_seconds。
- `test_renewal_before_each_commit_keeps_a_long_attempt_alive`：两轮各 390 s 的模型调用，续期 ≥ 3 次后完成；续期不生效的存储上同一尝试被自身租约栅栏（`CONTROL_DENIED`）。
- `test_a_refused_renewal_hands_off_and_keeps_the_late_result_as_history`：运行中暂停后续期计数不再增长，迟到最终步骤记为 `late_result`，`run_handoff`，租约释放。

PG（`tests/integration/test_m1_web_postgres.py`，`hasattr(DurableStore, "renew_lease")` 门控）：
- `test_run_once_renews_a_short_lease_before_each_commit_on_postgres`：4 s 租约、两轮各 2.5 s 真实等待，观察到 round 2 的 `lease_until` 晚于 round 1，完成且 owner 释放。
- `test_a_refused_renewal_on_postgres_hands_off_with_history`。

## 3. make check / PG 结论行

```
make check (f6decd6)                     -> All checks passed! / mypy Success / 1471 passed, 99 skipped, 2 xfailed
PG 本 base（无 #35）                      -> tests/integration/test_m1_web_postgres.py: 2 passed, 2 skipped (needs PR #35 renew_lease)
PG 临时合并（本分支 + origin/fix/lease-renewal，git merge --no-commit，仅 ROADMAP.md 文本冲突，代码 0 冲突）
  test_m1_web_postgres.py + test_m1_lease_renewal_postgres.py -> 21 passed
  全部 tests/integration (M0_B_POSTGRES=1 M1_DURABLE_POSTGRES=1)  -> 78 passed, 38 skipped
  合并已 git merge --abort，临时分支已删除，未提交、未推送
```

## 4. 独立审查（全新上下文子代理，只读）

第一轮：无 P1；1 P2（无能力时 420 s 默认让 >420 s 的真实尝试自我栅栏）→ 采纳 `renewal_supported` 回退；P3：urllib 超时为 per-socket 非 wall（既有 client 属性，已记入注释）、kill/refused 两条测试在回退下仍通过（已改为可判别，审查者用两种回退复验各失败 2 条）、class-level monkeypatch（已改）、内存 vs PG 栅栏仅有不可达差异（无动作）。审查者另行把 #35 的 `renew_lease` 在 scratchpad 里嫁接到 DurableStore，本分支的 2 条 PG 门控测试通过。
第二轮：全部 VERIFIED，剩一处 cosmetic（已在 `f6decd6` 改为 `monkeypatch`）。

## 5. CI

- run [35254466157](https://github.com/kevinWangSheng/production-ops-agent/actions/runs/35254466157)（`8d3277d`）success；
- run [35255237490](https://github.com/kevinWangSheng/production-ops-agent/actions/runs/35255237490)（`4609aa6`）success；
- run [35255509110](https://github.com/kevinWangSheng/production-ops-agent/actions/runs/35255509110)（最终 HEAD `f6decd6`）`checks` + `m0-postgres` success。
- PR #33 `mergeStateStatus=CLEAN`、`mergeable=MERGEABLE`。

## 6. 依赖顺序与交给其它 PR

- **#35 先合并**：合并后 `DurableIncidentStore.renewal_supported` 自动为真，`run_once` 切到 420 s 短租约并逐次续期；#33 先合亦安全（原行为）。
- #35 与本分支代码 0 冲突，仅 `ROADMAP.md` 状态行冲突（各自新增的一行，合并时任选保留两行）。
- client（PR #29）：`MODEL_REQUEST_TIMEOUT_SECONDS` 是 per-socket-operation 超时，慢速滴流响应可超过 420 s 租约；建议 #29/#35 后续加 wall clamp 或后台心跳续期（lease.md §2 已列为后续选项）。
- smoke 路径 6 复验（去掉 `lease_seconds=20`）需在 #35 合并后的集成分支上进行；本次未跑（本分支无 `renew_lease`，默认仍为 Run wall）。

## 需用户决定

无。
