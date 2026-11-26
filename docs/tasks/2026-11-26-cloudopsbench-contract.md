# Cloud-OpsBench 接入 OpsPilot：合同决定表（r1）

裁决（2026-11-26，用户）：U1–U8 全部按推荐；U9（简历口径）推迟，本任务不涉及。A1–A9 按可逆默认执行。

预审者：只读，未参与设计。COB 指基准仓库 clone（HEAD ec1d4f3），路径前缀 `COB/` = 基准仓库 `LLM4Ops/Cloud-OpsBench` 根目录。未跑全量探针：本步骤只加评测侧脚本，不改产品文件；若裁决 U1 选 b/c 或 U4 选 b，实施前必须补跑 `pytest tests/acceptance -q` 探针。

## 关键事实（已核对）

- 题面只有一句症状：754 题只有 6 种 query（例如 "Service Availability Disruption." 301 题），外加 namespace（COB 各 `metadata.json` 统计）。难度分布 easy 391 / medium 189 / hard 174；故障对象 app 659 / node 48 / namespace 47。
- 评分只看第 1 名，`fault_object` 和 `root_cause` 归一化后与标准答案精确比对（`COB/agents/cloudops_agent/evaluation.py:380-401`）。没有答案文件按 0 分计（同文件 :428-440）。标准答案来自 `process-label/.../milestone.json`（:224, :235）。
- 自带 Agent 的提示里给了 57 个根因标签的封闭清单和合法资源名（`runtime/contracts.py:1-140` 与 `build_expected_output`），默认 `max_iterations: 20`、`temperature: 0`、`max_tokens: 4096`（`configs/model_configs.yaml`，`runtime/core.py:44`）。MCP 的连接示例只给 query 和 namespace，不给清单（`cloudopsbench_mcp/README.md`「Connect an agent」）。
- 缓存未命中时工具会返回合成的 `Error from server (NotFound)`（`tools/snapshot.py:568-569, 648-649, 755-756`）。也就是说，参数没写对会看到“对象不存在”，这不代表环境里真的没有。
- OpsPilot 报告的 `root_cause` 是一个对象 `{kind, namespace, name, evidence_ids}`，没有故障类型字段（`opspilot/investigation/reports.py:101-113, 158-173`）。所以 57 个标签里的哪一个，报告结构本身给不出来。
- OpsPilot 唯一的防循环上限是每 Run 100 次模型请求（`opspilot/investigation/limits.py:25`）。`opspilot/` 里没有任何 mcp 引用（grep 为空）。
- 已发布的 DeepSeek-V4-Flash 成绩：Boutique CA .88 / FA .77 / JRA .76，TrainTicket CA .69 / FA .71 / JRA .63（`COB/README.md:282,297`）。是全集还是某个切分上的成绩：**未确认**（评测代码支持 train/test split，`evaluation.py:163-200`，但仓库里没找到 split 文件）。

## 需用户裁决（用户门）

