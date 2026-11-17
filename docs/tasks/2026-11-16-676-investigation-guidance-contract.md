# #676 合同决定表（r1）

预审者：全新上下文 Agent（未参与实现），2026-11-16，依据 origin/main 83b3b968。裁决：全部按推荐，lead 依用户授权定；唯一用户门项 D8-b 不做。

| # | 语义点 | 选项 | 推荐/裁决 | 理由与代码证据 | 用户门 |
|---|---|---|---|---|---|
| D1 | 全集群读事件是否在授权内 | a 已允许 / b 需扩权 | a | 工具无命名空间白名单，不传 namespace 即全集群（opspilot/tools/kubernetes_core.py:12、:1229；docs/tasks/2026-11-12-tool-expansion-contract.md:45,:59 R17/A2-7）；`OPSPILOT_K8S_NAMESPACE` 只作提示（tool-expansion-a2.md:88）；kind lab RBAC 为 ClusterRole 含 events（scripts/kind_lab/opspilot-readonly-rbac.yaml:27-39）；fake_k8s 不带 namespace 返回全部命名空间并支持 involvedObject.* 字段选择器（fake_k8s.py:613-617、:139-146） | 否 |
| D2 | 「事件投影带 involvedObject.kind」改在哪 | a 工具默认输出 / b 工具描述 + L1 / c 只 L1 | b | 三个工具无默认投影；a 是改 A2/A6 输出合同；A7 规定 L1 不写投影字段名（discipline.py:132-133），字面字段名与 group_by 示例放 L3，L1 用大白话。ext tool_schema_revision 会变 | 否 |
| D3 | 哪些工具描述要改 | a jq/count 共用段 / b 再加 tabular | b | 全量里三工具都读过事件；tabular「知道 namespace 就传」（kubernetes_tabular.py:441-444）与共用段「用 namespace 收窄」（kubernetes_core.py:1242）需加事件例外：先全集群聚合，超限改聚合或按 involvedObject 字段过滤，不缩到应用命名空间 | 否 |
| D4 | #628 的 RESULT_TOO_LARGE 句与「先全集群」冲突 | a 改该句 / b 另加一句 | b | discipline.py:129 不动；K8s 段另加「事件太大先分组计数，不按命名空间或 type 收窄」 | 否 |
| D5 | 本机「不超限」测试的查询形状 | a 任意查询 / b 只聚合查询 | b | 视图上限 25,000 token（registry.py:85）；逐条 2000 事件约 170 KB 超限，分组约 30 组不超；回放末尾存活事件实测 S16/S17/S38 仅 83/231/75 条（多数行为 DELETED），2000 作保守上界 | 否 |
| D6 | 「施加变更或故障的对象」与重启歧义 | a 原句 / b 加限定 | b | 模板未变的 rollout restart 是机制不是施加者，转第 3 项找配置；L2 只放通用句（已有 reports.py:97-98）；「只有事件支撑用 partial」放 K8s 段（base 无 K8s 工具）；partial 不影响 ITBench 计分（answer.py:554 只丢 inconclusive） | 否 |
| D7 | 「偏离默认」写法 | a 照写 / b 通用可判定 | b | 模型不知环境默认值；写成「名称或取值表明在注入故障、错误、延迟或失败的设置」；连接被拒且 Pod 健康查 networkpolicies 等，扩写 discipline.py:125 现有句并保留 services…ingresses…logs 语序（A7 K6 正则）；不写「优先于遥测」 | 否 |
| D8 | 第 4 项提示词还是确定性检查 | a 只提示词 / b 报告校验 | a | summary 自由文本，root_cause 无指向 claim 的结构（reports.py:409-421）；b 为 C3 L2 校验变更且违反 F3 R5（f3-contract.md:145、第七节语义点 3）；措辞：「把该对象认定为原因的那条 claim 是 hypothesis 时，conclusion 用 partial」，与 :104 不冲突 | b 是；a 否 |
| D9 | 各项放共用段还是 K8s 段 | a 全 K8s / b 按适用面拆 | b | F3 用 base（test_f3_step5_real_runs.py:81），第 4 项进共用 REPORT_CONTRACT；第 5 项扩写 INVESTIGATION_METHOD :94 句（discipline.py:233-252），「背景」写成「信号偏离前已稳定存在」，不算 affected 也不当根因；第 1、3 项与 D6 事件部分只进 KUBERNETES_GUIDANCE；base prompt_revision 变，F3 第 3 步 D9 与第 5 步重跑 | 否 |
| D10 | revision 钉值谁改 | a 实现者只改钉值 / b 测试者改 | a | 先例 #653；base 改动涉及 a6b4dfd02030 钉值（a7 合同:41、test_310 REPORT_CONTRACT_SHA256 与 baseline render sha、f3_step3:148、305/347/456、a7_main_path_impl:315、f3_pr2_impl:370）、a7 REPLAY_RENDER_SHA256、句数普查 30/8/9（test_instruction_discipline.py:286-288）；ext 动 EXT_TOOL_SCHEMA_REVISION（a7 合同:44）；base tool_schema_revision a3a95a0f888e 必须不变 | 否 |
| D11 | 旧证据处置 | 按 #653 惯例 | 按惯例 | F3 第 5 步 run-1/run-2 移到 `prompt-a6b4dfd02030/`，f3-acceptance-live 同理，第 3 步新增 `missing-source-rerun-<新后缀>/`；test_f3_step5_real_runs.py 3 条（第 5 步 2 条版本比对 + 第 3 步 D9）在租机证据入库前失败，证据与代码同一 PR；lite-full-1116、batch-646 成为旧 revision 证据，新旧分开报 | F3 `passes` 翻转是 |

