# 第 1 组 PR 合并决策审阅摘要

## 证据范围与共同结论

本轮读取了任务书、AGENTS.md 的 Code Review Rules、PRODUCT-CONSTRAINTS 全文、C3 第 4–8、13 节及相关上下文、ROADMAP/进度记录，在线读取 PR 描述、当前 HEAD、文件清单和 CI，读取本地对应提交的产品代码及测试差分。没有修改仓库文件、切换分支、启动 PG、读取 .env、调用模型或运行付费实验。未运行 pytest：PG 测试会写数据库，本轮只核查测试源码，不把实现者提供的通过数字当作本轮复验。

当前七个 PR 均 OPEN、base=main、mergeStateStatus=CLEAN，checks 与 m0-postgres 均 SUCCESS。固定 HEAD：#20 aff4586a；#21 5e581ee6；#26 8ff58806；#27 cf8f17d8；#34 f67446e2；#35 a85b4583；#36 080c62cc。下文文件行号针对各自 HEAD。CI 成功不代表新增 PG 用例实际被 CI 启用，也不代表产品验收通过。#21/#35/#36 的机器人摘要明确出现 Code Review Failed，不能当成无发现；本轮未逐个拉取全部 review thread，故不认证 AGENTS 要求的审查闭环。

判断：#34/#36 是较窄的修复；#35 是可独立评估的续租 API，不能当作 worker 已接线。#21 是合同层。#26 有错误码及 DDL 边界需接受。#27 保留三项显式决策。#20 的工具时间预算仍存在下述源码可定位缺口，不宜据其“跨 attempt 持久化”描述直接判断预算红线已闭合。

### 合并实测方法

对七个本地 origin 分支全部 21 对运行 `git merge-tree origin/main origin/<A> origin/<B>`，检查实际 `+<<<<<<<` 冲突标记；不能以正文出现 CONFLICT 字样、或 changed in both 作为冲突判据。实测冲突仅有：

- #21 × #26：tests/test_architecture.py。
- #26 × #27：tests/integration/test_m1_durable_state_postgres.py。
- #26 × #34：opspilot/persistence.py；tests/integration/test_m1_durable_state_postgres.py。
- #27 × #34：tests/integration/test_m1_durable_state_postgres.py。

其余 17 对无文本冲突，包括 #20 × #26、#26 × #35、#34 × #35。#20 描述中“install() 会冲突”未被当前实测支持。三参数模式固定 main 作共同基线；没有创建真实合并提交、没有验证全组累计合并后的运行语义。先后合并仍须重算并复验，不能把两两无冲突当整组可直接上线。

## PR #20 — 工具网关、证据投影与持久工具计量

### 1. 一句话

增加只读工具注册、精确目标授权、控制复核、raw/view/hash 证据及跨 attempt 用量记账；但尚不能证明原子工具预算与崩溃未知耗时红线闭合。

### 2. 接口形状（8 项）

- ToolRegistration / ToolDescription / ParameterSpec：工具执行合同与五字段模型描述。
- ToolRegistry / TargetRegistry：内容哈希注册表及不可变目标解析。
- QueryScope：Controller 授予的主体、Run、目标、工具、窗口、deadline 和上限。
- ReadOnlyToolExecutor.execute(ToolRequest)：执行一项经授权的只读查询。
- ReadOnlyTransport.fetch / TransportRequest：固定 endpoint、凭据句柄、时间和体积上限的传输接口。
- ControlAuthority.snapshot / EvidenceSink.register：可信控制读取与提交后可消费的证据接口。
- ToolOperation / ToolOutcome / EvidenceRecord：稳定操作身份、五类结果及原始/投影证据。
- ToolUsageLedger / DurableToolLedger / DurableStore.charge_tool：Run 用量读取与累计；新增 opspilot_tool_charges 表和 runs 两个用量列。

### 3. 场景清单

