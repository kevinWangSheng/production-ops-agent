# 流程审计与指令调整（2026-09-21）

- 状态：进行中（PR 待用户审核）
- 更新日期：2026-09-21
- 依据：用户 2026-09-21 决定：取消费用逐次审批；开发红线按事实删除；PR 体量按「一个合同条款」不设数字；授权修改 AGENTS.md 并提交 PR。功能 PR 是否纳入预授权自动合并未决，本 PR 保持用户门。
- 工作区：`../production-ops-agent-process-v2`，分支 `chore/process-audit-v2`

## 目标与范围

把三周运行日志审计出的流程问题改进版本管理内容：AGENTS.md 重写、SPEC 门槛与费用段收拢、ROADMAP 改为状态表、CI 覆盖所有 PR、删除 `.Codex` 遗留。不改产品代码、PRODUCT-CONSTRAINTS、验收步骤或 `passes`。

## 审计依据（已核查事实）

| 事实 | 数值 |
|---|---|
| PR #20 审查 thread / 其中 09-19「最终态」后新增 | 81 / 61 |
| open feature 分支 fix/bot 类提交占比 | 约 50% |
| 指向 integration 分支的 PR（#31 #32 #33）CI checks | 0（workflow 只触发 `main`/`chore/m0-*`） |
| 已合并分支未清理的 worktree / 本地 main 落后 | 7（#20 合并后）/ 82 提交 |
| 每任务固定阅读量（AGENTS.md:29 到 31） | 114KB |
| 全项目真实模型花费（估算，未对账） | 约 1 到 3 CNY，对比 1,000 CNY 参考预算 |
| 费用/上传/重跑规则实际触发 | 唯一反复触发的开发红线组；其余红线零触发且与宿主全局规则重复 |
| M1-01 阶段真实 Run 次数 vs 待合并代码量 | 个位数 vs 约 30,000 行 |
| Codex-progress.txt 最后条目 | 2026-09-16，此后交接只在会话 scratchpad |

## 变更

- `AGENTS.md`：14.6KB 重写为约 5KB。删开发红线段（宿主全局规则已覆盖）、费用审批、Codex-progress.txt、`.Codex` 遗留段；接手压成两步加合并后清单；PR 就绪改为 CI + 独立审查 + 一次机器人分诊；禁止 stacked PR；定义预授权自动合并类与用户门；任务记录两页上限；核心路径 PR 附真实 Run。
- `SPEC.md`：三处过期「gate remains not cleared」标为历史；费用段改为常设授权加技术上限；账单对账改为余额差记账。
- `ROADMAP.md`：只保留状态表、交付顺序与已确认决策；原文逐字归档到 `docs/archive/roadmap-history-2026-09-21.md`。
- `.github/workflows/ci.yml`：`pull_request` 去掉 base 分支过滤。`.github/pull_request_template.md` 缩为三段。
- 删除 `.Codex/rules/`（4 个指针）与 `.Codex/skills/`（10 个旧 skills）；`docs/README.md`、`docs/development.md`、`docs/tasks/README.md` 同步。

## 验证

- `make check`：见 PR 描述。
- Markdown 链接检查：见 PR 描述。
- 独立审查：全新上下文 Agent 核对 diff，12 条发现（2 P1、5 P2、5 P3）全部采纳修复：功能 PR 收回到用户门、squash 改为合并时执行、就绪顺序明确、SPEC 与 live.py 批准文件口径对齐、验收步骤不削弱条款恢复、红线三项动作保留一句、锚点与开发指南同步。

## 待决与下一步

- 本 PR 合并后：同步各 worktree，删已合并分支的 worktree，#31/#32/#33 retarget 到 `main`，#29/#21/#37 rebase，关闭 4 个 `integration/m1-01-full*` 变体。
- 把 09-17 简报改为常设「系统现状」页，每次合并后更新。
- 验收 harness 与 `feature_list.json` 步骤接线，另立任务。
