# F6 独立验收与合同测试作者

- 状态：最后一轮修订完成、机器人审查按停机规则收口（外部场景尚未接线，F6 未完成）
- 更新日期：2026-10-07
- 依据：[M1-02](2026-10-03-m1-02-recovery-observation.md)；[测试映射与接口提议](../testing/f6-acceptance-tests.md)
- 工作区：`/Users/shenghuikevin/dev/AI/production-ops-agent-f6-tests`；`feature/m1-02-f6-acceptance-tests`；基线 `0c97377`

## 目标与范围

按 C3 与 F6 五步独立编写纯函数采纳合同测试，以及接线前 skip 的外部场景。
不读并行实现、不改产品、依赖、迁移或验收原文。提交 PR，不合并。
接口格式选用显式字段与原始信号字典，便于只断言可观察结果；属于合同提议。

## 执行进展与证据

- 已确认工作区干净、分支与用户给定一致；fetch 后 origin/main 仍为 `0c97377`。
- 用户限定只能操作本 worktree，所以未同步主 worktree 或清理其他会话 worktree。
- 定向 pytest：54 passed、26 skipped、0 xfailed，0.29 秒；命令见测试说明。
- 独立审查首轮发现 4 项外部场景缺口，已补新鲜度/覆盖/处置前数据、信号阈值与低流量、零模型调用审计、重放独立重算；全新上下文审查者已静态复验受影响项，无未处置合同发现。
- 最终 `make check` 退出 0：锁检查、Ruff lint/format、mypy 通过；pytest 2654 passed、337 skipped、2 既有 xfailed，44.76 秒。两个 xfail 是既有 persistence/domain 架构债，不是 F6 缺陷。
- 独立审查仅做合同/断言静态审查；pytest 执行数字由作者本地运行记录，不混称独立运行验收。
- 未运行模型、trace、实验环境或遥测调用；费用 0，无供应商调用需对账。
- 首版纯函数合同未发现可复现不一致；当时 26 个外部场景尚未接线，不能认定 F6 完成。

## 审查处置（PR #112 追加修订）

- Bot P1：所有 run outcome 与请求 subject_id 绑定，采样也绑定主体；同 target 双事故交换被处置身份，分别核对观察中/确认后状态，另一事故的生命周期、会话、采样完全不变。
- P2-1：补控制 generation=4、观察 generation=3，大于当前值同样 history_only。P2-2：补 H,H,D 与 H,H,D,H,H，分别重置为 0、后续连续健康最多 120 秒；期限前不结束/不交接，期限后回 open 交接。
- P2-3：拆出 latest_sample_verdict 与 recovery_confirmed，不冻结窗未满 verdict；P2-4：测试-owned 环境/遥测桩记录并拒绝写请求和重放查询，driver 持有计数，另加 5 个可执行夹具负向检查；P2-5：Target 补 integration_id。
- O1：文档列冻结语义与实现者可自定接口两清单，原因码为 harness 规范化码；O2：逐信号 evidence_id 唯一，可读原始 bytes 时核验 SHA-256；O3：篡改重放接受重算还原或显式完整性不一致，不能复述错误判定；O4：补先健康后撤流量且错误下降序列。
- O5：文档说明运行时 reopen/新阶段未覆盖，因为移出 M1-02；O6：skip 补 #86/#87 和持久采样 #84；O7：删除非生命周期取值/触发器的空转参数；O8 按用户要求不处理，最低样本由合同/profile 决定。
- 全新上下文静态复验额外发现 H,H,D 可提前结束的缺口，已收紧，静态复验无未处置合同发现；不是产品运行验证。定向 pytest：47 passed（42 领域 + 5 夹具）、33 skipped、0 xfailed，0.30 秒；最终 `make check` 退出 0，锁/Ruff/mypy 通过；2647 passed、344 skipped、2 既有架构债 xfailed，44.59 秒。
- 本次仅追加提交和普通 push；不再触发机器人、不回复/resolve thread、不合并，lead 接手审查处置。

### 第二轮机器人 P2（943724a 之后）