- 给定未授权目标，当 execute，则不发查询；确定性测试：test_a_registered_but_unauthorized_target_is_denied。
- 给定 control lookup 消耗到 deadline，当派发，则拒绝；测试：test_a_deadline_that_expires_during_the_control_lookup_is_denied。
- 给定飞行中暂停，当结果返回，则只留历史、不送内容入模型；测试：test_a_suspension_during_flight_keeps_the_result_as_history_only。
- 给定暂停与 deadline 同时发生，当复核，则优先报告 SUSPENDED；测试：test_an_in_flight_suspension_is_reported_even_when_the_deadline_also_passed。
- 给定新 executor，当 ledger 已有用量，则从既有值开始；测试：test_the_executor_resumes_counting_from_the_ledger_not_from_zero / test_the_time_budget_also_resumes_from_the_ledger。
- 给定账本不能提交，当执行/结算，则停止读取或拒绝采纳；测试：test_a_ledger_that_cannot_record_the_operation_stops_the_read / test_a_result_whose_cost_cannot_be_settled_is_not_adopted。

以上为存在测试源码，不是本轮实跑结果。

### 4. 红线核对

- 只读权限：PASS（纯逻辑范围）。registry.py:276–286 拒写 verb 和保留参数；executor.py:225–227 再守 read_only。真实 transport、OS/网络最小权限不在本 PR，不能据枚举证明隔离。
- 人工控制优先：具体疑点。executor.py:449–485 的 control/deadline 检查后，:517 会先执行可能阻塞的 ledger charge，再在 :524 fetch；中间没有重新检查 deadline/控制。慢账本可能让读取实际发出时授权已过期。另 :599 snapshot 与 :630 register 非原子，EvidenceSink 协议只有 register(record)，需组合层保证提交事务检查当前授权。
- 业务记录恢复权威：PASS（账本存储方向）；组合层接线未证明。DurableToolLedger 为 PG 包装，但 PR 自述 #29/#30/#33 未接线，不能认为 Workbench 重启已经使用此账本。
- 秘密与数据出口：具体疑点。executor.py:21–25 明示尚无源 payload 脱敏，:712 直接保留结果行；registry.py:223–234 仅检查描述非空/占位符，不阻止描述含 endpoint/凭据。可信配置需要人工审核，注册结构校验不等于出口红线。
- 预算与 deadline：具体缺口。executor.py:517 只预记 0 秒，:539–545 结算失败仍留 0 秒；崩溃后已花耗时不保守占用，违反 C3 §13 未知用量不释放的原则。persistence.py:346–369 只累加、不原子比较上限；executor.py:343–355 仅构造时读取预算。多个同 Run executor 可基于同一余额同时通过 :456/:470 检查，缺少数据库原子“检查且预留”工具预算。

### 5. 疑点与怎么验证

- 优先验证未知耗时：预 charge 后模拟 worker 死亡，重新建 executor；应看到保守占用而非 0 秒。当前代码注释已承认结算失败只保留次数，不能只测成功结算后重启。
- 验证并发预算：同一有效 lease 建两个 executor，共读 19 次已用，屏障后各执行一次；数据库必须保证最多一个获准，现 charge_tool 无上限检查。此为源码推导，本轮未运行 PG 反例。
- 验证慢账本：假 ledger.charge 推进可信时钟越过 deadline，断言 transport 从未被调用；现有慢 control 测试不足以覆盖新增 charge 间隙。
- 验证最终采纳竞态：snapshot 后暂停、register 前设屏障；要求持久证据不以 adopted=true 进入模型。需要真实 EvidenceSink/controller 合同，不能仅测假的 control 快照。
- PR body 的“CI 红、BLOCKED、旧 HEAD”已过期，当前双绿/CLEAN；应修正文档，不能据旧文字再要求历史改写授权。

### 6. 合并依赖与冲突

base=main；本 PR 无本组必须先合的编译依赖。#29/#30/#33 的真实组合层需要接其 ledger；#27 的 L1/L3 语义归位是后续联动。与本组其余六个 PR 两两均无文本冲突；特别 #20/#26 的 persistence 自动合并成功，但计量栅栏与统一 _lease_revoked 语义仍须复核。当前新增预算问题应先处理或明确留作阻塞，不能靠无冲突放行。

### 7. 用户拍板事项（原文）

PR 描述：“数据源 payload 内部机密的脱敏策略未建立；在其成型前，可能返回机密的数据源不应登记”。这是需维持的接入边界，不是授权模型读取秘密。

