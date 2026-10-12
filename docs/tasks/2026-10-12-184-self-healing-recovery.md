# #184 自愈故障的独立恢复确认

- 状态：合同 r1 已裁决（2026-10-12）；授权 PR 待用户合并；实现未开工
- 更新日期：2026-10-12
- 依据：[#184](https://github.com/kevinWangSheng/production-ops-agent/issues/184)；C3 §4「事故与发布观察分开建模」「人工控制与结果所有权」「全局与目标级暂停」、§10「观察主体与授权」「采样与提交」；PRODUCT-CONSTRAINTS；feature_list F6（`passes` 已为 true，不得回退）；M1-02 [任务记录](2026-10-03-m1-02-recovery-observation.md)；M1-04 决定 C（resolved 告警只记录）
- 工作区：授权 PR `chore/184-self-heal-gate`，`../poa-184-self-heal`；实施每步一个 worktree，写在执行节

## 目标与范围

恢复观察现在只能由人在工作台登记「已处理」开启（`register_remediation`）。对会自行恢复的故障，没人点击就永远停在 `open`，值班侧分不清「仍在故障」和「早已自愈」。本切片让系统在约定时机为有 HealthProfile 的目标自动授权一次独立恢复观察，持续健康则推进到 `resolved` 并标明来源为自愈；无法确认则保持 `open` 交人。

不在范围：放宽 Observer 数据库权限；由模型认证恢复；改变「人工关闭不等于恢复」；告警 resolved 触发观察（#184 方案 B，依赖改 M1-04 决定 C，用户未要求）。

## SPEC 有界开放草案

草案文字见本分支 `SPEC.md` 末尾「2026-10-12 draft」段；用户裁决决定表后定稿，随授权 PR 合并（用户门）。

## 前提与完成条件

- 前提：用户裁决下方决定表；C3 §10 与生命周期描述按裁决修订（C3 变更属用户门）。
- 完成条件（草案）：自愈场景在 kind 实验环境真实跑通——故障注入后自愈、无人点击，事故经独立观察推进到 `resolved` 且来源标为自愈；持续故障场景到期保持 `open` 并交接；人工登记路径、F6 验收投影不回退；独立审查与独立验收测试作者按 AGENTS.md 执行。

## 合同（r1）

用户 2026-10-12 按推荐裁决 A1–A11；B1–B5 按推荐作可逆默认。完整表见[合同决定表](2026-10-12-184-self-healing-recovery-contract.md)（Codex GPT-6 Astra，high，只读预审）。

- **A1** 纳入 M1，仅限有 HealthProfile 的 `incident_recovery`。
- **A2** 只在合格结论发布后自动开观察；交接不发布（ADR-0005），交接路径不触发。
- **A3** 由 Controller 侧可重放任务授权，调用与人工登记相同的事务原语；worker 不写观察表。
- **A4** 会话持久 `authorization_source = post_remediation | self_healing`（下一个迁移）。
- **A5** 审计 actor 为 `system`，来源记 `self_healing`；不新增 DB 角色，DB current_user 仍是 Controller/web 角色。
- **A6** 观察起点为授权事务的 DB 时钟；HealthProfile 参数不变，不延长 deadline。
- **A7** 事故行锁 + 代际决胜，先提交者胜；人工登记、接管、暂停、取消撤销自动会话。
- **A8** 无 HealthProfile 的目标保持现状，只能人工处理。
- **A9** 保留 M1-04 决定 C；resolved 告警只作工作台提示，不触发观察。
- **A10** 不改写旧证据；旧会话兼容读作 `post_remediation`，新会话必须显式来源。
- **A11** 复用现有 Controller/web 观察写权限，不给 worker 扩权。
- B1 幂等键 `incident_id + control_generation + trigger=published`；B2 参数全部取自已加载 HealthProfile；B3 有界重试，失败写可见交接原因、事故保持 `open`；B4 实验用 harness 定时 `fault restore`，产品侧无人点击；B5 工作台/F13 展示来源。

## 执行进展与证据

- 2026-10-12 建 worktree、任务记录、SPEC 草案；Codex 合同预审完成；用户按推荐裁决 r1，F11 排在本任务之后。
- 实验环境：与另一会话（production-ops-agent-00）协调，同一时间只跑一个真实故障；真实 Run 前先确认。
- 迁移：线性链，当前 main 末尾 0012；对方先合并则 rebase 并重编号。

## 下一步与交接

用户裁决决定表 → 写入合同 r1 → 授权 PR（SPEC + C3 修订）→ 按步实施。