- 模型边界：测试-owned `model_request` 先计数再抛错；原始与重放均断言 driver.model_calls 为 0；补两例驱动吞掉拒绝仍可检出的夹具检查，产品自报审计保留但不再单独充当证明。
- unknown 留底：合法 stale/coverage-gap 调用 assert_saved_basis，逐样本 adopted、原信号和来源保留，used_sample_count=3、恢复 unknown 且未确认、全 stale 健康时长为 0；处置前数据拆成独立场景，不冻结采纳方式。
- 窗口重建：H,H,D,H,H 恰为 120 秒；新增原始期限 420 秒/次数 7 的冻结 profile，H,H,D,H,H 仍观察、H,H,D,H,H,H 达 180 秒并 resolved，不在观察途中延长预算。
- 定向 pytest：49 passed（42 领域 + 7 夹具）、35 skipped、0 xfailed，0.34 秒；全新上下文静态复验指出 stale 健康窗仍可增长，已补等于 0 并复验，本轮无未处置合同发现；最终 `make check` 退出 0：锁/Ruff/mypy 通过，2649 passed、346 skipped、2 既有架构债 xfailed，45.83 秒。35 个外部实例未接线，不是 F6 验收通过。
- 仅追加提交、普通 push；不触发 @codex review、不回复或 resolve thread、不合并，仍由 lead 接手审查。

### 第三轮机器人 P1/P2（442c7f3 之后）

- P1 跨目标：fixture 预种事故并保留不可变目标；assert_saved_basis 同时核对预种目标、事故快照目标、profile 和已存采样目标。新增 5 个目标身份字段变体，先空采样 run 授权，再 continue_observation 提交错误目标，核对 history_only 留底、无恢复确认、生命周期/采纳水位/健康窗/预算未推进；原授权身份、绑定与版本、处置审计不变，历史采样仍指向原会话。
- P2 证据解析：read_raw_payload 是必需接口，缺失/None/非 bytes 即失败；每个证据引用均核验原始字节 SHA-256，包括错误目标历史样本；补 3 个可执行 reader 拒绝检查，文档删除可选读取表述。
- 定向 pytest：52 passed（42 领域 + 10 夹具）、40 skipped、0 xfailed，0.27 秒；最终 make check 退出 0：锁/Ruff/mypy 通过，2652 passed、351 skipped、2 既有架构债 xfailed，45.70 秒。全新上下文静态复验补充要求核对原授权及处置审计，已修复并复验，无未处置合同发现。外部场景仍未接线，未改产品代码或 passes。
- 仅追加提交与普通 push；不触发机器人、不回复/resolve thread、不合并。

### 第 4 轮：最后一轮修复与停机收口（f3a9bad 之后）

- P2 信号绑定：每个场景修改刺激后生成测试-owned raw_payload bytes，桩按查询/绝对窗口返回这些字节；证据读取必须逐字节等于刺激，随后核 SHA-256；补可运行桩检查，不再只验证产品字节与产品摘要自洽。
- P2 有界继续：低流量及依赖退化 count=3（180 秒，原预算 300 秒/5 次）均要求 observation_ended=False、无交接/交接原因。
- P1 会话/任务：错误目标提交前后完整 observation_sessions 不变；若暴露 sample_jobs，完整集合也必须不变，不限于当前授权。
- 定向 pytest：53 passed（42 领域 + 11 夹具）、40 skipped、0 xfailed，0.35 秒；最终 make check 退出 0：锁/Ruff/mypy 通过，2653 passed、351 skipped、2 既有架构债 xfailed，45.57 秒；限定静态复验确认三项已处置，无 C3/PRODUCT-CONSTRAINTS 原文支撑的新 P1。
- 机器人审查已按用户停机规则收口：本轮三项合在一个追加提交，此后仅修能引用 C3/PRODUCT-CONSTRAINTS 原文的 P1；不继续扩展 P2/可选项。普通 push，不触发 @codex review、不回复/resolve thread、不合并。

- 停机后 C3 §10 P1（7a992ad 之后）：依据「失效结果只保留历史……也不安排后续任务」，删除 sample_jobs 条件守卫，完整任务集合必须提供且前后相等；文档明确任务五字段及会话合同字段的稳定投影，排除租约重试字段和辅助计数/时间戳；定向 pytest 53 passed、40 skipped、0 xfailed，0.35 秒，make check 退出 0（锁/Ruff/mypy 通过，2653 passed、351 skipped、2 既有架构债 xfailed，45.04 秒），一个追加提交普通推送，不触发审查、不操作 thread、不合并。