PR 描述：“组合层（#29 loop / #30 worker / #33 web）尚未接入 DurableToolLedger”。需决定本 PR 仅按基础接口合并，还是等组合验证闭合；原子预算/未知时间缺口不能仅以接线范围外解释。

历史原文：“四条处置路径均超出本任务授权，须由用户决定”。它指旧 secret-scan 历史处理；当前 CI 已绿，本报告不把该历史请求重新升级为现时待决。

## PR #21 — 认证 intake 合同

### 1. 一句话

增加认证身份与请求合同、三态幂等判断和安全错误表示；不等于已经提供认证 HTTP 入口。

### 2. 接口形状

- Principal：actor_id、ui_basic/event_token 通道、auth_revision。
- IntakeRequest：尚未 registry 解析的 target_id、question、idempotency_key。
- IntakeEnvelope：request_id、身份/请求及带时区 received_at。
- verify_channel：重新校验对象并校验入口通道。
- classify_intake_delivery：same_request / key_conflict / different_request。
- sanitized_errors：结构化错误去掉 input，extra 字段名末段脱敏。
- 共享 DTO.model_config：增加 hide_input_in_errors，影响全部 domain DTO 错误字符串。

### 3. 场景清单

- 给定 UI/event 身份互投，当 verify_channel，则拒绝；测试 test_each_channel_is_accepted_only_at_its_own_entry_point。
- 给定相同内容、不同 receipt/request_id 的重试，当分类，则 same_request；测试 test_a_redelivery_of_the_same_request_is_recognised_as_the_same_request。
- 给定旧 key 配新 actor/channel/auth_revision，当分类，则 key_conflict；测试 test_a_reused_key_under_a_different_identity_is_a_conflict。
- 给定旧 key 配不同 target/question，当分类，则 key_conflict；测试 test_a_reused_key_over_different_content_is_a_conflict。
- 给定新 key，当分类，则 different_request；测试 test_a_fresh_key_is_a_new_request_not_a_conflict。
- 给定校验错误，当产品渲染错误，则走 sanitizer；测试 test_product_code_renders_validation_errors_through_the_sanitizer（静态调用扫描）。

### 4. 红线核对

- 只读权限：PASS；纯 DTO/分类，无数据源执行。
- 人工控制优先：PASS（变更未改控制状态）；本 PR 不实现 expected_version 控制入口。
- 业务记录恢复权威：PASS（未引入其他权威）；持久化 accept、事件确认和审计未接线。
- 秘密与数据出口：具体边界。intake.py:14–23 明确类型不能区分合法 actor 字符串和误传认证头，adapter 必须隔离；domain/base.py:47–70 的 sanitizer 只裁 input/extra loc，保留 msg，不能一般化声称对所有自定义含秘密 validator message 都安全。
- 预算与 deadline：PASS（不发调用、不改预算）；此层未提供执行限额。

### 5. 疑点与怎么验证

- 单 Principal 类型不能静态强制入口调用 verify_channel；在 #33 真实认证入口做双向 token/UI 凭据互投测试并查调用点。
- sanitizer 的 msg 与非 extra 的动态 loc 在未来映射字段中可能带输入；用含秘密的自定义 ValueError/动态 dict key 构造反例，按实际 DTO 面决定是否只暴露固定错误码。现时 intake 校验器使用固定错误码，不能将潜在扩展问题说成现时已泄漏。
- 架构测试禁止 opspilot 全部 .json()/.errors()，并非类型感知；与后续 HTTP response.json 接线可能误冲突。验证真实组合层，避免为过宽测试削弱日志安全。

### 6. 合并依赖与冲突

base=main，无本组硬前置。与 #26 实测 tests/test_architecture.py 冲突；其余五对无文本冲突。必须保留 sanitizer 和 SQL qualified-star 两套测试，不能二选一。

### 7. 用户拍板事项（原文）

“需要用户决定：两条认证通道是否提升为类型层不可互换。当前是一个 Principal 类型 + 运行时 verify_channel，调用方忘记调用没有静态信号。收紧或下调合同表述都可接受，需选一条。”

旧 secret-scan“补救方案待用户决定”是历史状态；当前 CI 双绿，不依据旧正文请求重写历史。

