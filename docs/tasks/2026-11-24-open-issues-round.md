# 剩余开放 issue 收尾轮（2026-11-24 起）

- 状态：实施中。R1+R2 文档 PR（`chore/round-known-limitations`，worktree `../poa-round-docs`）已提交待合并，合并后关 7 个 issue；其余项实施中
- 更新日期：2026-11-24
- 依据：2026-11-24 开放 issue 重新同步（13 个）；四份独立预审（见「合同」）；[PRODUCT-CONSTRAINTS](../../PRODUCT-CONSTRAINTS.md)；[C3](../design/technical-proposal-2026-09-07.md) §3、§4、§10、§11；[后续四批](2026-11-22-next-batches.md)（#780 的方向、存储、审核流程、大小上限已在 B4-U3/U4/U6 裁决）
- 工作区：每个 PR 一个 worktree（`../poa-<issue>-<名>`），开工时填写

## 目标与范围

把 2026-11-24 仍开放的 13 个 issue 处理完：5 项实施，7 项照实写成已知限制后关闭，1 项（#564）进下次租机。

| 去向 | issue | 做什么 |
|---|---|---|
| 实施 | #295 + PR #551 | 解 SPEC 冲突，盲标 111 份考试报告校准二值 judge 流程，出结论；F1 第 5 步按结论处理 |
| 实施 | #780 | 每目标常驻操作员说明作为模型输入（新输入通道，进 tests/safety 加变异） |
| 实施 | #830 | SREGym p12 在故障注入前建 Valkey 只读账号 |
| 实施 | #498 | 用近期租机 Run 的产品内时间戳出性能基线，只测量 |
| 实施（Agent 自定） | 无 issue | 工作台 Human-owned 状态 Resume 提示语改为「解除暂停」语义（`opspilot/web/incident_view.py`）；`docs/portfolio/architecture.md` 过时的工具列表 |
| 关闭为已知限制 | #374 #375 #197 #124 #134 #159 #391 | 一个文档 PR 改 C3 文字为现状 + AGENTS.md 加一句，合并后关 issue |
| 下次租机 | #564 | ITBench 现场版冒烟 |

不做：#295 的「改 judge 后换新题重跑」；#498 的调查 loop 改动；#564 的 HolmesGPT 同条件对跑与 Chaos Mesh 读权限。

## 前提与完成条件

- 本机 swap 2026-11-24 约 16.2–16.4 / 17.4 GB，四份预审都没跑全量 `tests/acceptance` 探针；实施者开工前用 Draft PR 的 CI 当探针。同时最多 3 个执行 Agent。
- 完成条件：每个 PR 合并、CI 通过、独立审查（全新 Opus）发现已处置；#780 的验收与合同测试由未参与实现的全新 Opus 编写，PR 附本机 kind 有界真实 Run 的 trace 与 `summary.json`；7 个 issue 已关并链接本记录；租机清单交给用户。

## 合同（用户 2026-11-24 全部按推荐裁决）

完整决定表（选项、代价、Agent 自定默认值、PR 拆分）：[A：#830 #780](2026-11-24-round-prereview-a-eval.md)、[B：#295 #498 #564](2026-11-24-round-prereview-b-judge-perf-itbench.md)、[C：#374 #375](2026-11-24-round-prereview-c-retention.md)、[D：#197 #124 #134 #159 #391](2026-11-24-round-prereview-d-architecture.md)。各表「Agent 自定」项按表内默认执行。

| 编号 | 裁决 |
|---|---|
| R1 | 7 项关闭为已知限制，C3 改为现状：报告、审计、知识长期保留，不删不归档（#374，C3 §11 第 427、431 行附近）；关闭 30 天后不清步骤、人工消息、Run 输入快照，它们是 §7 恢复依据（#375，§11 第 429 行「provider history」加限定）；web ledger 原问题、Chat 面板与反馈表写进同一条「随事故保留」；worker 在租约与代际栅栏下写自己的 Run、目标凭据在 worker、网络不隔离（#197，§3）；接管不可撤回，要再查就关事故等新告警（#124，§10 + limitations）；pod 级信号维持 Deployment 级（#134）；Observer 不发 trace（#159）；换集群时加一条校准清单条目，走用户门 PR，`approval_ref` 写明沿用 U2 证据、未在该集群复测（#391） |
| R2 | AGENTS.md「验证与汇报」加一句：只改 Observer 的 PR，用冻结 `summary.json`、离线重放和 `/metrics` 作证据，不要求 LangSmith trace（用户授权） |
| R3 | #295 盲标者：Opus 5.5 + `gpt-6.2-astra`（两家厂商，PR #551 原设计，只换版本），111 份报告、保留集预期要素与 canary 发给 Anthropic 和 OpenAI；实施者先核实 OpenAI judge 密钥可用，不可用则改 Opus 5.5 + Fable 5.2（`claude-fable-5-2`），证据写明同一厂商。标注者身份不在冻结内容里，换人不需重新冻结 |
| R4 | 校准不达标：F1 第 5 步照实写「未达成」，F1 维持 false，关 #295；放弃「改 judge 一次、换新题重跑」 |
| R5 | F1 第 5 步「确定性检查负责恢复观察」引用 F6 现有证据满足；「真实 Run 没见过场景答案」由实施者在私有目录扫描考试真实 Run 的工具交付与 trace，仓库只写计数和 hash，补不上照实写 |
| R6 | #830：SREGym 注册加 `before_fault_injection` 钩子，注入前以 `default` 免认证建只读账号（钩子写成独立小文件，避开 SREGym 顶层 `scripts` 包同名）；沿用披露码 `VALKEY_READONLY_ACCOUNT_BY_HARNESS`，记录加「注入前建」字段；若故障重置账号则落回现有失败披露。PR 先合，下次租机 p12 ×3 验证 |
| R7 | #564：下次租机先冒烟 2 场景 × 1 遍，只跑 OpsPilot，不授权 Chaos Mesh 自定义资源读取，结果与 ITBench-Lite 分轨；冒烟后再定全量（24 × 3）。开工前重查 ITBench 公开场景表 |
| R8 | #498：基线用近期租机 Run 的产品内时间戳（预审粗算 411 个 Run），报 p50/p95 与 `traces_search` 超上限次数，写进证据并评论 issue；无明显热点即关，有则另开 issue |
| R9 | 下次租机在本轮 PR 全部合并后开，一次跑完：#830 p12 ×3、Holmes 162 复查（#780）、#564 冒烟。规格 8 vCPU / 32 GB amd64，预算 24 小时内；建机删机由用户做 |
| R10 | #780 按 B4 已裁决实现，含预审三处校正：恶意指令注入验收在 F7 第 2 步（原裁决误写第 4 步）；只有 Run 输入真带说明时边界措辞才计入 `prompt_revision`；说明放在 runbook 列表前（维持原裁决，不跟上游放后）。迁移 0029 与 Run 输入 v9 未被占用（预审时） |

## 证据

- 预审时 main `dda03323`；抽查核实：`opspilot/persistence/incidents.py:245-249` 清理后步骤重建抛 `INCONSISTENT_STATE`；`opspilot/web/incident_view.py` Resume 提示语为「Put the Run back in the queue」。

## 待决

无。实施中新冒出的用户门只停对应项，集中写进收尾简报。
