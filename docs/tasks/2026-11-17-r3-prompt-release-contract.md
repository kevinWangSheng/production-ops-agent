# #695 + #690 提示词发布：合同预审决定表（草案 r0）

预审者：全新上下文 Agent（未参与实现），2026-11-17，只读，依据 main `5df9db33`。答案隔离：没有打开 ground_truth / assertion.json / status.json / few_shot.py / ~/.agent/handoffs / ~/.agent/eval-probes；scores.json 只读了 `root_cause_entity_pass` 一列，没读 `root_cause_reasoning`。

范围：L1 `KUBERNETES_GUIDANCE`（#695，只在 ext 档）+ L1 `INVESTIGATION_METHOD`（#690，base 和 ext 两档共用）。L2 `REPORT_CONTRACT`、L3 工具描述、报告校验都不动。

## 0. 现状事实（已核查）

- 当前版本：base `prompt-replay-candidate-8688f6e52275` / `l1a-replay-candidate-9a45deb526fe`；ext `prompt-replay-candidate-k8s-0b066c7cd6d1`；tool_schema base `otel-demo-a3a95a0f888e`、ext `otel-demo-ext-c63f058afb18`（本机用 select_profile 打印）。
- `KUBERNETES_GUIDANCE` 23 句（discipline.py:118-142，句数钉在 tests/test_instruction_discipline.py:293）。#695 要替换的是 :135「Name such a setting as the root cause only when the fault it declares matches the failure path you observed; otherwise keep it as a hypothesis.」。它前后的 :132（原因对象是施加变更或故障的对象）、:133（模板未变的重启是机制，转去读 ConfigMap）、:134（列出名称或取值表明注入故障的设置）、:136（只有事件支撑时用 partial）都还在。
- `INVESTIGATION_METHOD` 8 句（discipline.py:87-96），sha256 `222e5fa7…` 钉在 tests/test_676_investigation_guidance_contract.py:37-39、:514-522（D13：base 与 83b3b968 逐字节相同）。
- 冻结段：`MISSING_SERIES`（:84，「缺失序列不等于零」）与 `EVIDENCE_DISCIPLINE` 由 tests/acceptance/test_310_investigation_method.py:40-44 逐字节钉住，baseline 变体也锚定它们，所以不能改。#690 第 2 句只能进 INVESTIGATION_METHOD。
- 用户裁决过的冲突源：IMPACT_SCOPE :106「say so when you did not check」与 :110「not checked is a gap」（#310 U1-a，docs/tasks/2026-10-22-310-investigation-method-contract.md:38、:50）。
- L2：reports.py:107-111「When conclusion is supported or partial and the evidence points to one object, set root_cause to it; otherwise set root_cause to null. Supported requires a fact claim that names that object as the cause; a hypothesis alone is not enough.」。ITBench 计分只丢掉 inconclusive（scripts/itbench_lite/answer.py:554），partial 不扣分。
- 数据面：K8s 读取对所有对象 `metadata.pop("managedFields")`（opspilot/tools/kubernetes_core.py:657），模型看不到设置是什么时候改的。
- 工具面：base 档只有 `get_metric_names`、`get_label_values`，没有列标签名的工具；`get_all_labels` 只在 ext 档。base 的 `metrics_range_query` 返回每个标签组合一条序列（工具描述原文）。
- A7/676 守卫：K8s 段禁止出现 `kubectl`、`describe`（整词）、工具名、带下划线的参数名，以及 chaos/flagd/otel-demo 等环境词（test_676…:369-421）。INVESTIGATION_METHOD 禁止出现 pod、checkout、payment、unrelated、工具名等（test_310:84-99）。句数计数器按「句点后接空白」切句（test_instruction_discipline.py:279-280）。

## 1. 需用户裁决

| 编号 | 问题 | 选项与代价 | 推荐 | 影响面 | 裁决 |
|---|---|---|---|---|---|
| U1 | F3 `passes` 是否翻转 | 已有待决（#676 任务记录「待决」），本项不新增 | 不变：本次发布使 base revision 变化，F3 第 3、5 步重跑后照旧由用户按两名审查者结论裁决 | feature_list.json F3（当前 false） | 用户 |
| U2（条件项） | #690 候选句的后半句「a signal you did not look for is not a gap」 | a. 照抄原句：与用户裁决的 #310 U1-a I8「not checked is a gap」字面矛盾，模型可能因此不再把未检查项写进 gaps。b. 改写成「…is unchecked, not unobservable」：意思是没查过的信号要写成「没查」，不能写成「不可观测」，与 I8 一致 | **b**。选 b 不改任何已裁决行为，由 Agent 定，不必问用户；只有坚持用 a 才需要用户裁决（a 会改动 #310 U1-a） | base+ext 的 gaps 写法 | 选 b 时不需要 |