## PR #26 — DurableStore SQL 与租约加固

### 1. 一句话

显式区分事故代际与 Run 副本，统一租约栅栏、过滤失效 pending tools 并收紧控制错误码，减少旧授权写入和错误重试。

### 2. 接口形状

- reserve_budget / commit_step / commit_tool / publish：显式列和统一 _lease_revoked 守卫。
- control：unknown identity、inconsistent state、generation conflict 分离；非 cancel 放行状态集合收紧。
- rebuild：pending_tools 按事故当前代际过滤，历史 steps 保留。
- install：新增 runs/controls incident_id 索引。
- _lease_revoked（内部）：owner、epoch、incident generation、非空有效租约、deadline 单一判断。

### 3. 场景清单

- 给定 run/incident 代际分叉，当四条写路径校验，则以事故为权威；测试 test_generational_fence_is_keyed_to_the_incident_not_the_run_copy。
- 给定租约被清空但 owner/epoch 相同，当旧 worker 写，则拒绝；测试 test_a_cleared_lease_cannot_be_used_even_when_owner_and_epoch_still_match。
- 给定 follow-up 推进代际，当 rebuild，则旧 pending 不重派；测试 test_rebuild_drops_pending_tools_from_a_superseded_generation。
- 给定事故不存在，当 control，则 UNKNOWN_IDENTITY；测试 test_control_distinguishes_unknown_identity_from_a_retryable_conflict。
- 给定未开放 run 状态，当非 cancel 动作，则 ILLEGAL_TRANSITION；测试 test_non_cancel_control_is_refused_from_every_unlisted_run_state。
- 给定 current_run_id 指向别的事故，当 control，则 INCONSISTENT_STATE 且状态不推进；测试 test_control_refuses_a_current_run_pointer_into_another_incident。

### 4. 红线核对

- 只读权限：PASS；只修改产品自身 PG 记录，没有新增目标系统写操作。
- 人工控制优先：PASS（本改动收紧）；代际取 incident，cancel 兜底保留。
- 业务记录恢复权威：PASS；PG 恢复、旧步骤保留、当前代际筛选一致。未证明并发死锁压测。
- 秘密与数据出口：PASS；无新网络/模型出口，SQL 参数绑定保留。
- 预算与 deadline：PASS（统一守卫范围）；索引 install 是 DDL，PR 不构成对共享/生产库执行授权。

### 5. 疑点与怎么验证

- CREATE INDEX 非 CONCURRENTLY，install 有 4s lock/5s statement timeout；对活跃大库可能失败。验证隔离等规模库的并发写入、超时与回滚，另定迁移机制。
- control 新的两步锁路径缺真实交错压力验证。用控制/提交/publish 并发事务和 lock timeout 注入复验；源码锁序一致只是推断。
- 当前 #26 仍保留旧 claim 的版本优先问题；#34 是它的独立修复。不能把统一 SQL 列名当人工暂停/版本冲突已解决。

### 6. 合并依赖与冲突

base=main；A 类可单独评估，完整人工控制要整合 #34。实测冲突：#21 的 tests/test_architecture.py；#27 的 durable PG 测试；#34 的 persistence.py 与 durable PG 测试。与 #20/#35/#36 无文本冲突。冲突解完需带 PG 复验，不能只跑默认 skip 的 pytest。

### 7. 用户拍板事项（原文）

“请用户特别确认：control() 的完整行为 delta”。唯一公开可达错误码变化：事故不存在从 CONTROL_CONFLICT 改 UNKNOWN_IDENTITY；另有不一致存储状态下的拒绝收紧。

“B 类、C 类未实施（依赖边界 / psycopg_pool / schema 演进机制 / 状态词汇单一来源 / 行宽 lint），按任务记录仍待用户决定。”这些不应在本次 A 类合并中默认为获批。

## PR #27 — 纪律模板与版本来源

### 1. 一句话

提供按变体组织的 L1 模板、渲染和版本哈希来源，保持历史候选 prompt 字节；版本摘要长度等仍待裁定。

### 2. 接口形状