| 编号 | 问题（行为） | 选项与代价 | 推荐与理由 | 类别 |
|---|---|---|---|---|
| U1 | OpsPilot 通过什么拿到基准的 12/10 个工具？ | a) 评测专用的只读工具族：在评测脚本里注册，把同名工具直接接到快照数据（读 `tool_cache.json`、`raw_data/`，复用 COB 的缓存键规则或调本机 MCP 服务）；不进产品镜像。代价是要写一份工具描述和参数 schema 的适配。b) 给产品加 MCP 客户端：通用性最好，但这是新的产品能力和新的数据入口，要过 PRODUCT-CONSTRAINTS 数据流合同和 tests/safety，工作量最大。c) 伪造 K8s 后端，让 OpsPilot 的真实 kubernetes 工具去查：快照只有 kubectl 文本，没有结构化对象，得把文本反解析成对象，容易失真。 | **a**。快照只有文本，c 不成立（任务事实）。b 扩了产品能力，只为跑一次评测不值。a 和 SREGym 的先例一致：钩子和题面都放在评测侧（`scripts/sregym_eval/opspilot_sregym_hooks.py:1-19`，`question.py:1-12`）。工具仍走产品的 `ReadOnlyToolExecutor` 和账本（`opspilot/tools/fixture.py:1-25` 是同类 profile）。按 PRODUCT-CONSTRAINTS「Data flow contract」，基于公开 fixture 在租机上搭的环境算 synthetic lab，不需要另批数据源，但任务记录要列明每个工具返回什么。快照文本是不可信输入，需要过名字上下文脱敏。是否要在 tests/safety 加带变异的测试，看 a 是否新开了模型输入通道（记忆规则：新通道必须覆盖）。 | 用户门（产品能力边界） |
| U2 | 57 个根因标签清单和合法资源名要不要给 OpsPilot？从哪里给？ | a) 给，原样放进评测侧题面（incident 文本），和自带 Agent 看到的一样。这是公平对比，但题面变长，接近“选择题”。b) 不给，只给 query 和 namespace（MCP 示例口径），事后再映射到标签。更接近真实运维，但 FA/JRA 会被映射环节拖低，和发布成绩不可比。c) 两种都跑。比较最完整，费用和时长翻倍。 | **a**。对比对象（发布成绩和同模型对照）都看了清单（`contracts.py:136-140` List A/B）。不给清单得出的低分，会被误读成 OpsPilot 比自带 Agent 差。题面上限 8192 字符（`scripts/sregym_eval/question.py`，`QUESTION_MAX_CHARS`）：清单约 5–6 KB，TrainTicket 加 42 个服务名需实测是否放得下，放不下就压缩说明文字，不删标签。题面模板要固定并钉 sha256（SREGym 先例）。 | 用户门（公平对比口径） |
| U3 | OpsPilot 的报告怎样转成 `{fault_object, root_cause}`？ | a) 确定性转换：`root_cause.name` 映射到 `app/<svc>`/`node/<n>`/`namespace/<ns>`；故障类型取报告 summary 或 claims 里逐字出现的第一个合法标签，没有就记空并计 0。不调模型，可复核；模型没逐字写标签就丢分。b) 评测侧加一个 LLM 映射步骤，把自由文本归到 57 类。分数高一些，但映射模型在“替答题”，不纯。c) 给产品报告加一个可选的 `fault_type` 字段。最干净，但这是报告合同变更（m0-report-v4），要走功能 PR 和用户门。 | **a**，并在题面里要求“在 summary 里逐字写出所选标签和 `kind/name`”。理由：ITBench 先例就是确定性转换，不调模型（`scripts/itbench_lite/answer.py:1-50`）；结果里按名单内/名单外、找到/没找到分别计数。不选 b：LLM judge 不能替代确定性断言（AGENTS.md「验证与汇报」）。规则需要细化：资源名匹配 Pod/Deployment 名时，去掉哈希后缀，按 List B 最长前缀匹配；kind 为 Node 时映射为 node。 | 用户门（得分定义） |
| U4 | 步数和模型参数是否向自带 Agent 看齐？ | a) 保持产品默认（100 次模型请求，产品的温度和输出上限），结果里写明差异。b) 评测时临时降到约 20 次，对齐自带 Agent。c) 两档都跑。 | **a**。100 是用户已裁决的唯一防循环上限（`limits.py:20-24`）；评测侧改上限意味着测的不是产品。自带 Agent 的 20 是“迭代”，和 OpsPilot 的“模型请求”口径不同，汇报时并列写出步数。如果用户想要严格同条件，选 c，费用约 ×2。 | 用户门（公平对比） |
| U5 | 子集怎么抽？ | a) 按（系统, 故障大类, 根因标签）分层：58 个层，每层至少 1 题，再按比例补到约 100 题，固定种子，跑任何一题之前冻结名单。b) 按（系统, 大类）分层（12 层），少数标签可能抽不到。c) 跑全量 754 题。 | **a**。58 层里有 47 层不超过 11 题（统计：层大小分布 3–47），按比例抽会漏掉小标签。抽题规则只看 metadata 里的系统、大类、标签，不看结果。名单和种子在跑之前提交进任务记录，题号可以公开（只是 `boutique/admission/1` 这类编号）。结果要同时报子集的未加权均值，以及按全集层占比加权的估计值。 | 用户门（得分定义） |
| U6 | 同模型对照怎么跑？ | a) 在同一台租机上、同一子集，用自带 ReAct（20 次迭代、温度 0）配 OpsPilot 的 `deepseek-flash` 跑一遍，用 COB 自己的 `evaluation.py` 打分。b) 只引用发布成绩（V4-Flash，模型不同）。c) a 每题跑 2 次，看波动。 | **a**。发布成绩用的是 V4-Flash，OpsPilot 用 V4.1 Flash（`docs/tasks/2026-09-15-model-profile-v41.md`），不能直接比。对照用 COB 自己的代码跑：它不可信，只在租机上的独立 venv 里运行，不碰项目解释器；它自己会读 `metadata.json`（`run.py:96-98`），这是基准自身的行为，不影响 OpsPilot 侧的隔离。DeepSeek 温度 0 也不完全确定，单次跑的波动在结果里注明。 | 用户门（公平对比 / 费用） |
| U7 | 冒烟测试怎样算通过？ | 一题 Boutique、一题 TrainTicket（最好有一题来自 codedefect，能覆盖源码工具），OpsPilot 和对照各跑一遍。 | 通过条件：① Run 正常结束，产出合法报告；② 转换后的 JSON 能被 `evaluation.py` 读入（`extract_final_answer_payload` 不返回 unparsed）；③ 工具调用里缓存命中率 > 0，且不全是合成 NotFound；④ OpsPilot 侧的模型输入里 grep 不到 `metadata`、`fault_object` 真值、milestone 内容；⑤ LangSmith trace 可访问。答对与否不算条件。冒烟结束后先看反馈，再决定要不要改适配（记忆「真实评测先冒烟再全量」）。改提示词属于产品改动，另走 PR。 | 用户门（验收口径） |
| U8 | 租机规格和时长 | a) 沿用 8 vCPU / 31 GB / 150 GB（`docs/evidence/rental-2026-11-23/run.md:10`）。b) 4 vCPU / 16 GB：没有 kind 集群，主要负载是 PG 和 3.6 GB 快照，可能够用。 | **b 起步，不够再换 a**。这次只需要 PG、OpsPilot worker 和每题一个工具进程；`benchmark/` 3.6 GB（`du`）。时长：约 100 题 × 2 条线，并发数和单题耗时**未确认**，冒烟后再估。跑完由用户在控制台 Destroy。 | 用户门（费用 / 主机） |
| U9（推迟） | 简历上怎么写结果？ | 照实写：“Cloud-OpsBench 分层子集 N=100（全集 754），OpsPilot（DeepSeek V4.1 Flash）CA/FA/JRA = x；同模型同子集自带 ReAct = y；论文发布 V4-Flash 全集 = z（模型不同，仅供参考）”。 | 推荐此写法。必须写出三点：是子集不是全量、模型版本不同、步数上限不同（U4）。过程指标不写进简历（见 A4）。 | 用户门（对外宣称） |

