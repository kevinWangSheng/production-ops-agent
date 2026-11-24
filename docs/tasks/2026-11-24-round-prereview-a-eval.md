# 合同预审 A：#830（SREGym p12 Valkey 管理认证）与 #780（常驻操作员说明通道）

- 日期：2026-11-24（PT）；main `dda03323`；预审者只读，未改仓库、未提交。
- 读过：AGENTS.md、PRODUCT-CONSTRAINTS.md、`docs/tasks/README.md:21-40`（合同预审）、两个 issue 全文与评论、`docs/tasks/2026-11-22-next-batches.md`（B4-U3/U4/U6、R2-3）、`docs/tasks/2026-11-22-next-batches-b4-prereview.md`（U3、U4、U6、B4-9~12）、`docs/tasks/2026-11-22-redis-tool-773.md`、`docs/tasks/2026-11-19-sregym-667.md`（R8-6）、证据 `docs/evidence/rental-2026-11-24/swing/run.md`、`docs/evidence/rental-2026-11-23/smoke/run.md`、p12 trial-1 `summary.json`（只看产品报告字段）；代码见各行引用。上游：SREGym `tmp/upstream/sregym-ac60c64`（与 `scripts/sregym_eval/upstream.py:20` 钉的 `ac60c642` 同一提交）的 conductor/hooks/agent_registry/main.py；HolmesGPT `tmp/upstream/holmesgpt-a0009195` 的 `global_instructions.py`、`base_user_prompt.jinja2`。
- 答案隔离：没打开 SREGym `problems/` 下任何文件、fault-spec、oracle、ground truth；没读 Holmes 162 的 `global_instructions.json` 内容与 judge 理由。（公开仓库版：已删去对题目故障机理的推断，完整版只在仓库外。）
- 全量探针：**未执行**。lead 指令本机 swap 将满、禁跑全量 pytest/`make check`；`tests/acceptance` 的探针留给实现者在开工前跑，结果补进决定表。

---

## #830 SREGym p12：harness 怎样以管理员身份建只读 Valkey 账号

### 现状（已核实）

- 入口在 p12（按 seq + 题目摘要判定，`agent_entry.py:641-645`）以 Valkey `default` 用户、**不发 AUTH** 执行 `ACL SETUSER opspilot_ro reset on >pw ...`，再 `ACL GETUSER` 自查（`agent_entry.py:150-167`、`:682-726`）；失败记 `valkey_account.code` 与回复原文，披露 `VALKEY_READONLY_ACCOUNT_FAILED`，本遍继续（`:712-726`）。口令每遍新生成、只在内存（`:688`），进 worker 环境 `OPSPILOT_REDIS_USERNAME/PASSWORD`（`:389-391`）。
- 11-24 三遍都是 `ERR Protocol error: unauthenticated multibulk length`（`docs/evidence/rental-2026-11-24/swing/run.md:19`）：被测 Valkey 在入口启动时已要求认证。
- 入口由 SREGym 在**故障注入之后**启动（`agent_entry.py:3-4`、`agent_registry.py:5-6`）。
- 注入后再取管理口令，有接触题目答案的风险（公开仓库版不展开）；注入前建账号可以完全避开这个问题。
- SREGym 钉住的提交支持注册级钩子：`agents.yaml` 条目可写 `hooks: module:attribute`（`sregym/agent_registry.py:44-45,67`），`before_fault_injection(ctx)` 在注入前一刻于 conductor 进程内运行，`ctx` 含 `problem_id`、`namespace`（`sregym/conductor/hooks.py:1-28`、`conductor.py:442-466,481-485`）；钩子抛异常会让本次尝试失败且不重部署（`hooks.py:12-14`、`conductor.py:459-464`）。我们的注册文件目前没写 `hooks`（`scripts/sregym_eval/agent_registry.py:21-33`）。
- 先例：Holmes 评测建 SQL/Mongo 只读账号是 `kubectl exec -i deployment/<x>` 跑固定脚本，管理权限来自容器自己的环境，只读口令走 stdin（`scripts/holmes_evals/db_backends.py:130-136,218-224`）；ES 用操作员提供的管理口令（`driver.py:176-180,2153-2166`）。SREGym 其他源没有认证（Kafka PLAINTEXT，`agent_entry.py:106-108`），无先例。
- main.py 进程环境含 engineer `KUBECONFIG` 与 job 路径（`scripts/sregym_eval/driver.py:276-291`），钩子可用。坑：SREGym 仓库顶层有 `scripts/__init__.py`，与我们的 `scripts` 包同名，钩子不能直接写 `scripts.sregym_eval.x:y`。