除此以外，本表各项都在用户已批准的方向（#695 第 1–3 条、#690 两句）以内，属于可逆的文字选择，没有新的用户门。

## 2. 可逆默认（Agent 自定，每项附一行理由）

### A. #695（只改 KUBERNETES_GUIDANCE）

| 编号 | 问题 | 选项与代价 | 推荐 | 依据 | 裁决 |
|---|---|---|---|---|---|
| D1 | 新句和现有句的关系、净增句数 | a. 替换 :135，另加 2 句（23→25）。b. 把「重启对象不作根因」并进 :133：:133 会过长，还要保住 restart_configuration 正则要求的词（restart/template/unchanged/workloads/configmaps/read，test_676:492-499）。c. 4 条各写一句（+3） | **a**：:135 换成「对象对应」指认句；新增「结论强度」句、「重启对象不作根因」句；:132/:133/:134/:136 一字不动；多个设置同时开启的情况并进指认句（见 D5），不单独成句 | discipline.py:132-136；顾问第 3 节「净增不超过 2 句」 | |
| D2 | 「开启」怎么写（防止已命中的题退化） | a. 写 enabled / on。b. 按取值判断：「its value in effect is not off, zero or false」 | **b**，指认句里不要用 enabled 这个词：已命中的报告里，设置对象的记录一律是「state ENABLED、defaultVariant off」（S105 t2、S24 t1、S29 t1 的 claim 原文），模型正确驳回了。写 enabled 会让这些题误触发 | 见第 4 节抽查 | |
| D3 | 「点名」怎么写 | a. 「names the workload」（模型要猜怎么算点名）。b. 「its name or description refers to the workload or service」 | **b**：未命中的那些 Run 里，设置靠名称或描述指向服务（S8 t1、S14 t1 的 claim 原文），句子里不放具体名字 | — | |
| D4 | 「无因重启」写法与范围 | a. 任何重启都算，包括 OOMKilled/BackOff 这类容器重启。b. 只算「restarted with an unchanged pod template」（沿用 :133 的说法）或「is failing」，并加上「没有看到其他对象对它施加变更或故障」 | **b**：S35 是命中题，ad 被 OOMKilled，根因是 Schedule；如果容器重启也算，再碰上设置开启，就会误指认 ConfigMap。:132 的「施加者优先」靠这个限定成立 | discipline.py:124、:132、:133 | |
| D5 | 多个设置同时开启时根因写哪个对象 | a. 写持有这些设置的那一个对象，每个设置各写一条 fact。b. 新加一句处理设置分在多个对象里的情况 | **a**，并进指认句（「write each such setting you read as its own fact」）。设置分在不同对象时，交给 L2 reports.py:107-108「指向一个对象才填，否则 null」处理，不新增句子。lite-full-1117 里的双设置题（S14）两个设置都在同一个对象里 | reports.py:107-108；S14 t1 claim | |
| D6 | 指认的是「设置」还是「持有设置的对象」 | 旧句 :135 写的是「name such a setting as the root cause」，有歧义 | 写「name the object that holds the setting」：L2 的 root_cause 要求的是对象的 kind 和 name，设置的键名写在 claim 文本里 | reports.py:97-106 | |
| D7 | 结论强度与 L2 是否一致 | a. 写「把因果关系写成 hypothesis → partial」：重现 D12 已删除的「hypothesis⇒partial」绑定。b. 写成条件：「没看到声明的故障发生，或无法判断设置何时改的 → partial，两点都写进 gaps」 | **b**，新句不提 hypothesis。理由：D12 证据显示「hypothesis⇒partial」会给「拿未验证的假设填根因然后收尾」放行（676 合同 r2 D12）。条件 b 与 L2「supported 需要点名该对象的 fact」不冲突：设置的取值、重启都是 fact；因为改动时间看不到（managedFields 被删），结论实际上总是 partial，root_cause 照填 | reports.py:107-111；kubernetes_core.py:657；676 合同 D12 | |
| D8 | 「除非已确认设置在错误前后没变」这个例外要不要写 | a. 写：现在的数据面无法确认（managedFields 被删），这句永远不会成立，只会让模型困惑。b. 不写，留给 C（managedFields 摘要）那一项 | **b**；issue #695 第 2 条本来也没写这个例外 | kubernetes_core.py:657；#695「后续」 | |
| D9 | 「根因留空」怎么写 | 用 L1 的普通说法「name no root cause and record that as a gap」，不写 `root_cause`/`null` 字段名 | 同左：L1 不内联 L2 字段名（discipline.py:115、#310 U5-a） | — | |
| D10 | 措辞禁区 | — | 不用 describe（A7 整词守卫会拦）、enabled（D2）、e.g./i.e.（计数器会多切一句）、任何设置名、ConfigMap 名、服务名、flag/flagd/chaos。草稿已实测能通过现有守卫（第 5 节） | test_676:369-421；test_instruction_discipline.py:279 | |

