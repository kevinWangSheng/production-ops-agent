# M0 / SPEC gate 决策草案（待用户决定）

本草案不修改 `SPEC.md`，也不修改 `feature_list.json`。当前事实：M0 八个工作包仍有 partial/证据不足；本轮 B3、B4、B5 PG、B6 provider probes 和 B7 产生了可回读证据，但不等于产品验收完成。

## 选项 A：保持 `SPEC gate not cleared`（推荐）

依据：

- 工作包 2 的 streaming/工具错误只在隔离 provider probe 部分通过，未接入产品 StepStore/PG 审计；仓库无 context compressor。
- 工作包 3 的 pause/resume 状态机、HealthProfile 持久乱序重放、独立 observer 授权并发仍缺。
- 工作包 5 没有形成可比较的 Holmes 最终报告；judge 尚未人工校准，保留集盲测未就绪。
- 工作包 7 工具总 wall-time/清理上界和供应商账单仍 unknown；B5 K8s/OS 隔离缺测。

若选择 A：继续保持当前 ROADMAP `[-]` 状态，不改 SPEC gate；M1-01 仅保留任务拆分和设施准备，不开始产品实现。

## 选项 B：以有界证据开放 M1-01（仍需补齐以下条件）

该选项不是把 M0 写成完成，而是明确授权开始受限 M1-01 实施，并把缺口变成 M1 必须的入口验收：

1. **SPEC.md gate 段**：新增带日期/决策人的“有界开放 M1-01”记录，保留未通过项和禁止能力；不得删除原验收条件。
2. **ROADMAP.md**：将 M1-01 从“待 gate 决定”改为“有界实施中”，链接本草案和所有未完成证据项。
3. **M1-01 必补验证**：真实产品 streaming/断点恢复、工具错误 PG 审计、压缩器配对；pause/resume/observer authorization；K8s RBAC/OS 隔离；正式 judge 校准与未见保留集；工具 wall-time/清理上界；供应商账单对账。
4. **实施限制**：仍不得获得产品写权限、发布批准权、任意 shell/SQL、凭据导出、trace 越界或自动 gate 决策；每个新增真实实验另立合同和 ledger。

## 用户选择

- [ ] 选择 A：保持 `not cleared`。
- [ ] 选择 B：有界开放 M1-01，并接受上列补齐清单、日期和责任人。

在用户明确选择前，Agent 不修改 SPEC gate、不修改 feature passes、不合并 PR、不采购。