### 需用户裁决

| 编号 | 问题（行为） | 推荐 | 代价一句 | 不在意时的兜底 |
|---|---|---|---|---|
| 830-U1 | **harness 什么时候、用什么身份建只读账号？** a. **注入前建**：给 opspilot 注册加 SREGym 自带的 `before_fault_injection` 钩子，故障注入前 Valkey 还没口令，以 `default` 免认证建账号；注入后入口用只读账号自查，产品照常用它。b. **注入后从容器里拿管理口令**（照 Holmes 数据库先例 exec 进容器读环境/配置）：有接触题目答案的风险。c. **不接**：p12 保持披露失败、0/3，关掉 #830。 | **a** | a 让评测脚本在故障前写一次被测系统（只加一个账号，B4-U6 已允许），故障若重置 ACL 或重启 Valkey，账号会没【未确认，按答案隔离不查】，那就落回今天的失败披露；c 零成本但这题永远 0/3 | a |
| 830-U2 | **评测在注入前多做的这一步，结果页怎么说？** 推荐：沿用披露码 `VALKEY_READONLY_ACCOUNT_BY_HARNESS`，在入口记录里加一个字段写明「注入前建」；不新增披露码。另一选项：新增 `VALKEY_ACCOUNT_BEFORE_FAULT` 披露码 | 沿用旧码加字段 | 新码多改驱动合同测试与结果表口径；旧码已表达「评测脚本动了被测系统」 | 沿用旧码 |

（不重提：只在 p12 建账号、产品不加匿名访问——B4-U6，`next-batches.md:47`。）

### Agent 自定（可逆默认）

| 编号 | 问题 | 默认 | 理由 |
|---|---|---|---|
| 830-1 | 钩子代码形态 | 一个只用标准库、顶层名唯一的小文件（如 `opspilot_sregym_hooks.py`，驱动拷到 round 目录并把该目录放进 main.py 的 `PYTHONPATH`），钩子里 `subprocess.run([<我们的 venv python>, "-m", "scripts.sregym_eval.valkey_hook", ...], cwd=repo, timeout=30)` | 避开 SREGym 顶层 `scripts` 包同名冲突与依赖差异 |
| 830-2 | 钩子何时动手 | 只在 `problem_id` 摘要 = p12 摘要时动手（同 `_is_valkey_problem` 规则），其余题立即返回 | 与 #809 已定的「只在 p12」一致；`problem_id` 只在评测侧进程，不进产品可见字段 |
| 830-3 | 钩子失败 | 永不抛异常；失败码与回复原文（口令打码，同 `valkey_reply_text`）写进 round 目录的结果文件，尝试继续 | 抛异常会让 SREGym 整次尝试失败（`hooks.py:12-14`），违背 #809「记录后继续」 |
| 830-4 | 钩子怎么连 Valkey | `kubectl exec -i -n <ns> deploy/valkey-cart -- valkey-cli`，命令走 stdin（口令不进 argv）；超时 10 s | 照 Holmes C6-21 先例；不需要在注入前另起 port-forward【未确认：镜像内有 `valkey-cli`，官方镜像通常带】 |
| 830-5 | 口令谁生成、怎么传给入口 | 驱动每遍生成口令，写进该遍 round 目录 0600 文件（同 `entry-secrets.env` 做法，`driver.py:437`），钩子与入口都读；本遍结束删除；入口仍只把它放进 worker 环境 | 钩子在入口之前、另一个进程运行，需要共享；去向与今天相同（只到 worker 环境），不进记录、提示词、trace |
| 830-6 | 入口的自查 | 注入后改为用只读账号自己 `AUTH` + `ACL GETUSER opspilot_ro`，走工具自带的 `account_problem`（`redis_inspect.py:423-428`）；不再以 `default` 发命令 | 注入后 `default` 已要口令；自查口径与产品工具一致 |
| 830-7 | 钩子结果没出现（钩子没跑、文件缺失） | 入口记 `HOOK_RESULT_MISSING`，按失败披露，本遍继续 | 失败可见、不静默 |