## 可逆默认（Agent 自定，各附一行理由）

| 编号 | 问题 | 默认 | 理由 |
|---|---|---|---|
| A1 | 工具适配代码放哪里 | `scripts/cloudopsbench_eval/`，不进 `opspilot/` 和产品镜像 | 与 `scripts/itbench_lite/`、`scripts/sregym_eval/` 结构一致。 |
| A2 | 防止答案泄漏 | 租机上为每题建一个只含 `tool_cache.json`、`raw_data/`、`code/` 的快照视图；`metadata.json`、`process-label/`、`golden-trajectory/` 放在 OpsPilot 进程不可读的目录；题面只从 metadata 取 query 和 namespace，由驱动程序填入 | COB README「Benchmark data boundary」就是这样要求的。SREGym 也有不让题号进题面的先例（`question.py:11-12`）。 |
| A3 | 缓存未命中的 NotFound | 原样返回，但在工具描述里写明“未缓存的查询会显示 NotFound”；未命中率进 summary | 不改基准语义，让读结果的人看到失真来源。这和产品“空结果不等于不存在”的报告规则一致（`reports.py:95-99`）。 |
| A4 | 过程指标 MC/EOC/ECR/EE | 只报 OpsPilot 的工具调用次数和缓存命中率。只有当工具名、参数和 COB 完全一致（A1 采用同名同参）时，才把 OpsPilot 轨迹转成 COB 格式试算过程指标，并标为“近似” | 过程指标依赖 COB 的轨迹格式和工具名（`evaluation.py:265-378`），换了格式就不可比。 |
| A5 | 打分 | 把转换结果写成 COB 格式的 case JSON，用 COB 的 `evaluation.py`（`python3 -I`，独立 venv）打分，不另写打分器 | 两条线用同一个打分器，口径一致。 |
| A6 | 入库与不入库 | 入库：每轮冻结一份 `summary.json`（题号、转换方法、CA/FA/JRA、步数、命中率、费用），子集名单，题面模板及其 sha256，LangSmith 链接。不入库：标准答案和 milestone、原始 ledger、工具输出全文、对照组轨迹 | ADR-0006 和 AGENTS.md「验证与汇报」的要求。PR 正文和 issue 不写题目答案或故障机理（记忆规则，仓库公开）。 |
| A7 | 失败计分 | Run 失败、报告被拒、转换失败都按 0 分计，原因分列 | 和 COB 对“缺答案文件”的处理一致（`evaluation.py:428-440`）。 |
| A8 | 费用 | 按 token × 单价计算（DeepSeek 余额是账户级，多 key 共用），写入任务记录 | 记忆「DeepSeek 余额是账户级」。 |
| A9 | 密钥扫描 | 冻结证据前跑 `scripts/check_secrets.py` 和 gitleaks；快照里如有假密钥，按精确路径加值写 allowlist | 沿用 rental-2026-11-23 的做法和 gitleaks 先例。 |

## 未确认

- 发布成绩是全集还是 test split 上的成绩，以及每题跑了几次。
- 57 标签清单加 TrainTicket 服务名后，题面是否在 8192 字符以内（需实测）。
- 单题耗时、可用并发数、总时长和费用（冒烟后估算）。
- COB 的 MCP 服务和工具是否依赖 Python 3.10+ 以外的库（`cloudopsbench_mcp/requirements.txt` 未读）。
