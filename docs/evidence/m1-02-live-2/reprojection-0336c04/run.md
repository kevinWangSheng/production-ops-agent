# 第 6 步真实数据在 main `0336c04` 上的重新投影（F6 `passes` 前置，用户 2026-10-09 决定「先补真实数据投影再翻」）

- 日期：2026-10-09；分支 `feature/F6-passes`（worktree `../production-ops-agent-f6-passes`，基于 main `0336c04`，含 #161 健康窗规则 + 迁移 0006、#163 部署合同、#165 迁移 0007 结束记录保存任务身份）。
- 数据：[../run.md](../run.md) 记录的 2026-10-09 00:18–01:49Z 真实环境重跑所产生的 PostgreSQL 数据目录（当时在临时实例 55661 上，库 `f6live`，schema `0005_incident_mode`），由 lead 归档在主仓库 `tmp/m1-02-live-pg-archive/f6-window/pg55661`。本次**复制**一份到本 worktree `tmp/pg55681-archive`（原件不动），改端口 55681 启动。
- 不重新拉起 kind 实验环境、不调用模型、不查遥测：全部结论来自已存记录的重新投影与离线重放。
- 全套 PG 套件（integration + acceptance + contracts，含 0006/0007 迁移测试）先在 main `0336c04` 空库上跑过：**564 passed、10 skipped、0 xfailed**（#165 去掉了 2 个 `sample_jobs` xfail；临时实例 55681 空库，命令见 [../../../development.md](../../../development.md)「测试」）。

## 迁移（部署合同 #163：服务停止状态下 `make migrate`）

该库无任何运行时连接。`OPSPILOT_DSN=<f6live> python -m opspilot.schema migrate` → `schema upgraded: 0007_ending_job_identity`（[migrate.txt](migrate.txt)）。迁移前后：

| | 迁移前（0005） | 迁移后（0007） |
|---|---|---|
| 会话 | completed/recovery_confirmed 1、expired/max_samples_exhausted 4（无 `authorized`） | 不变 |
| 结束记录 | 5 条，`authority_revoked` 0 | 5 条，`authority_revoked` 0；新增列 `job_id`/`job_sequence` 对这 5 条**均为 NULL**（0007 只对之后写入的结束记录保存任务身份） |

0006（规则变更：结束仍 `authorized` 的会话）在此库上**没有可结束的会话**，行为是空操作，照实记录。

## 重新投影（新代码，同一数据）

每个事故：`lab_run.py summarize`（owner 登录读 `session_history` / `replay_session` / `incident_records`，Observer 登录 `obs_lab_login` 实测 `table_privileges()`，产品 `recovery_outcome` 投影）→ `summary.json`；`env -i python -m opspilot.observer.replay --incident <id>`（Observer 登录）→ `replay.json`，退出码全部 0。

| 场景 | 事故 | 生命周期 | verdict / confirmed | 健康窗 s | 交接 | 重放 CLI |
|---|---|---|---|---|---|---|
| 1 处置后恢复（shipped profile） | `0834af7e…` | resolved | healthy / true | 601 | — | consistent, healthy, 601 |
| 2 首次尝试（部分覆盖） | `d30abd62…` | open | unknown / false | 0 | handoff（MAX_SAMPLES_EXHAUSTED） | consistent, unknown |
| 2 重做：错误率下降时撤流量 | `ae2cbd44…` | open | unknown / false | 0 | handoff | consistent, unknown |
| 3 去遥测 | `cf0a71f8…` | open | unknown / false | 0 | handoff | consistent, unknown |
| 4 持续异常 | `a6c68a1d…` | open | degraded / false | 0 | handoff | consistent, degraded |

## 与原冻结摘要（[../<场景>/summary.json](../)，代码 `3bbad40`+main `fe62caa`）的差异

对五份 `summary.json` 做逐键深比较（忽略 `frozen_at`），每份**恰好 3 处**差异，且五份相同：

1. `endings[0].job_id`、`endings[0].job_sequence`：新增键，值 `null`——0007 新列，0007 之前写入的结束记录没有任务身份（预期）。
2. `observer_grants.tables.public.opspilot_observation_endings.INSERT`：列集合 8 → 10（0007 给 Observer 角色授予新列的 INSERT；`permissions` 仍为 `[read_only, human_control]`，`permissions_from_grants` 与 `OBSERVER_GRANTS` 一致）。

其余全部相等：`incident_lifecycle`、`recovery_verdict`、`recovery_confirmed`、`healthy_window_seconds`、`used_sample_count`、`recovery_reasons`、`handoff_reasons`、`observation_ended_reason`、`actions`（逐条）、`permissions`、`recovery_samples`（逐读数状态/值/哈希/判定）、`observation_sessions`、`sample_jobs`（五个事故的任务集合逐项相同——所有任务都有采样，没有「已结束且无样本」的任务，因此 #165 的呈现变化在这批数据上不出现）、`handling_audit`、`replay_session`（逐采样 `matches`）、`recovery_profile_content_sha256`。`replay.json` 与原件逐字段一致（`consistent`、`recovery_verdict`、`healthy_window_seconds`、`expected_lifecycle`、逐采样 stored/replayed）。

结论：main `0336c04` 上的在线折叠规则（#161）、重放与投影对这批真实数据给出与冻结时相同的判定、生命周期与健康窗；差异只来自 0007 的新列。归属的准确表述（独立审查措辞）：**`3bbad40` 的真实实验环境运行证据，经 `0336c04` 对同批数据重新投影与重放确认兼容**；不是 `0336c04` 新跑了一次真实环境。

## 限制

- 这是对已存记录的重新投影，不是新的真实环境运行；真实环境证据仍是 [../run.md](../run.md)（代码 `3bbad40`，与 `0336c04` 的差别为 #163 文档、#165 结束记录任务身份与 `sample_jobs` 呈现、#161 的迁移 0006——这些不改变采样与折叠规则，本次投影证实）。
- 0006 的「结束在途会话」与 0007 的「保存任务身份」在这批数据上都没有触发（无在途会话、无 0007 之后的结束记录）；两者由各自的 PG 迁移测试证明。