### 必答类别

1 时间：钩子在注入前一刻、propagation 等待之前（`conductor.py:481-485`）；钩子自限 30 s。2 计数：p12 仍 3 遍计分，失败照记。3 状态：账号只在 Valkey 内存，故障或重启可能清掉【未确认】。4 主体与权限：钩子用 engineer kubeconfig（已有、admin 背书，`job.py:3`）；产品身份不变。5 数据合同：只改评测脚本与入口记录字段，无迁移。6 旧证据：11-23/11-24 的失败记录不重算。7 并发：每遍一个账号，同名 `reset` 覆盖。8 外部调用：一次 kubectl exec（≤10 s），无新增模型调用。9 验收：无 feature_list 条目，`passes` 不动；驱动合同测试改为钩子+入口两段。

### 建议

- **做（小）**：评测脚本 PR，属预授权脚本类（`next-batches-b4-prereview.md:87` 先例）；约半天，合同测试用假 conductor 与本机 Valkey 容器模拟「注入前无口令 → 注入后设口令」。
- **真实 Run**：需要。必须在租机 SREGym 集群跑 p12 ×3 才能证实账号能活过故障；不为这一项单独租机，搭下次租机。
- PR 拆分：1 个 `chore/830-valkey-pre-fault-account`。

---

## #780 经审核的、按目标的常驻操作员说明进模型

### 已裁决（不重提，引出处）

- **B4-U3 = a**：复用 runbook 的提交/批准/驳回/撤销流程，新增「每次都提供」类别（`next-batches.md:44`；方案正文 `next-batches-b4-prereview.md:18`）。
- **B4-U4 = a**：与 runbook 同类——审核过的清单，不是证据，不授予权限，不改目标、时间范围与报告要求；PRODUCT-CONSTRAINTS 不改，C3 §10 加一句，F7 注入恶意指令那一步扩展覆盖此通道（`next-batches.md:45`）。
- **R2-3 已成立**：11-23 冒烟 162 题不带说明 B、拼上 5 条上游说明后 A，诊断相同，差别只在说明要求的那部分；各 1 遍，弱证据（`docs/evidence/rental-2026-11-23/smoke/run.md:5-14`）。按 R2-3「成立进下一轮」。
- 可逆默认已记录：B4-9（每目标最多一份生效、1–4096 字节纯文本、进模型前过凭据脱敏、单独一条 user 消息）、B4-10（沿用 `OPSPILOT_KNOWLEDGE_TOOL=1`；没有已批准说明时模型输入逐字节不变、提示词版本不变）、B4-11（`tests/safety` 两条 + 变异）、B4-12（驱动经工作台接口导入并批准、披露码改为 `GLOBAL_INSTRUCTIONS_IMPORTED_REVIEWED`）（`next-batches-b4-prereview.md:51-54`）。

### 本次核对发现的事实与需更正处

