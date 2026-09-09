# PR 机器人审查强制门禁

- 状态：进行中
- 日期：2026-09-09
- 工作区：`/Users/shenghuikevin/dev/AI/production-ops-agent-review-gate`，`chore/pr-review-gate`，起点 `ad99e6a`
- 依据：用户本轮明确要求以代码和配置强制执行机器人审查闭环；[AGENTS](../../AGENTS.md)、[SPEC](../../SPEC.md) 开发权限与交付边界。

## 目标与范围

将 GitHub 机器人审查当前提交完成、发现处置纳入 main 的必需检查。保留用户最后审核合并，不触及 OpsPilot 产品能力、真实模型/trace 实验、产品验收和部署。保护配置由本次授权涵盖；本 PR 合并仍待用户审核。

## 前提与完成条件

先独立审查设计，再实现及反例测试、独立实现复验、任务 PR 与最新 CI/机器人复审。部署主线受信工作流后才可激活相应必需状态并验证阻断；不得把 PR 上新增但尚未运行的工作流称为已生效。既有保护回读并保留。

需验证：未审/运行中/失败/格式未知拒绝；旧提交拒绝；全部讨论分页及 unresolved/outdated 拒绝；身份伪造拒绝；HEAD 更新及并发安全；API 故障拒绝；修复重推必须复审；正常完成可以就绪。只读检查不得暴露 token，写权限工作流不得执行 PR 代码。

## 已核查事实与设计准备

- main 必需 checks 仅 `checks`，strict/admin/conversation-resolution 已启用；`m0-postgres` 未必需，审批人数0。没有 ruleset。
- #11 于08:39:45Z合并，机器人08:42:22Z才提交审查，存在未解决发现。
- bot REST身份 `chatgpt-codex-connector[bot]` / ID199175422 / Bot；自动审查汇总含 full headSha 与 Code/Security 行。#12 自动汇总仍覆盖3309daf，后续手动review没有同步该汇总；不能把旧汇总当新提交通过。
- 正在核对仓库级自动触发设置和机器可读完成证据；未修改远程配置，未触发新审查。

## 进展及下一步

独立设计审查进行中。确认可可靠绑定提交的机器人完成信号后实现最小门禁。记录实际证据与局限，禁止以 reactions 或无评论单独判通过。

## 实现进展

- 本仓库Codex Review all PRs保持启用，Review trigger由On PR open改为On every push，UI回读确认；credits未开启，个人默认未改。
- 独立设计发现与处置见[报告](../evidence/pr-review-gate/design-review.md)。门禁信任仓库写作者，不宣称抵御恶意有写权限workflow或持续原子失效。
- 实现及激活顺序见[开发说明](../development-review-gate.md)。37项定向测试通过；独立实现复验进行中。main工作流及必需review-gate尚未部署/激活。

- `make check`首次完整验证：227 passed、13个本地PostgreSQL显式opt-in测试跳过；本任务不修改数据库机制，CI继续跑合成PG集成。真实GitHub只读`--pr 12`返回UNRESOLVED_THREADS，未发布任何状态。
- 独立实现审查发现commit status每SHA/context 1000条上限，轮询可能耗尽。已改为同SHA单一GitHub Actions check-run首次创建/后续PATCH，权限改checks:write；1001轮合成回归与重复check拒绝通过，40项定向测试通过，独立复验继续。

## 当前交付边界

本地实现与独立复验完成，仍待任务PR的CI/机器人审查、用户审核合并及主线实际发布/激活。独立报告见[实现审查](../evidence/pr-review-gate/independent-review.md)：状态历史上限P1已修复，1003轮独立合成验证仅创建一个check-run，失败/恢复可更新。

main保护已将现有m0-postgres加入必需检查，API回读确认绑定Actions app15368，全部其他保护保持原值；见[前值](../evidence/pr-review-gate/protection-before.json)及[当前值](../evidence/pr-review-gate/protection-ci-required.json)。review-gate尚未激活。

本任务后续：审核合并PR后，按[首次启用步骤](../development-review-gate.md#首次启用及恢复)验证真实check创建、跨运行更新、撤绿/恢复、HEAD绑定与服务器阻断，再追加review-gate必需检查。检查及review动态结果保留在本任务PR；不将未执行主线步骤勾为完成。本任务不合并#12或修改其工作区；任务worktree保留，未启动后台服务。