- Segment / VARIANTS：冻结的有序模板与实例/报告槽位。
- render：填预算、报告契约和服务列表。
- template_projection：不含实例值的模板/结构投影。
- discipline_revision：L1a 内容版本。
- prompt_revision：L1a 与 L2 复合版本。
- prompt_face_sha256：实际渲染字节完整摘要。
- UnknownVariantError：未知变体拒绝。

### 3. 场景清单

- 给定 replay-candidate，当渲染冻结预算/契约，则复现证据 hash；测试 test_replay_candidate_reproduces_the_frozen_m0_prompt_hash。
- 给定模板文本变动，当计算 revision，则变化；测试 test_template_byte_change_bumps_the_revision。
- 给定实例预算变化，当渲染，则 face 变而 revision 不变；测试 test_face_hash_moves_with_instance_values_while_the_revision_holds。
- 给定槽位重排，当计算 revision，则变化；测试 test_reordering_a_report_or_instance_slot_bumps_the_revision。
- 给定一次性授权列表迭代器，当 render，则列表不被校验耗空；测试 test_a_one_shot_iterator_does_not_get_silently_drained_to_nothing。
- 给定无预算槽位变体收到非 1 预算，当 render，则拒绝；测试 test_budgets_for_a_variant_without_the_slot_are_refused_not_dropped。

### 4. 红线核对

- 只读权限：PASS；纯渲染模块，无工具执行权限。
- 人工控制优先：PASS（无状态写）；授权实例不进入 revision，控制代际仍需执行层承担。
- 业务记录恢复权威：具体疑点。discipline.py:288–317 只输出 12 hex 摘要；C3 §5 要求“比对仍用完整值”，实现与规范口径待决；产品 prompt_revision 接线本 PR 未实现。
- 秘密与数据出口：具体边界。render 接受调用者 report_contract/服务文字，不是运行时秘密过滤器；测试 credential 正则约束模板样本，不证明任意输入都无秘密。
- 预算与 deadline：PASS（不改变执行预算）；model_requests 文本不能替代实际预算执行。

### 5. 疑点与怎么验证

- 48 bit 截断摘要是否符合合同，不能由审查者替用户确认。建议完整摘要用于兼容性判断、短码仅显示，改后测试持久 versions/claim。
- 投影含 key/tool_specific，内部改名也 bump；用仅改 key 的差分验证 render 完全相同而 revision 变化，再决定是否接受额外 blocked。
- 两个 baseline 只锚定历史拼装片段，没有 Holmes 外层完整 hash 的 CI 证明；需要固定 checkout 才能重算，不因候选 hash 通过外推。

### 6. 合并依赖与冲突

base=main，无必须先合 #20 的代码依赖；后续 tool-specific 文案迁移需要两者都合入。#26/#27、#27/#34 在 durable PG 测试实测冲突；其余组合无文本冲突。当前 #20 已新增 ToolDescription，#27 中“尚待注册表”应按最新合并状态更新。

### 7. 用户拍板事项（原文）

1. “revision 该截断还是用完整哈希比对？”——A 保持短码 revision，B 使用未截断摘要。
2. “template_projection() 该只含模型可见字节，还是也含结构元数据（key/tool_specific）？”——A 收窄投影，B 接受内部标注触发 bump。
3. “C3 第 7 节第 6 项（来源索引静态检查）与已批准的归位决定相冲突”——A 新建结构来源索引，B 确认该项废止。

第 3 项原文中的“C3 第 7 节七项检查”不能按字面当当前 C3 §7：当前 §7 是故障恢复合同，七项清单来自任务历史材料。报告保留原文并指出来源名称混用，避免悄悄修改批准合同。

## PR #34 — claim 保留人工状态

### 1. 一句话

部署版本变化后，claim 不再把暂停、取消和完成状态覆盖成 blocked。

### 2. 接口形状

- DurableStore.claim：状态守卫先于 versions 比较。
- CONTROL_DENIED：不可领取状态拒绝且不更新。
- INCOMPATIBLE_STATE：只把 queued/running 转 blocked；已 blocked 不静默恢复。

### 3. 场景清单

以下由 test_version_change_cannot_override_a_paused_cancelled_or_completed_run 覆盖（已读源码，未实跑）：

