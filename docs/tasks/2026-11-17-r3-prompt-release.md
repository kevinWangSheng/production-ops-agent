# r3 提示词与报告校验发布（#695 + #690，#686）

- 状态：租机验收完成（2026-11-17）。PR-R（#686）已合并 main（#700 `c7f89dbc`）；PR-G（#697）证据入库，待合并。全量 82/105，冒烟、全量各门通过；集成批 REPORT_INVALID 1/16 按偶发失败接受（#701）；F3 第 5 步 Run 1 两名审查者 FAIL，`passes` 保持 false（用户裁决）。
- 合同修订：r1（2026-11-17 用户「按推荐开始吧」）
- 依据：lite-full-1117 失败分类（54/105，51 次失败中 36 次为「读到非 off 故障设置、根因写成被模板未变重启的 Deployment 或 null」），证据 [run.md](../evidence/itbench-lite-sre-lite-full-1117/run.md)；issue #695、#690、#686。

## 目标

一次发布修两件事，再用一次租机按「冒烟 → 集成批 → F3 → 全量」验收；之后停止为 ITBench 分数调指导，转拓宽只读数据面。

1. **PR-G 调查指导**（#695 + #690）：`KUBERNETES_GUIDANCE` 替换一句并加两句（23→25），`INVESTIGATION_METHOD` 加两句（8→10）；更新 revision 钉值；F3 第 3、5 步证据随租机重做。
2. **PR-R 报告校验**（#686）：解析前去一层前置文字/代码围栏，剩余必须正好是一个合法 JSON 对象；在工作台 P1（m0-response-v1 包装）合并后基于它实施。

## 合同（r1）

PR-G 决定表全文：[2026-11-17-r3-prompt-release-contract.md](2026-11-17-r3-prompt-release-contract.md)（预审 r0，D1–D22 可逆默认按推荐）。要点：