- 迁移号 `0029` 仍空（最新 `opspilot/migrations/versions/0028_runbook_imports.py`）；Run 输入 `v9` 仍空（`context.py:203-221` 只到 v8）。
- runbook 表：每条目只有 `resource_uid`，没有类别列（`0028_runbook_imports.py:166-173`）；审计 `object_kind` 有 CHECK 只许两种（`:144,158-161`）——加类别不需要改审计。自批准未被禁止（导入脚本用同一账号提交并批准，`import_skills.py:6-9`）。
- 消息顺序：system → 问题 → 告警上下文 → 发布上下文 → 复盘知识 → runbook 清单 → evidence_context（`context.py:872-895`）。上游把全局说明放在 Skill Catalog 之后、优先级第 3（`base_user_prompt.jinja2:1-30`），格式 `* 条目`（`global_instructions.py:15-22`）。B4-9 定的是「runbook 清单之前」，与上游相反；两者都可逆，见 780-3。
- **提示词版本**：现在 runbook 清单的边界文字只在「开关打开」时计入 `prompt_revision`（`loop.py:155-157,532-536`），按 fact 是否在输入里判定（`loop.py:618`）。要满足 B4-10「没有说明时版本不变」，新边界文字必须按「本 Run 输入里有说明」计入，不能按开关计入——否则所有开着知识开关的 Run（含 Holmes 评测全部题）版本都会移动。见 780-4。
- **F7 步号更正**：B4-U4 写的「F7 第 4 步」在 `feature_list.json` 里是「全局/服务级暂停」；「往日志和 runbook 注入恶意指令」是 **F7 第 2 步**（F7 `passes: true`）。按裁决本意落到第 2 步，属收紧；任务记录里改步号即可，不改裁决。
- 继任 Run（追问、重开）每次重读 runbook 清单、不沿用（`web/service.py:2148-2154`）；创建 Run 时 web 用自己的只读快照读（`service.py:2003-2011`）。
- 现有安全测试已覆盖 runbook 清单描述与 fetch 正文两个通道（`tests/safety/test_safety_model_inputs_postgres.py:222-286`），变异在 `tests/mutation/mutations.py:330-335`；新通道照此加。

### 需用户裁决

| 编号 | 问题（行为） | 推荐 | 代价一句 | 不在意时的兜底 |
|---|---|---|---|---|
| 780-U1 | **功能 PR 审查干净、真实 Run 证据齐后，Agent 能不能自己合并？** a. 能：独立审查无阻塞项、CI 绿、`CLEAN`、带一次有界真实 Run 的 trace（看到说明消息、报告逐条回应）后 Agent squash 合并，再给你简报。b. 不能：这是新模型输入通道 + 数据流变更，等你合并。 | **a** | 方向和语义都已在 B4-U3/U4 裁定，剩下是实现正确性，由独立审查与安全测试兜；b 多一轮等待 | a |

除此之外没有新的用户门：存储、审核主体、信任口径、开关、上限、驱动导入都已裁决（见上）。

### Agent 自定（可逆默认）