- 给定 paused，当新版本 claim，则仍 paused，人工 resume 可行。
- 给定 cancelled，当新版本 claim，则取消终态不变。
- 给定 completed 与已发布结论，当新版本 claim，则结论/状态不变。
- 给定暂停后显式 resume，当不兼容 claim，则 blocked。
- 给定 blocked，当旧版本重新匹配，则仍拒绝，不静默恢复。

### 4. 红线核对

- 只读权限：PASS；仅自身 PG 状态。
- 人工控制优先：PASS；persistence.py:211–228 先拒人工终态，再处理不兼容。
- 业务记录恢复权威：PASS；已提交控制状态/结论不被版本尝试覆盖。
- 秘密与数据出口：PASS；无新出口。
- 预算与 deadline：PASS（未改）；deadline 检查仍在状态守卫之前，因此过期暂停会报 DEADLINE_EXCEEDED，但不改写人工状态。

### 5. 疑点与怎么验证

- #26 改 run_state 别名，本 PR 仍使用 row['state']；解决文本冲突时须同步状态键，否则可能 KeyError 或漏守卫。执行合并后的 PG paused/cancelled/completed/blocked 全场景。
- 一个综合测试覆盖多分支，中间失败会遮住后面的结果；验证时逐场景观察 DB 最终状态，不只看测试名。

### 6. 合并依赖与冲突

base=main，无硬前置。与 #26 冲突 persistence.py 和 durable PG 测试；与 #27 冲突 durable PG 测试；其余无冲突。建议与 #26 合并冲突一并处置、PG 复验，不丢任一方回归。

### 7. 用户拍板事项（原文）

无新增产品选项。PR 提示：“与 PR #26 后合并者会在 claim() 同一函数出现可手工解决的冲突”。本轮证实，且还存在测试文件冲突。

## PR #35 — 租约续期 API

### 1. 一句话

新增只能续有效持有者租约、且不超过 Run deadline 的 API，为短租约 worker 恢复提供基础；尚未接入 worker。

### 2. 接口形状

- DurableStore.renew_lease(lease, extend_seconds) -> datetime：返回数据库时间下的新 lease_until。
- INVALID_INPUT：非正 extend。
- CONTROL_DENIED：非 running、owner/epoch/事故代际不符、空/过期 lease 或过 deadline。
- 无新表、无新端点；只更新现有 runs.lease_until。

### 3. 场景清单

- 给定有效持有者，当续期，则身份不变且延长；测试 test_holder_renewal_extends_without_changing_identity。
- 给定更长的已有 lease，当短 extend，则不缩短；测试 test_renewal_never_shortens_a_longer_remaining_lease。
- 给定过期 lease，当续期，则拒绝；重新 claim 必须新 epoch；测试 test_expired_lease_cannot_be_revived_only_reclaimed_with_a_new_epoch。
- 给定 pause/cancel/correct/follow_up，当旧 lease 续期，则拒绝；测试 test_human_control_revokes_the_lease_and_renewal_is_refused。
- 给定单独事故代际变化，当续期，则拒绝；测试 test_generation_change_with_same_owner_and_epoch_is_refused。
- 给定 deadline 临近或已过，当续期，则封顶/拒绝；测试 test_renewal_is_capped_at_the_run_deadline_and_refused_after_it。

### 4. 红线核对

- 只读权限：PASS；仅自有业务租约更新。
- 人工控制优先：PASS；persistence.py:260–281 事故先锁，按 incident generation 拒绝旧权。
- 业务记录恢复权威：PASS（API）；未接线不等于硬杀后已能快速接管。
- 秘密与数据出口：PASS；无新调用/出口。
- 预算与 deadline：PASS；:285 LEAST/GREATEST 保证不越 deadline、不缩短更长有效 lease，不重置预算。

### 5. 疑点与怎么验证

- extend_seconds 仅做 <=0 判定（:255），类型注解不执行 runtime 检查；bool/float/极端大整数行为未覆盖。用这些值调用并确认稳定 INVALID_INPUT，而非 DB 异常或意外接受。低风险合同严谨性问题，不是已证明权限绕过。
- PR 推荐“每次 committer 前续期”不是任意长调用的 heartbeat。接线后用最长模型调用、工具耗时和 CPU 阻塞测试租约不提前过期，并硬杀确认接管时间。
- scope suspension 尚不在此 API 独立出现，需组合层明确其如何推进事故代际/撤销租约；不能以本方法代替完整 suspension 合同。