- 用户修正（da9f04a 之后）：sample_jobs 比较收窄为 job_id/session_id/sequence 集合，允许同一任务释放租约或推迟，不把 due_at/state 变化误判为安排后续任务；已用源文件实际断言验证重试字段变化通过、任务新增/身份与序号变化拒绝。定向 pytest 53 passed、40 skipped、0 xfailed（0.35 秒）；make check 退出 0，锁/Ruff/mypy 通过，2653 passed、351 skipped、2 既有架构债 xfailed（45.97 秒）；普通追加提交，不触发 review、不操作 thread、不合并。

## 下一步与交接

检查和独立审查完成，提交本分支 PR，等待接线；不合并。解除 skip 时提供真实外部驱动；原断言只按合同或验收变更处理。
F6 第 6 步真实环境、持久化原子边界与角色隔离验证留在原 M1-02 任务范围。


## 接线历史（2026-10-08，main 280e550 改接）

以下各轮数字保留当时运行结果；2个任务身份 xfail 已在 #164 移除，当前状态与验证见文末。

- 状态：F6五步41个外部场景全部pass，无skip；纯领域42pass，持久授权2xfail，原夹具11pass，驱动检查30pass。F6仍未全部合同/真实生产验收完成。工作区`production-ops-agent-f6-wiring`、分支`test/f6-acceptance-wiring`；lead负责提交和停55651，本轮只改测试/文档，未改产品、迁移、profile、原M1-02任务、ROADMAP或passes。
- 产品入口：真实登记处置→受限Observer领取→产品PrometheusReadOnlySource（测试opener）查询→原子提交→incident_records→recovery_outcome(RecoveryRecords)。判定/原因/交接/健康窗/actions由产品给出；permissions从Observer自己的table_privileges实测，不拼装或过滤。target只取产品会话不可变绑定。映射表见[测试说明](../testing/f6-acceptance-tests.md)。
- 合同变更（用户2026-10-08）：四身份字段分别异身份再登记同一目标必须TARGET_MISMATCH拒绝，完整快照不变；现有会话正常采样只能落在原绑定目标，遥测元数据不能改目标。revision登记产生新会话/控制与观察版本，旧会话撤销，已领取旧结果只history_only且不推进新会话。原5个xfail与不存在的caller-target提交断言移除，逐条旧→新断言与理由见测试说明；#138已关闭。
- 重放：去掉第5步5个skip。新ObservationStore以incident_records单快照按主体/会话取回全部持久输入；产品replay_history和recovery_outcome给出结果，信号字节映射同样取捕获快照，不读原始outcome或外部遥测。重放前后完整业务快照相等；篡改判定得到unknown+完整性码及unverified（recorded_lifecycle仍为已存值）。模型/遥测边界计数均为0。
- #115五项：①新存储取回、②重放不改业务状态现均在5个真实产品重放场景实际生效；③query/绝对窗/类型集合独立核对，④采样完成后提交前撤销/到期，⑤240秒耗4次预算早于600秒期限均运行。④当时缺全部旧任务清单，保留2xfail；#164 已补表示并改为真实断言。
- 4条机器人线程处置：①删除evaluate_readings和合成判定/交接/动作，改产品投影；②permissions直接复制实际grants，私有f6_acceptance_*库撤销PUBLIC TEMP；③target复制产品会话绑定，原始payload仅作证据；④原跨目标5个xfail随批准合同改写消失，余2例任务身份xfail限定ContractInterfaceConflict、保留RuntimeError不被吞的回归。
- 权限/CI：仅夹具自己创建的库执行REVOKE TEMP FROM PUBLIC，非m0_budget或其它库；同一PG17 SQL适用于CI的m0-postgres服务，CI入口已包含验收/合同。新增PG检查临时授TEMP时产品与驱动如实报告database_temp、冻结权限断言失败，finally收回；无过滤。Prometheus原始刺激改为真实vector格式、分别返回value/coverage/freshness字节，数值/新鲜度/原始字节断言未放宽。
- 时间准备：合成窗尾/处置时间及owner SQL仅模拟调度。到期成对回拨deadline_at/created_at，维持冻结正duration，避免旧单独deadline修改造成新重放SESSION_PARAMETER_MISMATCH；由sweep_expired_sessions真正结束会话，夹具不写判定、生命周期、水位、计数。证明边界与离线一致性，不证明实际流逝/延迟/生产时钟；未kind、模型或真实遥测，费用0。
- 历史2xfail（#164 已解除）：revoke/expire在所有状态、授权、水位、预算、历史/证据、只读断言通过后，产品sample_jobs集合从空变为原已领取旧job。当时投影缺已结束且无样本任务身份（acceptance_recovery.py sample_jobs、revocation.py:53），不能推定安排了新任务。第1/2/3/4/5步分别22/6/6/2/5pass，0xfail/skip；合同42pass/2xfail。
- 独立审查：全新上下文Agent复核产品入口、TEMP、不可信payload目标、合同变更及时间准备，无新增合同/正确性缺口；改接阶段独立PG复跑105pass/2xfail（8.27秒）。静态审查与运行证据分别记录。
- 复验P2（137-recheck2-final.md）已处置：别名转换前核验顶层/样本/会话/授权主体UUID；revision必须存在且与对应持久会话绑定一致，内容SHA-256与持久值及规范化revision一致；未知版本不回退当前profile，顶层profile缺失也不得清空样本。新增10类投影/记录篡改检查直接AssertionError、无xfail；独立复验21个驱动检查通过，原缺失profile复现现在直接失败。当时测试说明raises统一为ContractInterfaceConflict（现 #164 删除标记及异常类）；m0-postgres作业增加驱动检查入口，确保新增PG guard不会在默认非PG检查中仅skip。
- 最后P1（PRRT_kwDOUSm_486qdOfc）：选择事故/控制快照按所选会话时间边界裁剪，保留之前会话以维持产品登记/action关联；截止所选结束记录时间（无结束则最后采样/授权审计时间），移除后续登记/控制，生命周期取结束后的历史值或授权观察阶段，历史mode未知则None。新增第一会话观察→第二会话resolved→重放第一会话检查，1次登记/2次持久化/36次查询、观察中生命周期，不能混入第二次登记与当前resolved。仍使用产品重放和投影，无业务状态写入。
- 最后P1（PRRT_kwDOUSm_486qdOfm）：动作验收仅收紧，必需登记/查询/持久化/生命周期转换与交接动作；登记次数核对审计、持久化次数核对提交样本数、查询次数核对测试桩按真实receipt.sample_id关联的实际发出次数。新增8种空/缺/截断audit拒绝检查，范围规则及数量在测试说明写明。
- 最后两P1全新上下文审查者独立复验124pass/2xfail（9.62秒），无未处理正确性缺口；当时两项任务身份投影限制保留，现已由 #164 解除。
- 本地PG：`M1_DURABLE_POSTGRES=1 OPSPILOT_LAB_DSN="host=127.0.0.1 port=55651 dbname=m0_budget user=m0_lab" OPSPILOT_PG_DUMP=/opt/homebrew/opt/postgresql@17/bin/pg_dump .venv/bin/python -m pytest tests/acceptance/test_f6_recovery.py tests/contracts/test_f6_observation.py tests/test_f6_driver_guards.py -q --tb=short` → **124 passed、2 xfailed、0 skipped**（9.66秒、退出0），日志`tmp/f6-final-p1-pg.txt`。
- `UV_CACHE_DIR=tmp/uv-cache make check` → **3192 passed、492 skipped、2个既有架构债xfailed**（72.18秒、退出0），锁/Ruff/mypy全过，日志`tmp/f6-final-p1-check.txt`；默认非PG。另Ruff与git diff --check通过。无提交/push/PR，未initdb/停PG/操作其它端口。