参考措辞（已在草稿补丁里跑过现有守卫，实现者可以改，但要保住 D2–D9 的语义）：
- 替换 :135：「When such a setting is switched on, meaning its value in effect is not off, zero or false, and its name or description refers to the workload or service that was restarted with an unchanged pod template or is failing, and no other object is seen applying a change or fault to that workload, name the object that holds the setting as the root cause, write each such setting you read as its own fact, and write the restart and errors as its mechanism and consequences.」
- 新增：「When you did not see the declared fault itself happen, or cannot tell when the setting was changed, set the conclusion to partial and record both as gaps; a setting that is still on while the errors have stopped is not a reason to reject it.」
- 新增：「A workload restarted with an unchanged pod template is not itself the root cause; when you cannot find what triggered the restart, name no root cause and record that as a gap.」

### B. #690（只改 INVESTIGATION_METHOD，两档共用）

| 编号 | 问题 | 选项与代价 | 推荐 | 依据 | 裁决 |
|---|---|---|---|---|---|
| D11 | 位置 | a. 放在 :91 之后（issue 原话）：会把 :91→:92 这条「查无结果→对照名字→相似实体」的连贯链切断。b. 放在 :92 之后，两句相邻 | **b**；8→10 句。测试不钉位置（见 D16），所以这是可逆选择 | discipline.py:91-92 | |
| D12 | 「列出标签名」在 base 档能不能做到 | base 档没有列标签名的工具（`get_all_labels` 只在 ext） | 写成「list the metric and label names that could show it and check them」，不点工具名。base 档可以通过 get_metric_names 加查询结果里每条序列自带的标签拿到这些名字。不改 base 工具描述（base tool_schema_revision 必须不变） | 工具描述原文；test_676:356-359 | |
| D13 | 第 1 句后半句 | 见 U2 | 「a signal you did not look for is unchecked, not unobservable」 | IMPACT_SCOPE :106、:110 | |
| D14 | 第 2 句措辞 | 原句「A gap between samples…」里的 gap 会和报告字段 gaps 混淆 | 「A time range with no samples is unknown, not zero; state the sampled times you rely on.」，与 MISSING_SERIES（冻结、讲缺失序列）分工：这句讲序列在，但某段时间没有样本 | discipline.py:84；test_310:40-44 | |
| D15 | issue 里「查询时也要先列标签名再过滤或分组」要不要另成一句 | a. 另成一句（+1 句，超出「两句」）。b. 并进第 1 句的「list…names…and check them」 | **b**；issue 写的是两句一起改 | #690 正文 | |

### C. 测试、钉值、证据

