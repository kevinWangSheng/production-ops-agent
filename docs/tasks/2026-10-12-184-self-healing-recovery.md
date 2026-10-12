# #184 自愈故障的独立恢复确认

- 状态：合同预审中，等待用户裁决；未开工实现
- 更新日期：2026-10-12
- 依据：[#184](https://github.com/kevinWangSheng/production-ops-agent/issues/184)；C3 §4「事故与发布观察分开建模」「人工控制与结果所有权」「全局与目标级暂停」、§10「观察主体与授权」「采样与提交」；PRODUCT-CONSTRAINTS；feature_list F6（`passes` 已为 true，不得回退）；M1-02 [任务记录](2026-10-03-m1-02-recovery-observation.md)；M1-04 决定 C（resolved 告警只记录）
- 工作区：`feature/F6-184-self-healing-recovery`，`../poa-184-self-heal`

## 目标与范围

恢复观察现在只能由人在工作台登记「已处理」开启（`register_remediation`）。对会自行恢复的故障，没人点击就永远停在 `open`，值班侧分不清「仍在故障」和「早已自愈」。本切片让系统在约定时机为有 HealthProfile 的目标自动授权一次独立恢复观察，持续健康则推进到 `resolved` 并标明来源为自愈；无法确认则保持 `open` 交人。

不在范围：放宽 Observer 数据库权限；由模型认证恢复；改变「人工关闭不等于恢复」；告警 resolved 触发观察（#184 方案 B，依赖改 M1-04 决定 C，用户未要求）。

## SPEC 有界开放草案

草案文字见本分支 `SPEC.md` 末尾「2026-10-12 draft」段；用户裁决决定表后定稿，随授权 PR 合并（用户门）。

## 前提与完成条件

- 前提：用户裁决下方决定表；C3 §10 与生命周期描述按裁决修订（C3 变更属用户门）。
- 完成条件（草案）：自愈场景在 kind 实验环境真实跑通——故障注入后自愈、无人点击，事故经独立观察推进到 `resolved` 且来源标为自愈；持续故障场景到期保持 `open` 并交接；人工登记路径、F6 验收投影不回退；独立审查与独立验收测试作者按 AGENTS.md 执行。

## 合同（r0，待裁决）

预审决定表：[合同决定表](2026-10-12-184-self-healing-recovery-contract.md)（Codex GPT-6 Astra，high，只读预审）。

## 执行进展与证据

- 2026-10-12 建 worktree、任务记录、SPEC 草案；派发 Codex 合同预审。
- 实验环境：与另一会话（production-ops-agent-00）协调，同一时间只跑一个真实故障；真实 Run 前先确认。
- 迁移：线性链，当前 main 末尾 0012；对方先合并则 rebase 并重编号。

## 下一步与交接

用户裁决决定表 → 写入合同 r1 → 授权 PR（SPEC + C3 修订）→ 按步实施。