- #145 收紧（PR #155，2026-10-08）：驱动逐样本绑定持久会话，生命周期/交接动作精确计数；测试作者 Codex，独立审查另一 Codex 会话。该轮PG套件134 passed、3 xfailed；当前口径与复验见下条 #154。
- #154 已按用户2026-10-08裁定落地：`actions` 为完整审计，#145第2条仅约束最新会话/当前范围不新增交接；历史交接必须保留。所有结果的 human_handoff 次数精确等于同一持久范围内各会话 deadline_expired / max_samples_exhausted 结束记录数；交接结果还须至少1，单会话非交接仍为0。先交接后恢复直接检查恰为1，并拒绝缺失/多余历史交接，交接结果也拒绝重复动作。驱动仅从同一持久快照提取结束原因作为独立见证，不从 actions 或最新判定倒推。
- 本轮工作区 `production-ops-agent-f6-154`、分支 `test/f6-154-handoff-scope`、main起点 `1004a86`。只改测试/指定文档，未改产品、ROADMAP、passes，未提交；未操作其它端口。临时PG由调用者管理，不停库。
- 红：移除连续场景的跳过逻辑、增加交接结果重复动作检查后，PG定向 **2 failed、7 passed、33 deselected**（1.86秒）：旧断言要求历史交接1等于0，且不能拒绝重复交接。随后按持久结束记录计数修改断言，未放宽其它断言。
- 绿：端口55633指定三文件PG命令 **136 passed、2 xfailed、0 skipped、0 failed**（11.22秒，退出0）；41外部场景、42领域合同、11原夹具、42驱动检查通过。该轮2个持久任务身份缺口为xfail（现 #164 已解除）；本轮无产品投影失败。完整命令与合同说明见[测试说明](../testing/f6-acceptance-tests.md)。
- `UV_CACHE_DIR=tmp/uv-cache make check` 最终退出0：锁检查、Ruff lint/format、mypy（75源文件）通过；pytest **3386 passed、512 skipped、2个既有架构债xfailed、0 failed**（88.92秒）。首轮因新增测试格式不符退出2，定向格式化后重跑通过。默认未启用PG，不能代替上条PG证据；`git diff --check`通过。
- 全新上下文独立审查完成：未发现正确性/合同缺口，独立抽取 `assert_readonly` 的17个合成计数案例通过，3个测试文件语法解析通过；文档旧口径已复验消除。审查者未操作PG，此项不替代作者PG运行。