| 编号 | 问题 | 选项与代价 | 推荐 | 依据 | 裁决 |
|---|---|---|---|---|---|
| D16 | test_676 里两条被新合同取代的断言怎么处理 | 实现者不得改断言（AGENTS.md「独立审查」节）。a. 由独立测试作者在新的 `tests/test_695_…_contract.py` 中改写，同时在 test_676 中删除/改写对应项。b. 留着让它一直失败（不可接受） | **a**，由测试作者执行：①`configuration_cause_requires_matching_failure_path`（test_676:72-86、:154-156、:187-189）改成禁止断言，即旧的「only when … matches … failure path … otherwise … hypothesis」句不得再出现在 K8s 段（与 test_676:548-569 的 D12 写法相同）；②D13 的哈希测试（:514-522）改成「从 INVESTIGATION_METHOD 删掉这两句新句后，sha256 等于 `222e5fa7…`」，K8s 段同理：「删掉 3 句新句、放回原 :135 后，sha256 等于 `1ff52ac3…`」。这样能钉住「除这几句外一字不动」，并且与位置无关；D13 原意（base 没有背景句，:525-545）保留。方向用户已批准，不算新的用户门 | test_676 原文；本机算出的哈希 | |
| D17 | 新合同测试断言什么 | — | 学 test_676 的单句正则加删除变异写法（每条规则一句话满足全部正则，删掉这句后断言转红，再配一个削弱版反例）：**K1 指认**（开启/取值不是 off、名称或描述指向、模板未变的重启或在出错、没有其他施加者、持有设置的对象、根因、机制）。削弱反例 3 个：去掉取值条件、去掉模板未变、把「持有设置的对象」写成「设置本身」。**K2 强度**（没看到声明的故障、改动时间、partial、gaps、仍开着且错误已停、不驳回）。**K3 重启对象**（模板未变、不是根因、找不到触发者、不给根因、gap）。**M1**（不可观测、列指标/标签名、检查、unchecked）。**M2**（无样本、unknown、not zero、采样时刻）。另外 4 条：K 段不得出现 `\benabled\b`；M1/M2 两档 render 里都有；K1–K3 只在 ext、不在 base；句数 25/10/32 | test_676:45-141、:424-469 | |
| D18 | 要不要一条固定回放的行为验收（IncidentScenario→IncidentOutcome） | a. 加：现有设施只有脚本模型/回放模型（tests/m1_investigation_support.py:51 ScriptedModel、tests/acceptance/test_m1_live_flash_replay.py:36 ReplayModel），回放的是预先写好的报告，测不出提示词改动后模型怎么做，等于同义反复。b. 不加；行为证据靠租机真实 Run（冒烟加 F3），PR 附 trace 和 summary.json | **b**。L2 本来就接受 kind 为 ConfigMap、conclusion 为 partial 的 root_cause，这次不改校验，所以不需要新的确定性验收。顾问提的「设置全关 + 模板有变化」对照，改放到租机冒烟的对照题里 | reports.py:97-111；AGENTS.md「验证与汇报」 | |
| D19 | 哪些不该测 | — | 不测整句字面、不测句子位置、不测模型行为结论（LLM judge 不能替代确定性断言）；不给 base tool_schema 加断言以外的东西 | — | |
| D20 | 因 revision 变化要更新的钉值（实现者只改钉值，沿用 676 D10） | — | base prompt `8688f6e52275`：tests/acceptance/test_305_successor_window.py:388、test_347_release_reframe_input.py:203、test_456_forced_final_fence_feedback.py:51、test_f3_step3_coverage.py:148、tests/test_305_successor_window_impl.py:221、test_a7_main_path_impl.py:315、test_f3_pr2_coverage_impl.py:370、test_tool_expansion_a7_contract.py:41。base discipline `9a45deb526fe`：test_a7_main_path_impl.py:316、test_tool_expansion_a7_contract.py:42。render 哈希：test_tool_expansion_a7_contract.py:45 `REPLAY_RENDER_SHA256`。句数：test_instruction_discipline.py:291-293（30→32、8→10、23→25；baseline 24/24、并集 30 不变）。ext prompt `0b066c7cd6d1` 在测试里没有钉值，只出现在证据里。必须不变：base/ext tool_schema、context_policy、REPORT_SCHEMA_VERSION、test_310 的 baseline 哈希（草稿实测都过） | git grep；第 5 节探针 | |
| D21 | F3 证据（base 变了，第 3、5 步要重做） | 沿用 #653/#676 D11 的惯例 | step5 与 f3-acceptance-live 的 run-1..3 移到各自的 `prompt-8688f6e52275/`；step3 新增 `missing-source-rerun-<新后缀>/`（测试按后缀 glob，test_f3_step5_real_runs.py:217）。test_f3_step5_real_runs.py 的 3 条（:147 ×2 run、:222）在租机证据入库前一直失败，所以证据与代码放同一个 PR；step5 照 r2 跑 3 次 | test_f3_step5_real_runs.py:52-56、:217-225 | |
| D22 | 一个 PR 还是两个 | a. 一个 PR（两个 issue 同一次发布，revision 只变一次，F3 只重跑一次）。b. 两个 PR，F3 要跑两次 | **a** | #690「排期」 | |

## 3. 必答类别

1. 时间：只涉及措辞（「无法判断设置何时改的」、「采样时刻」），不引入时钟或容差。2. 计数与口径：句数 25/10/32（D20）；冒烟门 6 次里至少 4 次由 lead 定（顾问第 3 节）。3. 状态转移：不涉及。4. 主体与权限：不涉及，只读面不变。5. 数据合同：L2/schema/校验不变；只有 L1 字节和 revision 变（D20）。6. 旧证据：lite-full-1117、batch-676r2 按旧 ext revision 单独报，不和新结果合并；F3 旧证据按 D21 搬迁。7. 并发与水位：不涉及。8. 外部调用：租机冒烟、F3 重跑、全量都在常设授权内；没有新增数据源。9. 验收投影与 passes：F3 `passes` 翻转是用户门（U1）；ITBench 没有 passes。