- U2(#690 后半句)：b，「…is unchecked, not unobservable」，不与 #310 U1-a「not checked is a gap」冲突。
- D2 按取值判断「不是 off/0/false」，K8s 段不得出现 enabled；D4 只算模板未变的重启或正在出错，且无其他施加者；D6 根因是持有设置的对象；D7 结论强度写条件、不提 hypothesis；D8 不写「已确认未变」例外。
- D11 #690 两句放在 INVESTIGATION_METHOD 第 92 行后；D14 无样本时段是 unknown 不是 zero。
- D16 test_676 两条被取代的断言由独立测试作者改写；D17 新合同测试 K1–K3、M1–M2 单句正则 + 删除变异 + 削弱反例；D18 不加固定回放行为测试，行为证据靠租机。
- D20 实现者只改钉值；D21 F3 旧证据移入 `prompt-8688f6e52275/`，新证据与代码同 PR；D22 一个 PR。

PR-R（#686）用户裁决：U1=a（#456 七项断言由独立测试作者改写为「合法带壳当轮接受」）、U2=a（hypothesis/recommendation 缺 evidence_ids 默认空列表，fact 缺键由 FACT_SCOPE_REQUIRED 拒）、U3=a（缺 next_steps 默认空列表，gaps 不放宽）、U4=a（不剥后置文字）。决定表在 PR-R 开工时入库。

## 分工与 worktree

| 角色 | 执行者 | worktree / 分支 |
|---|---|---|
| PR-G 实现 | Claude Opus 5.5 子 Agent | `../poa-695-impl` / `feature/695-setting-restart-guidance` |
| PR-G 验收与合同测试 | Codex（codex-role test），不看实现 | `../poa-695-tests` / `test/695-guidance-contract` |
| 独立审查 | Codex（codex-role review） | 只读 |

## 验收计划（租机一次，在 PR-G 分支上跑）

顺序（D21：新证据与代码同一 PR）：PR-R（#686）先合入 main；PR-G 分支随后 merge origin/main；唯一一次租机在含两处改动的 PR-G 分支上跑；证据提交进 PR-G；之后 PR-G 合并。

阶段与通过条件见交接中的验收表（冒烟 9 次：S8、S14、S16 各 3；集成批 16 次；F3 ×3；全量 105）。全量门：root_cause_entity_pass ≥ 54/105、REPORT_INVALID ≤ 2、首答被拒 < 39。

## 证据

- PR-G 实施（本地分支 `feature/695-setting-restart-guidance`）：新版本 base `prompt-replay-candidate-860b743085d1` / `l1a-replay-candidate-454225cff16b`，ext `prompt-replay-candidate-k8s-1137251eea4d` / `l1a-replay-candidate-k8s-cfd71e12cb4a`；tool_schema（`otel-demo-a3a95a0f888e`、`otel-demo-ext-c63f058afb18`）与 context_policy 不变（select_profile 打印）。删掉新句、放回旧 :135 后 INVESTIGATION_METHOD 与 K8s 段 sha256 分别为 `222e5fa7…`、`1ff52ac3…`，即其余句逐字节不变。
- 改动：discipline.py 文字与段说明；D20 列出的 9 个测试文件只改钉值和句数；F3 第 5 步与 f3-acceptance-live 的 run-1..3 移到 `prompt-8688f6e52275/`，两份 run.md 各加一行说明。
- 定向测试（审查修正后：#690 两句移到「When the exact entity named has no data…」之后，D11）：test_695 与 discipline/A7/676/310/对齐合同共 7 个文件 242 过、0 败；钉值与 F3 8 个文件 76 过、12 跳过（PG）、14 败（test_f3_step5_real_runs 里依赖 run-1/run-2 与第 3 步重跑的全部条目，等租机证据）。ruff、ruff format、mypy（discipline.py）通过。

- **租机验收（2026-11-17，一次租机，代码 `b0652dbd` = PR-G `ea2bc41b` + PR-R `719919e7`，代码树与 PR-G 最终 HEAD `d065c41e` 逐字节相同，lead 按 ls-tree 核对）**：
  - 冒烟 [lite-smoke-r3](../evidence/itbench-lite-sre-lite-smoke-r3/run.md)：设置题 6/6 根因为 ConfigMap `flagd-config`（不是被重启的 Deployment），0 次 supported；S16 3/3；REPORT_INVALID 0/9。门全过。
  - 集成批 [lite-batch-r3](../evidence/itbench-lite-sre-lite-batch-r3/run.md)：首答被拒 3/16 = 18.8%（1117 同 8 题 8/24 = 33.3%）；S22 2/2、S34 1/2；REPORT_INVALID 1/16（S105 t2）、S105 1/2 两门不过。重放：首答截断 id，重试暴露多余 target_refs，引用检查每条 claim 只报第一个失败（早于 r3）。lead 裁决按偶发失败接受，#701 跟踪。
  - 全量 [lite-full-r3](../evidence/itbench-lite-sre-lite-full-r3/run.md)：root_cause_entity_pass 82/105（1117 54/105）；REPORT_INVALID 1/105（S12 t2，同一机制）；首答被拒 29/105（1117 39）；1117 的 16 道 3/3 题 15 道保持，S33 3→2；升 12 题。supported 51→22、根因 null 12→17。
  - F3：第 3 步 D9 重跑通过（[README](../evidence/f3-step3-coverage/README.md)「r3」）；第 5 步三次真实 Run（[运行](../evidence/f3-acceptance-live/run.md)、[判定](../evidence/f3-step5-competing-cause/run.md)「r3」）：Run 1 两名审查者 FAIL（第 5 项：只否定了「换镜像」），Run 2、Run 3 均 PASS 8/8，第 2 步全部一致。审查者都是 Claude Opus 子 Agent（用户 2026-11-17 起不用 Codex，偏离「审查者 B 用另一模型」）。`test_f3_step5_real_runs` 15 条全过。
  - 评测后 main 合入 #702（工作台 P2，改 loop/context 的消息接力）；r3 证据对应 b0652dbd ≡ d065c41e 的代码，revision 未变。
  - 原始包（ledger、重放、主机脚本、日志）在仓库外 `~/rental-evidence/r3/r3-raw.tgz`（sha256 `d78e6b74…fce5`）；入库前秘密值原文搜索 0 命中、正对照 1。

## 待决

- F3 `passes`：用户 2026-11-17 裁决保持 false（原标准是前两次 Run 都通过，Run 1 不通过），F3 不再为此调提示词。已定，不再待决。
- 引用检查只报每条 claim 的第一个失败、使强制重试看不到后续问题：#701。
