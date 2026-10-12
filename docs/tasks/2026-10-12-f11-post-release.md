# F11 发布后观察与调查

- 状态：合同预审中，等待用户裁决；未开工实现
- 更新日期：2026-10-12
- 依据：[#207](https://github.com/kevinWangSheng/production-ops-agent/issues/207)；feature_list F11 四步；C3 §1（第二个入口）、§4「事故与发布观察分开建模」「人工控制与结果所有权」、§6、§9（发布观察页面与操作）、§10「观察主体与授权」「采样与提交」中发布观察条款、§12「实验环境」（发布实验变更镜像或配置，不重建 chart）；PRODUCT-CONSTRAINTS（不批准、不阻断、不执行发布）；ROADMAP M2
- 工作区：`feature/F11-post-release`，`../poa-f11-post-release`

## 目标与范围

第二个入口：观察到一次发布（滚动更新）后，为该发布建立独立的 `ReleaseObservation`（不需要先有事故），由确定性 Observer 在固定期限内按 HealthProfile 采样，覆盖「先健康后变坏」的延迟回归。结论为 healthy / anomalous / unknown（或 cancelled）。异常时幂等创建或关联事故，并以发布前后版本、新旧实例证据启动独立调查。结果只作建议，不改变任何 CI 检查、部署门、rollout 或流量策略。

C3 §14 把 F11 排在 M2；本切片是提前开放。F11 `passes` 只在四步验收全部实际通过后翻转。

## SPEC 有界开放草案

草案文字见本分支 `SPEC.md` 末尾「2026-10-12 draft」段；用户裁决后定稿，随授权 PR 合并（用户门）。

## 前提与完成条件

- 前提：用户裁决决定表（发布来源、版本身份、观察参数、对比口径、拆步）；实验环境能做「改镜像或配置」的真实发布。
- 完成条件（草案）：F11 四步在 kind 实验环境真实跑通——健康发布、带无关并发故障的发布、延迟回归；输出区分 healthy / degraded / unknown；零发布门或流量策略写入；独立审查与独立验收测试作者按 AGENTS.md 执行；四步都有证据后翻 `passes`。

## 合同（r0，待裁决）

预审决定表：[合同决定表](2026-10-12-f11-post-release-contract.md)（Codex GPT-6 Astra，high，只读预审）。

## 执行进展与证据

- 2026-10-12 建 worktree、任务记录、SPEC 草案、issue；派发 Codex 合同预审。
- 实验环境：与另一会话（production-ops-agent-00）及 #184 协调，同一时间只跑一个真实故障。
- 迁移：线性链，当前 main 末尾 0012；#184 或其他会话先合并则 rebase 并重编号。

## 下一步与交接

用户裁决决定表 → 合同 r1 → 授权 PR → 按步实施。