## 4. 风险：已命中的题会不会退化（抽查 lite-full-1117 的 105 份 summary）

方法：只读报告 summary、claims、gaps 和 cited_evidence 的文本，用两组正则判断。一组找「设置取值为 on/百分比/not off」，另一组找「unchanged template / restartedAt / rollout restart」。脚本在 scratchpad/pr695/onflag.py。
- 命中 54/54：两组都没匹配到。逐条读了 Deployment 命中题（S105、S16、S20、S23、S24、S33）：模板都有改动（command、image、env、nodeSelector），设置一律「ENABLED / defaultVariant off」，已被驳回。Schedule/NetworkChaos 命中题（S19、S29、S35、S81）同样是设置全关；S35 是 OOMKilled 容器重启，不是模板未变的 rollout。
- 未命中、读到开启的设置：29 次（正则口径；issue 说的是 35，差异来自正则只查报告文本和引用视图）。其中 24 次同时有模板未变的重启：S2、S5、S6、S7、S8、S9、S11、S13、S14、S15。
- 结论：只要遵守 D2（按取值判断，不写 enabled）和 D4（只算模板未变的重启，加施加者优先），触发条件在命中题里不成立，和顾问的判断一致。剩余风险：①模型把「ENABLED」读成开启（靠 D2 堵住，并用 D17 的 `enabled` 禁词测试锁住）；②「is failing」这一支比重启那支宽，命中题里没有反例，但样本里也没有正例，靠租机对照题看（顾问建议 S16 或 S105）。上面这些都是正则启发式，不是逐条人工核对。

## 5. 探针

全量 `pytest tests/acceptance` **未执行**：本机 swap 已用 11.8G/12G，另有 6 个其他会话的 pytest 进程在跑（项目记忆：swap 满时不跑重负载）。改为在 scratchpad 的 `git archive HEAD` 副本上打草稿补丁（INVESTIGATION_METHOD +2 句；K8s 段替换 :135 并 +2 句，即上面的参考措辞），跑引用 revision、discipline、K8s 段、F3 证据目录的 42 个非 integration 测试文件：18 failed / 1102 passed / 34 skipped。其中 3 条是副本环境造成的：没有 .git（test_itbench_lite_driver 2 条）、没有 .venv（test_a7_main_path_impl::test_probe_reads_the_checkouts_own_code），在主 checkout 未打补丁时这 3 条都通过。真实失败 15 条：

- 钉值（D20）：test_305_successor_window::test_item8…、test_347…::test_ac10…、test_456…::test_prompt_revision_is_unchanged、test_f3_step3_coverage::test_item17…、test_305_successor_window_impl::test_revisions_are_unchanged、test_a7_main_path_impl::test_expected_versions_are_the_pinned_code_values、test_f3_pr2_coverage_impl::test_model_visible_revisions_are_unchanged、test_tool_expansion_a7_contract::test_otel_demo_frozen_prompt_and_discipline_values_remain_byte_stable、test_instruction_discipline::test_sentence_census…
- 证据型（D21）：test_f3_step5_real_runs::test_step5_real_run_used_the_frozen_files_and_current_model_surface[run-1]、[run-2]、::test_step3_d9_rerun_of_scenario_a_on_the_current_prompt_revision
- 合同断言（D16）：test_676::test_r2_required_semantics…[configuration_cause_requires_matching_failure_path]、::test_r2_deleting…[同上]、::test_d13_base_investigation_method_is_byte_identical_to_main_83b3b968

草稿下：test_310（禁词、关键短语）、A7 K1–K9 正则、676 其余语义与守卫（含 restart_configuration、D12、D13 背景句）全部通过；base/ext tool_schema 与 context_policy 不变；草稿 revision 为 base `e74f898baaec`、ext `bc0fdf7f2ba6`（只作示意，最终以定稿为准）。实现者在内存允许时补跑一次全量 acceptance。

## 未确认

- PG 门控与 live 类验收没有跑（需要 M1_DURABLE_POSTGRES=1）。
- 「is failing」一支在样本里没有正例，误伤面只能靠租机对照题确认。
- 回放快照里的 ConfigMap 是否带 creationTimestamp/resourceVersion 等能辅助判断改动时间的元数据：未查（属于 C 那一项）。
