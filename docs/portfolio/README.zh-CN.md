# 作品集导览

[English](README.md) | 简体中文

给审阅者的一条阅读路线，约需 20 分钟。OpsPilot 是只读的事故调查 Agent：它读取遥测数据，写出有证据支撑的报告，另有一个独立进程检查服务是否已经恢复。OpsPilot 从不执行生产变更或修复；合格结论发布后，恢复观察（只读）可以自动授权，其余一切仍由人控制。它只在合成流量、注入故障的实验环境里构建和测量过，从未在生产环境运行（见[限制](limitations.zh-CN.md)）。

57 秒插画概览（英文字幕；画面中的字符串与数字来自 2026-11-27 的 lab 运行与 Cloud-OpsBench 子集）：

https://github.com/user-attachments/assets/dcbc0f35-e940-4afe-8495-aeac3e8a3395

## 1. 它是什么、拒绝做什么（3 分钟）

- [README](../../README.zh-CN.md)：它做什么、当前状态、如何安装。
- [PRODUCT-CONSTRAINTS.md](../../PRODUCT-CONSTRAINTS.md)：运行时规则。没有生产写入，没有发布门，也没有由模型选择的 shell 或 SQL。
- [架构](architecture.zh-CN.md)：有哪些进程、各自负责什么，一次事故在其中的路径，以及代码与设计仍有哪些出入。

## 2. 一次事故，从头到尾（5 分钟）

这次事故在一台刚重装的租用主机上运行。装有 OpenTelemetry Demo 的 kind lab 和 OpsPilot（经 Compose）都用仓库里的脚本安装。把两者连起来需要三个当时文档里还没写的步骤，之后在 #532 中补上（[第 1 步证据](../evidence/f9-clean-install-2026-11-09/README.md)）。时间为 2026-11-10 的 UTC。下表每一行都是一条数据库记录，列在 [f9-steps-2-4 证据第 2 步](../evidence/f9-steps-2-4-2026-11-10/README.md#第-2-步一次事故从告警到归档)中：

| 时间 | 发生了什么 |
|---|---|
| 07:07 | lab 注入 flagd 故障 `paymentFailure`（注入的故障，load-generator 合成的流量） |
| 07:13 | Alertmanager 触发 `CheckoutPlaceOrderErrorRatioHigh`。OpsPilot 创建一个事故和一个 Run |
| 07:13–07:16 | 12 轮模型调用、20 次只读 Prometheus/Jaeger 查询。报告为 `supported`，根因与注入的故障一致 |
| 07:16 | 合格结论发布后，恢复观察自动授权 |
| 07:19 | lab 脚本撤除故障 |
| 07:43 | Observer 在持续健康窗口之后记录 `recovery_confirmed`，用的是它自己的账号，不调用模型 |
| 07:45 | worker 起草复盘，等待人工审核 |
| 09:39 | 用户在工作台关闭事故 |

同一证据页也列出了这次运行没有覆盖的内容：没有人接手交接，没有在 OpsPilot 之外处理任何事情，也没有人审核复盘。在同一台主机上，还在 Run 进行到一半时杀掉了 worker，Run 在同一个事故上恢复（第 3 步）；发布镜像也在有进行中工作的情况下做了升级、回滚、再升级（第 4 步）。

更早的 M1 流程中文演示（含知识审批）见 [docs/demo/m1-end-to-end.md](../demo/m1-end-to-end.md)。

2026-11-27 在另一台按同样方式安装的新租主机上又跑了一次，用重设计后的工作台从告警走到复盘获批（批准由录制脚本点击，不是人审阅后的决定）；截图见 [README](../../README.zh-CN.md#工作台里的一次事故)，记录见[该次证据](../evidence/workbench-live-2026-11-27/README.md)。

## 3. 效果如何（5 分钟）

- [与 HolmesGPT 的同条件对比](../results/f1-same-condition-comparison-2026-11.md)（英文）。两个系统使用相同的 lab、故障、模型、只读代理和预算，每个场景 3 次尝试。该页给出 dev 与 holdout 结果、失败情况、考试开始后改动了什么，以及为什么 judge 算作未校准。数字来自封存的 [S10 证据](../evidence/f1-s10-scoring-2026-11-06/)。
- [72 小时 lab soak](../evidence/m3-soak-2026-11-07/README.md)。内容包括在按计划注入故障和演练下的可用性、缺失的观察、卡住的工作、资源占用与费用。这是 lab soak，不是生产证明。
- [公开基准结果](../../README.zh-CN.md#评测结果2026-11)。表中列出每次运行及其证据：HolmesGPT 公开评测集上与 HolmesGPT 同模型、同 judge 的对比（2026-11-23 为 163/171 对 162/171，HolmesGPT 一侧部分沿用之前的结果）、SREGym Lite（38/51）、ITBench-Lite 离线快照（82/105）、在真实注入故障下的 ITBench SRE 现场版（14/24），以及 Cloud-OpsBench 100 题子集上与基准自带 agent 同模型的对比（JRA 0.96 对 0.89）。每一行的限制写在表下的说明里；这些都不是生产证据。

## 4. 如何运维与保障安全（4 分钟）

- [运维](operations.zh-CN.md)：健康检查、常见故障情形及处理方法、升级与回滚、数据与费用。
- [威胁模型](threat-model.zh-CN.md)：每条产品规则由什么强制执行、证据，以及剩余缺口。
- [上游归属](upstream-attribution.zh-CN.md)：从 HolmesGPT 和 DeepSeek tokenizer 复制了什么、用哪个许可证；也说明拿什么做了对比、哪些只在 lab 中运行。

## 5. 如何构建的（3 分钟）

- 合同先行。设计决策见 [C3 技术方案](../design/technical-proposal-2026-09-07.md)和 [ADR](../adr/)；验收步骤在 [feature_list.json](../../feature_list.json) 中。只有每一步都有证据，功能的 `passes` 才会翻转。
- 证据优先于声称。真实运行会在 [docs/evidence/](../evidence/) 下留下冻结的 `summary.json`，原始日志保存在仓库之外。部分结果和失败照实记录：F9 第 2、4 步在[干净主机终轮证据](../evidence/f9-final-clean-host-2026-11-11/README.md)中只部分达成，之后在[第二次终轮](../evidence/f9-final2-2026-11-11/README.md)中重做。F9 现在只凭 lab 证据通过，限制列在[限制](limitations.zh-CN.md)中。
- 作者分离。按 [AGENTS.md](../../AGENTS.md) 中的项目规则，验收测试和审查由没有参与实现的 Agent 完成。工作顺序与状态见 [ROADMAP.md](../../ROADMAP.md)。

## 动手试试

在一台干净的 Ubuntu 24.04 主机上，按 README 的[同一台主机上的 OpenTelemetry Demo lab](../../README.zh-CN.md#同一台主机上的-opentelemetry-demo-lab)一节操作。需要一个 DeepSeek API key。lab 启动约需 5 分钟；执行 `fault inject` 后，checkout 告警约在 5–7 分钟内触发。