### 6. 合并依赖与冲突

base=main；无本组硬前置，与其余六 PR 均实测无文本冲突。与 #26 共用租约语义，可后续收敛辅助函数，但合并本 API 不代表 #30/#33 调用方完成。

### 7. 用户拍板事项（原文）

无独立产品决策。交接原文：“使用方接线不在本 PR”；“建议默认 lease_seconds=420（模型单请求上限 360 s + 60 s 余量），每次 committer 调用前续期”。建议数值尚不是用户批准的新冻结值，本报告不代为批准。

## PR #36 — 私有探针 invalid-body 冷启动

### 1. 一句话

把第三方/业务 import 延后到请求体校验之后，让非法输入不为 HTTP/PG 大依赖冷启动付费，减少原 5 秒测试的负载超时。

### 2. 接口形状

- scripts/m0_pg_private_transport.py.main：参数与 body 校验后才导入 httpx2、预算、配置和 StepStore。
- 无新增公开 API/表/端点；固定私有 pipe 协议和 DeepSeek URL 保持不变。

### 3. 场景清单

- 给定 invalid body，当私有子进程启动，则在配置/HTTP/PG import 前拒绝；现有确定性测试 test_private_worker_invalid_body_has_no_export_or_config_read。
- 给定合法 body，当通过校验，则继续既有 send_guard、固定 endpoint 请求和私有输出；本轮静态核对，未运行合法请求（会读凭据/出网）。
- 给定异常，当脚本退出，则保持 os._exit(2)，不输出异常或 provider body；同文件 :108–113 静态证据。

PR 描述中的冷 venv 40 次、30 次循环、PG 36 passed×4、全量两轮是实施者记录，本轮未独立重现，不列为本轮实测。

### 4. 红线核对

- 只读权限：PASS（变更范围）；只移动 import，不新增被调查系统权限。
- 人工控制优先：PASS（变更未触及）；send_guard 仍保留。
- 业务记录恢复权威：PASS（变更未触及）；StepStore/PG 路径未重写。
- 秘密与数据出口：PASS（差分）；非法 body 更早拒绝，合法路径仍读取固定 .env 并只用于认证。此审核未执行合法路径。
- 预算与 deadline：具体时序疑点。:15 的 timeout 现在在重型 import 之前计算，而原版 import 在 main 前；冷 import 可消耗剩余 deadline，HTTP client timeout 却仍用旧值。:68 的发送 trace 会再查 deadline，可防过期发送，但连接阶段/整体墙钟边界需父进程负责。不能仅凭“import 搬移”声明所有合法路径时间语义完全等价。

### 5. 疑点与怎么验证

- 用不出网的 fake httpx2/假时钟延迟 import，检查剩余 deadline、send_guard 和父进程 kill 超时；证明即使冷 import 很慢也不越 Run 总 deadline继续运行。
- 脚本保留 deepseek-v4-flash（:23）属历史探针，当前前瞻配置已切新名；确认调用方是否仍使用，不能本轮顺手改写历史实验。
- 独立审查“无发现”与 CI 不等于重现 macOS 冷原生扩展签名延迟；若要求重现根因，需原始计时输出，不能把解释当确定性事实。

### 6. 合并依赖与冲突

base=main，无本组硬前置；与其余六 PR 两两无文本冲突。只改该脚本与独立任务记录，默认不涉及产品运行时。

### 7. 用户拍板事项（原文）

无新增决策。PR 原文：“本任务为工程维护，不改变任何功能门槛/验收状态，ROADMAP 无需更新。”本轮同意按维护范围评估，但保留上述时间语义验证边界。

## 交付边界

本报告完成审阅摘要，不批准合并，不修改 PR、不解决冲突、不触发机器人。对红线给出 PASS 的条目仅限本次变更及已核查源码，不表示完整产品红线验收。#20 的原子预算/未知耗时和新 charge 间隙值得优先复验；其余明确决策见各 PR 第 7 节。未来实际合并前须重新核对当前 HEAD/CI/适用审查、解决实测冲突并保留双方测试。