## #164 已结束任务身份（2026-10-08）

- 工作区 `production-ops-agent-f6-jobs`，分支 `feature/F6-ended-job-identity`，起点 `3c7f044`；只改测试与本次指定文档，不提交、不改产品/迁移/ROADMAP/passes，不操作其它端口或停库。
- 移除 revoke/expire 两个 strict xfail、捕获/转换与无其它使用处的 `ContractInterfaceConflict`。提交前快照必须恰含 lease 独立见证的旧 job，session_id 等于原会话、sequence 等于测试刺激；history_only 入库后任务集合不增不减。其它断言保留。另加10个投影篡改负向检查，覆盖缺旧任务、多任务及错误 job/session/sequence，前后同错也必须拒绝。
- 红/基线按实记录：仅去 xfail、不改断言时 **2 passed、42 deselected**（0.64秒，退出0），当前产品已补表示，未出现断言红。临时恢复原 strict/raises 标记核对旧行为，得到 **2 failed、42 deselected**（0.62秒，退出1），均为 `XPASS(strict)`；随后移除标记并收紧。这是旧标记失效证据，不是产品缺陷复现。
- 绿：下列指定 PG 套件 **149 passed、0 xfailed、0 skipped、0 failed**（12.51秒，退出0）：42外部场景、42领域合同、2持久撤销/到期合同、11原夹具、52驱动检查（含新增10例）。收紧后、增加负向检查前的同套件为139 passed（11.63秒）。

```sh
M1_DURABLE_POSTGRES=1 OPSPILOT_LAB_DSN="host=127.0.0.1 port=55671 dbname=m0_budget user=m0_lab" OPSPILOT_PG_DUMP=/opt/homebrew/opt/postgresql@17/bin/pg_dump .venv/bin/python -m pytest tests/acceptance/test_f6_recovery.py tests/contracts/test_f6_observation.py tests/test_f6_driver_guards.py -q --tb=short
```

- `UV_CACHE_DIR=tmp/uv-cache make check` 退出0：锁检查、Ruff lint/format、mypy（77源文件）通过；pytest **3389 passed、524 skipped、2既有架构债xfailed、0 failed**（88.76秒）。默认非PG，新增10例在此skip，不能替代上面的PG证据。检查日志 `/private/tmp/f6-164-check.log`；`git diff --check` 通过。
- 旧说明核查：允许范围内的当前口径已更正；历史运行数字保持原值并标明已被本轮取代。范围外仍有 `ROADMAP.md` 的2例接口冲突/验收阻塞口径、`docs/tasks/2026-10-03-m1-02-recovery-observation.md` 第177行当前状态与第186行历史记录/第312行待作者改标记描述，以及 `docs/evidence/m1-02-recovery-outcome/run.md` 第26行旧投影来源表。依本轮只改指定路径限制未改这些文件，交由 lead 收口。
- 全新上下文独立 Agent 静态审查与最终文档复核完成，无未处置合同/正确性缺口；确认 witness 来自 fault 前的 held lease。审查者未运行PG，运行数字来自作者实际命令。