## 应新增的测试（全新 Agent 按公开接口写）

1. 句数普查：更新 replay-candidate 句数，新增 KUBERNETES_GUIDANCE 句数钉值。
2. revision：base 与 ext prompt_revision 都变；ext tool_schema_revision 变；base tool_schema_revision、context_policy、REPORT_SCHEMA_VERSION 不变；解析与 REPORT_INVALID 现有测试不改。
3. K8s 段：保持 A7 守卫（工具名、参数名、kubectl/describe），新增环境词负向守卫（chaos、chaos-mesh、flagd、otel-demo、NetworkChaos、PodNetworkChaos、data-recorders）。
4. 关键短语正则（学 test_310，不断言整句）：事件全集群且不按 type 过滤、按 kind/namespace/reason 分组；模板未变的重启转去读工作负载与 ConfigMap；networkpolicies/services/endpoints/resourcequotas；summary 只把 fact 写成事实；hypothesis 根因对应 partial；窗口前的稳定错误写成背景。
5. 工具描述：jq/count 共用段与 tabular 有事件例外，出现 `involvedObject.kind`。
6. fake_k8s 经执行器（沿用 tests/test_itbench_lite_fake_k8s.py:584 fixture）：合成约 2000 条跨 3 命名空间的 Normal 事件含一个合成自定义 kind；不传 namespace 的分组查询 ok、计数正确、含其他命名空间的 kind；只查应用命名空间时漏掉；involvedObject.kind 字段选择器可用；可选：逐条全量输出报 RESULT_TOO_LARGE。

只能租机验证：冒烟 S19/S15 各 2 次；F3 第 3 步 D9、第 5 步两 Run 与两名审查者；集成批 8×2；全量 105。

## 未核实

逐条事件超 25k token 按字节估算；其余 32 个快照存活规模按三份样本类推；生产部署 RBAC（deploy/ 下无 ClusterRole）；钉值完整清单以实现后全量测试为准（预审未跑全量探针，由实现者先跑）。

## r2（2026-11-16，租机结果后）

依据：租机 F3 第 5 步两次 Run 均未查 flag 指标（#646 revision 4/4 查到）、集成批 REPORT_INVALID 2/16 与冒烟 1/4；全新上下文诊断者逐轮读四份 F3 ledger 推理与四个批次 ledger（scratchpad/diag676/，结论摘要如下）。lead 依用户授权定。

| # | 变更 | 理由与证据 | 用户门 |
|---|---|---|---|
| D12 | 改写 L2 r1 新句：删「When the claim that names that object as the cause is a hypothesis, conclusion is partial, not supported.」，改为「Supported requires a fact claim that names that object as the cause; a hypothesis alone is not enough.」；summary 那句保留 | #676 F3 Run 2 第 8 轮、Run 1 终答轮推理逐字引用旧句，为「按未验证假设填 root_cause 并收尾」放行；集成批 16 个终答轮 12 个引用旧句。新写法保住 #664（supported 必须有 fact 支撑）而不暗示「partial 是给假设点名用的」 | 否 |
| D13 | 「告警是入口不是范围」句从 INVESTIGATION_METHOD（两档共用）移到 KUBERNETES_GUIDANCE（仅 ext），措辞不变；D9 中第 5 项的适用面改为仅 ext | F3 两次 Run 把 flagd 按「错误是否偏离」判为背景后不再查；flag 翻转不产生错误。该句源自 ITBench 失败分析，base 档没有它要治的问题。base 相对 #646 只剩 L2 一处变化 | 否 |
| D14 | #678 并入：替换 L2 现有 no_data 句为「A view whose status is no_data, error, timeout or denied never supports a fact, counter_evidence or rejected_hypothesis, including a statement that something is absent, healthy or free of errors: an empty result shows only that this query matched nothing; write what the query was and that it returned nothing in gaps, or cite an ok view that shows it directly.」 | 首答引用非 ok 视图 #646 口径 24/120、#676 口径 9/20（Fisher p≈0.02），是首答被拒最大单项；#676 批 13 处非 ok 引用 6 处是 K8s 空列表（networkpolicies、resourcequotas）。F3 本来就要重做，推迟理由不再成立。不改校验 | 否 |
| D14a | D14 原文「matched nothing.」句号改分号，两句并一句 | 预存 tests/test_m1_report_contract_citations.py:81 要求 no_data 与 gaps 同句；语义不变 | 否 |
| — | 不做：K8s 列表零对象算 no_data（合同层，用户门，另开 issue）；next_steps 补句（证据弱，p≈0.19） | | — |

验证（r1 第四节的租机部分修订）：F3 第 5 步跑 3 次（合同要求 2 次，多 1 次因 n=2 分不清），按 k/n 记录是否查到 flag 侧证据、root_cause 是否为 checkout 部署、对部署有无否定或反证；冒烟 2 题各 2 次；集成批 8×2：REPORT_INVALID ≤1/16、首答重试率不高于约 8/16、root_cause_entity_pass ≥4/16、根因非 null ≥80%。
