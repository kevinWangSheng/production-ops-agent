# F8 逐步核对（翻 `passes` 前）

- 状态：已完成（2026-11-10 独立 Codex 审查结论 FLIP，用户批准翻 F8 `passes` 为 true；范围仅 M3 lab，不是生产证明）
- 更新日期：2026-11-10
- 依据：feature_list.json F8 四个步骤；[F8 任务记录](2026-10-27-m3-f8-operability.md)（已超两页，故另开本记录）与[合同](2026-10-27-m3-f8-operability-contract.md)；[soak 记录](2026-11-04-m3-soak.md)；#386、#429
- 工作区：`~/dev/AI/poa-f8-verification`，分支 `docs/f8-verification`（起点 origin/main `ed96ec2b`）

## 目标与范围

把 F8 每一步的证据位置、判定和证据证明不了的东西列在一张表里，供独立审查者核对、用户决定是否翻 `passes`。本记录不翻 `passes`，不改产品代码。顺带收尾 #429 剩下的两项（trace 说明、费用），写在第 3 步证据目录里。

## 逐步核对

类别都是集成运行（本机 colima VM 或租用 lab），不是生产证明。

| 步骤 | 证据 | 判定 | 证明不了什么 |
|---|---|---|---|
| 1 健康、trace、指标、结构化日志、队列深度、provider 状态、工具错误、卡住的 Run、token 与成本可按事故和版本关联 | 字段对照表：[合同「字段对照表」](2026-10-27-m3-f8-operability-contract.md#字段对照表f8-第-1-步预审-71-86-定稿)。健康、队列深度、provider 状态、版本：PR #418，[F8 记录「#413 实现」](2026-10-27-m3-f8-operability.md#413-实现对齐合同-r1)的真实运行；lab 抓取 PR #421，[f8-step1-drills](../evidence/f8-step1-drills/README.md)（`running_lapsed` 在那里只有 0）。卡住的 Run：杀 worker 让租约过期，`running_lapsed` = 1，同一次抓取的 build_info revision 与 PG 行、outbox、worker 日志对上事故 `75ea6248` 与 Run `7f1e5f5d`，[f8-step1-stuck-run-2026-11-10](../evidence/f8-step1-stuck-run-2026-11-10/README.md)。trace、token、工具错误码、Run 状态、成本估算按 `incident_id` + `image_revision` 筛选：PR #441，[243-trace-outbox](../evidence/243-trace-outbox/README.md)（Run `f18affc0` 等 4 个）。结构化日志带 `incident=` kv 与 `revision`：PR #450，[F8 记录「结构化日志实现」Compose 抽检](2026-10-27-m3-f8-operability.md#结构化日志实现对齐合同-r2--r21)。soak summary 的 `stuck_work`（[m3-soak-2026-11-07](../evidence/m3-soak-2026-11-07/summary.json)）按事故列出卡住的工作，但其中唯一一项 `over_wall_clock_runs`（1 个）统计的是超过墙钟的 Run，不是租约过期 | 按已裁决的字段对照表达成，各面分别在不同 Run 上证明 | 没有一次 Run 同时把十个面按同一事故串起来。指标按合同只按版本关联（事故 ID 不作标签），卡住的 Run 在指标上只有 `running_lapsed` 计数。按事故关联 token、工具错误、成本只能靠 trace（默认 `off`，soak 也是 `off`，S-R18）或直接查 PG；trace 证据只有 4 个 fixture Run，没有成功的工具调用，`run_state` 行与根 span 不带镜像字段。成本只是 LangSmith 估算；按 [Q17 修订](2026-10-27-m3-f8-operability-contract.md#243-产品模式-trace-outbox-合同r3f8-第-2-实施步骤)（用户 2026-11-10）对照 token×官方单价：[联合 Run](../evidence/f8-step1-joined-2026-11-10/README.md) `2e4ef59a`（闲时）LangSmith $0.00482625，token×单价 CNY 0.0288 ≈ $0.00431（6.6760 CNY/USD，H.10 11-06），LangSmith 高 11.9%，差额 $0.000514069 中 $0.000508032 是 3456 个缓存命中 token 按未命中价计（3456 × (0.15 − 0.003) 每百万），其余约 $0.000006037 来自汇率折算：人民币单价折美元（未命中 1 元 ≈ $0.14979、输出 4 元 ≈ $0.59916 每百万）与 LangSmith 条目的 $0.15 / $0.60 不完全相等；LangSmith 用内置条目 `deepseek-flash` 输入 $0.15 / 输出 $0.60 每百万（API 读取）。此前 4 个 fixture Run（北京时间周一高峰）LangSmith $0.0086，token×单价 CNY 0.0673 ≈ $0.0101，LangSmith 偏低，因条目不分峰谷。结构化日志原始记录在仓库外，库内只有任务记录里的摘录 |
| 2 模拟 provider 故障与 Agent 不可用，验证外部检测、有界降级与人工交接 | PR #421，[f8-step1-drills](../evidence/f8-step1-drills/README.md)：演练一停 worker → `OpsPilotAgentAbsent` firing（约 3m46s）、Run `queued`、过 deadline 后清扫得到 `DEADLINE_EXCEEDED` 交接；演练二断模型出口 → 两次 `MODEL_UNAVAILABLE`、`waiting_human`、`OpsPilotModelCallsFailing` firing，人工 `resume` 后恢复。soak 复现：A1 杀 worker、A3 断模型出口 20 min（[soak README](../evidence/m3-soak-2026-11-07/README.md)「A3 的 Run」） | 达成 | 外部检测只到 Alertmanager：`opspilot-self` 接收器按 F8-Q3 不接任何通知，没有人会被叫醒。Agent 不可用时的交接要有人打开事故页触发清扫（F8-Q4，不做自动清扫）。provider 故障只模拟了解析到 127.0.0.1（连接被拒），没有覆盖超时、慢响应、429/5xx。合成告警由脚本 POST 到 intake |
| 3 带在途事故的版本化 schema 升级与有文档的回滚 | PR #439（`cf2952b6`），[f8-step3-upgrade-rollback](../evidence/f8-step3-upgrade-rollback/README.md)：升级、回滚、不兼容三段，四种在途形状两段都通过，业务表三次快照只增不减，不兼容版本 `blocked(INCOMPATIBLE_STATE)` 后人 `new_run` 跑完；`deploy/compose/UPGRADE.md`「Rolling back」；独立审查 review-439 APPROVE（`6db84625`）。trace 说明与费用（本 PR）：同目录 README「trace 与费用」、[cost-primary.json](../evidence/f8-step3-upgrade-rollback/cost-primary.json) | 达成 | 没有观察到在途 Run 的优雅排空（每次都先 SIGKILL）。只走了一步 schema（0023↔0024），两个版本号都是 0.1.0，按 revision 与镜像 ID 区分；没有经过「迁移结束授权会话」的路径。固定 digest 的升级属 F9 第 4 步。演练 Run 没有 trace（演练时没有产品模式投递，理由见证据）。告警只取了 Prometheus `ALERTS` |
| 4 至少 72h lab soak，有背景流量、故障与恢复；公布可用性、缺失观测、卡住的工作、资源与费用，不宣称生产证明 | PR #528，[m3-soak-2026-11-07](../evidence/m3-soak-2026-11-07/README.md)：259218 s，33/33 注入，`step4.achieved = true`、`unmet = []`；费用口径 PR #529（CNY 4.46）；租机按 Vultr 牌价 72.005 h × $0.132 ≈ $9.50（不是账单，见证据 README「费用」）；[soak 记录](2026-11-04-m3-soak.md) | 达成 | 不是生产证明：一次运行、共享 vCPU、控制主机上有别的负载，资源和延迟数字不可比较。Observer `/readyz` 97.74% 与恢复样本缺 27% 都来自采样慢于轮询陈旧阈值（#522），不是服务停了。有 1 个未结算预留（#527）。trace `off`。费用没有控制台对照 |

## 我认为最弱的一步

第 1 步。它是按裁决过的对照表「分面」达成的：每个面都有真实证据，但按事故关联 token、工具错误和成本依赖默认关闭的 trace 或直接查库，持续运行（soak）中没有开 trace。翻 `passes` 前请审查者和用户确认：对照表的这种口径就是第 1 步要的「可关联」。

## #429 收尾（2026-11-10）

- trace：8 个演练 Run 运行时 `OPSPILOT_TRACE` 为默认 `off`，三个构建都没有产品模式投递。#441 合并后不补带 trace 的演练：旧版 0023 没有 outbox，回滚时 downgrade 按 Q18 删掉未投递的 span，回滚段在设计上无法完整 trace；「跨 worker 重启续跑仍能投到」已由 #428 故障②证明。全文见证据 README。
- 费用：22 次已结算调用 token×单价 CNY 0.0733（全部闲时），另有 3 次被 SIGKILL 打断、无用量的调用，上限合计 CNY 0.1064。不用余额差与控制台，理由同 soak。
- #429 其余验收项均已有证据（确定性测试 `tests/acceptance/test_429_*` 在 main、独立审查 APPROVE、`passes` 保持 false）。本 PR 合并后可关闭 #429。

## 待决

- 无。裁决：独立 Codex 审查 FLIP（2026-11-10），用户批准翻 F8 `passes`（第 1 步按字段对照表口径）；范围 = M3 lab，不是生产证明。