| 编号 | 问题 | 默认 | 理由 |
|---|---|---|---|
| 780-1 | 存储 | 迁移 0029 给 `opspilot_runbook_submissions`/`revisions` 加 `kind text NOT NULL DEFAULT 'runbook' CHECK (kind IN ('runbook','standing_instructions'))`；旧行全是 `runbook` | 照上游同表存放；默认值让旧行与旧读取不变 |
| 780-2 | 「每目标最多一份」怎么保证 | 常驻说明的名称固定为 `standing-instructions`，新提交按「取代」走现有 supersede 链；服务层拒绝同目标第二个条目（码 `STANDING_INSTRUCTIONS_EXISTS`） | 复用现有取代/撤销语义，不新造状态 |
| 780-3 | 消息位置 | 按 B4-9 放在 runbook 清单之前；不改 | 已记录的默认；上游在之后但无行为依据要求照抄，改动只是顺序 |
| 780-4 | 提示词版本口径 | 新边界文字只在「输入里有 `standing_instructions` fact」时计入 `prompt_revision`（新增布尔参数，与 `runbook_catalog` 分开）；没有说明的 Run 版本逐字节不变 | 满足 B4-10；不动 Holmes 评测其他题与基础档位 `otel-demo` 的钉值 |
| 780-5 | 输入版本 | `opspilot-investigation-input-v9`，fact 名 `standing_instructions`，含 `entry_id`、`revision`、`approved_at`、`approved_by`、正文（已脱敏）；v1–v8 照旧可读 | 与 v7/v8 同模式（`context.py:175-221`）；审计链能从 Run 追到批准人 |
| 780-6 | 正文格式 | 纯文本原样（脱敏后），不解析成列表；驱动导入时按上游格式写成 `* 条目` 每行一条 | 产品不必理解结构；上游格式就是这样拼的 |
| 780-7 | Chat 与继任 Run | 与 runbook 清单同规则：每个新 Run（含追问、重开、Chat 产生的 Run）创建时重读，不沿用旧 Run 的版本 | `service.py:2148-2154` 先例 |
| 780-8 | 报告校验 | 不加确定性校验（不检查报告是否逐条回应），只靠固定说明文字 | 上游也没有；不碰报告校验就不用重跑 F3 证据型验收 |
| 780-9 | 工作台 | runbook 提交页加类别单选；列表显示类别；常驻说明在批准页显示全文 | B4-U3 a 已写；照 runbook 页最小改动 |
| 780-10 | 驱动导入 | `import_skills.py` 新增读 `global_instructions.json`（只在 162 题目录），走同一提交+批准接口、题后撤销；文件头「从不打开该文件」一句同步改；披露码换 `GLOBAL_INSTRUCTIONS_IMPORTED_REVIEWED`，合同测试 `test_holmes_evals_lookback_809_contract.py:186-187`、`test_holmes_evals_d_trial_contract.py:559` 相应改 | B4-12 已定；这些是钉披露码的测试，按合同变更改，不是削弱 |
| 780-11 | 安全测试 | `test_safety_model_inputs_postgres.py` 加：带凭据形状的说明进模型前被脱敏；说明里写「改查别的目标／扩大时间范围」不产生越权查询（F7 第 2 步同款）。各配变异：去掉脱敏、去掉边界文字 | B4-11；项目规则「新模型输入通道必须进 tests/safety 加变异」 |

### 必答类别

1 时间：Run 创建时读快照，之后撤销不影响已开始的 Run（`catalog.py:1-9` 同规则）。2 计数：每目标 ≤1 份、1–4096 字节；不加新的累计上限。3 状态：提交→批准/驳回→撤销，与 runbook 同（C3 `technical-proposal-2026-09-07.md:402`）；worker 与模型不能触发审核。4 主体：登录用户提交与批准，可同一人（现状）。5 数据合同：迁移 0029 → 再同时升 web 与 worker；输入 v9。数据流：说明正文是操作员写的文本，去向与 runbook 正文相同（证据存储、所选模型、trace），不新增接收方。6 旧数据：v1–v8 照读；没有说明的 Run 字节与版本不变；已归档证据不重算。7 并发：无新增。8 外部调用：无新增模型调用，每 Run 多一条 ≤4 KiB 消息。9 验收：F7 第 2 步覆盖面扩大（收紧），F7 `passes` 保持 true 的前提是新增安全测试通过；F3 不动。

### 建议

- **做**：原因已在 R2-3 证实，162 是 Holmes 题集同组唯一失分题；方向与语义都已裁决，照搬上游，约 2–3 个工作日（B4 估计）。
- PR 拆分：1 个功能 PR `feature/780-standing-instructions`（迁移、输入 v9、边界文字、工作台类别、安全测试与变异、C3 §10 一句）；评测驱动导入可并入同一 PR，也可另开 `chore/780-import-global-instructions` 在功能 PR 合并后合（无 stacked PR）。验收与合同测试由未参与实现的全新 Opus 写。
- **真实 Run**：PR 本身要一次有界真实 Run 的 trace（触碰调查输入）——可在本机 kind lab 对任一目标批准一份说明跑一次，不必租机（注意本机 swap，观察窗内停重负载）；162 的分数复核搭下次租机全量。
