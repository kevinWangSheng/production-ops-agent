# ITBench-Lite SRE 离线快照评测适配

- 状态：#581（PR #586，`4f9b42b2`）与 #582（PR #587，`be92dc65`）已合并（2026-11-13）；#583 等 #555、#558；#571 等租机；L6 = a（用户 2026-11-13）
- 更新日期：2026-11-13
- 依据：[工具面拓展任务记录](2026-11-12-tool-expansion.md)、[决定表 r4](2026-11-12-tool-expansion-contract.md)（L1 至 L16）；用户 2026-11-12 的四条对齐；ITBench 官方 agent 仓库 `30673b23` 与评测器 `ITBench-Evaluations@14f026fc`（查于 2026-11-12）；数据集 `ibm-research/ITBench-Lite` SRE `v0.2`。issue #571（35 题真实运行）、#581（数据转换与告警投递）、#582（报告转答案与判分）、#583（假 API server）。两份回放探针（2026-11-04 指标与 trace、2026-11-12 集群对象与日志）和开工前预审都在仓库外。
- 工作区：#581 在 `/Users/shenghuikevin/dev/AI/poa-lite-581`，分支 `feature/itbench-lite-581-convert`；#582 在 `/Users/shenghuikevin/dev/AI/poa-lite-582`，分支 `feature/itbench-lite-582-scoring`（均起于 `origin/main` 52d640a0）。

## 目标

把 35 个离线场景逐题回放成 OpsPilot 现有工具能查的端点，跑调查，把报告转成 ITBench 的答案格式并判分。这是评测设施，产品代码不改；结果只代表 Lite 轨道，与现场版分开报。

## 行为

一题的流程：

1. **回放**：指标灌进 Prometheus，trace 灌进 Jaeger；集群对象、事件、日志由一个只读的假 Kubernetes API server 提供。全部只绑本机回环地址。六类数据共用一个时间平移量，让快照末尾落在投递告警前约 1 分钟。
2. **触发**：从这题的告警文件里选一条入口告警，转成 Alertmanager webhook 投给现有告警入口。灌完立刻投；间隔超过 2 分钟这次尝试作废。
3. **调查**：照常跑，档位用扩展档位 `otel-demo-ext`。
4. **转换**：评测侧不调模型，把报告和工具调用记录转成官方的 `agent_output.json`。
5. **判分**：官方 LLM judge 分和确定性实体匹配分都报。

假 API server 的行为：按「虚拟当前时刻之前最近一次全量」回答对象查询；事件由 watch 流重放成列表；读 Secret 返回 403；35 题里从没出现过的资源类返回 503；要上一次容器日志返回 400。

输出文件的字段（查于 2026-11-12，官方 agent 提示词）：`entities[]` 每项有 `name`（写成 `namespace/Kind/name`）、`contributing_factor`（布尔）、`reasoning`、`evidence`；`propagations[]` 每项有 `source`、`target`、`condition`、`effect`；`alerts_explained[]` 每项有 `alert`、`explanation`、`explained`。字段名以钉住版本的官方评测器实际读取的为准：#582 开工先写一条结构合同测试，用合成输出（无报告、有实体、有传播链三种）过一遍评测器的加载与解析，不调 judge，字段对不上就先改这段再写转换。

## 用户已定（2026-11-12）

- 报告格式不改，转换在评测侧做且是确定性的。
- 两种分都报。
- 35 题全留分母，按数据覆盖分档，标出数据残缺的 S38、S18。
- 第一轮只跑 OpsPilot，标为单边结果；HolmesGPT 对照之后补。

## 转换能做到什么程度（预审结论）

报告里没有结构化的实体字段，也没有传播边。所以：

- **根因实体**：能部分导出。用这题快照里的对象名加服务名当词表，在报告的结论与各条论断里匹配；资源类型取被引证据的工具参数。「哪个是根因」按规则近似（总结里第一个提到的实体），是启发式。
- **传播链**：基本导不出，只抽总结里显式的「A → B」写法，抽不到就留空。官方的传播链分标「受转换限制」。
- 根因是 ConfigMap、NetworkPolicy 这类对象的题，要靠报告文字写出对象名，丢得比服务级的题多。

这些限制随结果一起对外写明。

## 已裁决：L6

用户 2026-11-13 裁决 a：官方 judge 用仓库已有接收方（Z.ai、OpenAI、DeepSeek 之一），不新增 OpenRouter；结果写明「judge 与官方示例不同」。

## 这个 PR 做什么

全部放 `scripts/itbench_lite/`（不进产品镜像），分两步：

- **本机可做**（纯 Python 加单元测试，用合成数据）：转换器加固；假 API server；告警转换与时间平移；报告转答案格式；确定性实体打分与覆盖分档表；官方评测器的调用封装；评测驱动支持扩展档位。
- **等租机**：灌 Prometheus 与 Jaeger、35 题 × 3 次真实 Run、LangSmith trace、真实数据上的投递间隔核对。

答案隔离：加载器按白名单只读指标、告警和四个 TSV；标准答案只在判分进程里读；数据目录里有答案文件时拒绝启动。入库的只有每题分数、覆盖档位、`summary.json` 与 trace 链接。

## 不做

改产品报告格式、HolmesGPT 对照组、现场版、用 kwok 或 kind 加载快照、自定义资源回放。

## 完成条件

1. 代码分三个 ticket，各自的 PR 合并后关闭：#581、#582 不依赖工具，可立即开工；#583 在 A2、A3 合并后做，本机用真工具打回环上的假 server 的集成测试通过。
2. #571 关闭：租机后 35 题跑完，仓库里有分数表（两种分、覆盖分档、限制说明）、`summary.json` 与 trace 链接。

## 已知风险

1. 即时查询以查询窗终点为时刻，投递晚于快照末尾超过约 5 分钟会全空，所以有 2 分钟作废线。
2. 日志只有经 OTLP 上报的应用日志，没有容器维度，也分不出上一次崩溃前的部分。
3. trace 只有 9 题覆盖故障窗。
4. 没有 kube-state-metrics 与 rpc 指标；调用总数的分母是派生的。
5. `kubectl` 对假 server 的兼容性未确认，影响以后补 HolmesGPT 对照。

## 依赖

假 server 的消费方是 A2（对象与事件）、A3（日志）、A6（按列查询），#583 等这三项合并后做，集成测试三个工具都打；扩展档位来自 A2；trace 新工具来自 A4，即时查询来自 A5。转换器、告警转换、报告转答案、两种判分不依赖 A 系列，现在就能开工。

## 下一步

L6 已写入决定表。实现会话先做不依赖 A 系列的部分；假 server 在 A2 接口合并后做。

## 实施记录（#582）

代码在 `scripts/itbench_lite/` 的 `answer.py`、`entity_match.py`、`coverage.py`、`official_eval.py`。

结构合同（2026-11-13，评测器 `14f026fc` 克隆在仓库外 `~/.agent/eval-probes/itbench-evaluations-14f026fc/`，用其 `uv.lock` 建独立 venv）：
- 评测器的确定性代码不读 `agent_output.json` 的任何字段：加载器只要求 `<根>/<场景目录>/<数字 trial>/outputs/agent_output.json` 能解析成 JSON，整份对象原样序列化进 judge 提示词。字段名由 judge 提示词消费：实体的 `name`、`contributing_factor`，以及 `propagations`。与上文字段一致，无需改。
- 场景目录名不区分大小写地按 `scenario-<id>` 等候选解析；trial 目录缺文件记 bad run，且该题没有任何可读输出时整题不进评测，所以 L5 必须写出空结果文件。

可逆技术细节（每条一行理由）：
- 名字边界为 `[A-Za-z0-9_-]` 以外的字符、不区分大小写、重叠取最长：Kubernetes 名字含连字符，`\b` 会把 `checkout` 匹配进 Pod 名。
- Kind 规则依次为：被引证据带 `kind` 且其他参数里出现该名字；`pod`/`pod_name` 等于该名字取 Pod；`service` 或 PromQL `service_name="…"` 取 Service；只提到这一个实体的论断所引证据的 `kind`；否则 Service。`events` 不算实体类型。理由：把 L1「取工具参数」写成可测的顺序。
- namespace 取定出 Kind 那条证据的 `namespace` 参数，否则 `otel-demo`：快照目录里 namespace 的来源以 #581 为准。
- `alerts_explained` 恒为空：L1–L5 没有规则，钉住版本的 judge 提示词也不给它打分。
- 「启发式」「受转换限制」写在 trial 目录的 `conversion_meta.json` 与 judge 标签文件里，不写进 `agent_output.json`：评测器把整份对象发给 judge，额外字段会进提示词。
- 确定性匹配的 filter 正则对对象名和完整 `namespace/Kind/name` 都试 `re.search`：judge 示例里的 filter 两种写法都有可能。
- judge 默认接收方 Z.ai（`glm-5.3-flashx`），`ITBENCH_JUDGE_PROVIDER` 可选 openai、deepseek，其他一律拒绝；子进程只拿 `JUDGE_*` 三个变量、`PYTHON_DOTENV_DISABLED=1` 与基础路径（评测器会 `load_dotenv` 并向上找 `.env`），工作目录有 `.env` 仍拒绝；路径在启动子进程前转成绝对路径。
- L5 的强制：`conversion_meta.json` 里 `report_present=false` 的 trial 不送 judge，`trial_scores.json` 里这类 trial 全部指标记 0 并留在分母；评测器自带的 statistics 不含它们，不作为公布分数。
- 分档清单必须正好是官方 35 个场景 ID（常量取自仓库外两次普查的场景目录名，只用目录名）。
- 结构合同测试设了 `ITBENCH_EVALUATIONS_DIR` 才跑，CI 跳过。

偏离与已知限制：
- 第一个实体若被 `rejected_hypothesis` 提到，则整份报告没有根因，不顺延到下一个实体（按 L2 字面）。
- 只在 summary 里出现、没有论断提到的实体，`reasoning` 与 `evidence` 为空串（L4 只说用论断原文）。
- 集群级对象（Node、Namespace）也写成 `<namespace>/Kind/name`，官方对集群级对象的写法未确认。
- GT 的 `kind` 若不是标准 Kubernetes Kind（judge 提示词提到 Chaos、flagd 类），确定性匹配不会命中，只有 judge 分能覆盖。
- 本 PR 不调用 judge；分档清单与真实分数等 #571。

## 实施记录（#581：快照转换与告警投递）

代码在 `scripts/itbench_lite/`：`snapshot.py`（白名单加载器）、`shift.py`（统一平移）、`metrics.py`、`traces.py`、`alerts.py`；测试 `tests/test_itbench_lite_{snapshot,shift,metrics,traces,alerts}.py`，全部合成数据。各模块可用 `python -m scripts.itbench_lite.<模块>` 单独跑，真实灌数据与投递等租机。

自行决定的可逆细节：

- 平移量取整秒并向下取整：所有时间戳保留原有小数位与格式，最晚结束时刻落在「投递时刻 − 60 s」之前不到 1 秒。
- 各类的结束时刻：指标取有值行的时间戳；trace 取 span 开始加时长，再与 span 事件时间取最大；告警取所有告警文件里的 `activeAt`；对象只取采集时间列（对象体里可能有过期日之类的未来值）；事件取采集时间与事件体里的 RFC 3339 时间；日志取 `Timestamp`。
- 对象与事件行的平移改写 `Timestamp`、`TimestampTime` 和 `Body` 里所有恰好是 RFC 3339 的字符串，Go 零时刻不动；日志只改时间列，正文不改。
- 答案文件检查只看名字，大小写不敏感，覆盖整个目录树（含同名目录）和白名单文件的符号链接目标；名单：`ground_truth.yaml/.yml`、`assertion.json`、`status.json`。目录树里有目录符号链接或有子目录列不出来时也拒绝启动（fail-closed）。
- 指标去重保留第一个值并计数冲突；空值、坏 tags、非法指标名或标签名的行跳过并计数；派生 `calls_total` 替换有对应 `+Inf` 桶的原序列，没有桶的原序列保留；无标签序列不写花括号。
- trace 任何一行转换失败就报错退出（不静默丢 span）；trace ID 须为 32 位、span ID 与 parent 须为 16 位非全零十六进制（parent 可空，links 同样校验）；SpanKind 与 StatusCode 同时认 ClickHouse 的 `Server`/`Error` 写法和 proto 的 `SPAN_KIND_*`/`STATUS_CODE_*` 写法，未知值报错；按服务、资源属性、scope 分组，带上 links。
- 入口告警只取 `state=firing`；同一时刻按排序后的标签打破平局。webhook 的 `startsAt` 保留原小数位，`endsAt` 用零时刻，不带 `generatorURL`（Prometheus 告警 API 没有这一项，所以问句里不会出现 PromQL）。
- 目标条目配置只传 `match.labels`；选出的告警不带这些标签就报错，不投递。默认配置用告警的 `service_name`。
- 投递与 trace 灌入只允许回环地址，发送不走代理、不跟随重定向（30x 直接报错，避免 token 被带到新地址）。2 分钟作废线提供成纯函数 `delivery_gap_ok`，由驱动调用。

偏离与已知限制：

- 指标 TSV 的列名（`timestamp`、`metric_name`、`value`、`tags`、`namespace`、`pod_name`、`service_name`）沿用 11-04 原型，本机没有真实指标样本可核对；trace 列名同理。对象与事件的平移在本机真实片段（S16，4601 行对象、872 行事件）上跑通。
- 对象体里恰好是 RFC 3339 的业务值（例如 ConfigMap 数据）也会被平移；日志正文里的时间不平移。
- 告警文件的三种外形（API 信封、`{"alerts": [...]}`、裸列表）都接受，真实文件的外形未在本机核对。

## 实施记录（#583：假 Kubernetes API server）

代码在 `scripts/itbench_lite/fake_k8s.py`，测试 `tests/test_itbench_lite_fake_k8s.py`（合成数据）。读快照走 `snapshot.load_snapshot` 白名单，时间平移复用 `shift.shift_json_times`；`python -m scripts.itbench_lite.fake_k8s <场景目录> --offset-seconds N --token-file F --cert-dir D` 启动，打印 `api_url` 与 `ca_file`，产品侧只把 `OPSPILOT_K8S_API_URL`/`OPSPILOT_K8S_CA_FILE`/`OPSPILOT_K8S_TOKEN` 指过去。集成测试用真实的 `kubernetes_jq_query`、`kubernetes_count`、`kubernetes_tabular_query`、`fetch_pod_logs` 经 `otel-demo-ext` 执行器打 127.0.0.1 上的 HTTPS 假 server 通过。另在仓库外 S16 真实片段（对象、事件、日志开头一段）上核对过格式。

自行决定的可逆细节：

- 「一次全量」= 同一资源类、`Timestamp` 完全相同的一组行（S16 实测每类每 5 分钟一组）；取采集时刻不晚于虚拟当前时刻的最后一组。虚拟当前时刻默认取本机时钟（数据已平移到投递前约 1 分钟）。
- 虚拟当前时刻早于这一类的第一组全量时返回 503「尚未采集」，不返回空列表：没采集不等于没有。
- 「35 题出现过的资源类」用常量 `SEEN_RESOURCES`（11-12 探针整读三题加流式统计两题得到的 28 类，含 events），再并上本题快照里实际出现的类。A2 表里的 cronjobs、replicationcontrollers、apiservices、两类 webhook 配置不在其中，返回 503；对象按 (组, 版本, 复数名) 建索引，版本取对象自己的 `apiVersion`：同组不同版本的对象互不混入，请求没出现过的版本返回 503。`events.k8s.io` 组不提供（503），core `v1` 事件照常。
- Secret 在任何组、列表与单对象都返回 403，先于资源类判断。
- 分页：`limit` 为 0 或省略返回全部；`continue` 令牌记下第一页的虚拟时刻与偏移量，后续页读同一组全量，不受时钟前进影响；坏令牌 400。先按选择器过滤再分页，按 namespace/name 排序。
- label 选择器支持 `=`、`==`、`!=`、`in`、`notin`、存在与 `!` 不存在；field 选择器只支持 `=`、`==`、`!=`，标签集合照真实 API（所有类的 `metadata.name`/`metadata.namespace`，Pod 的 `spec.nodeName`、`status.phase` 等，事件的 `involvedObject.*`、`reason`、`type`、`source`、`reportingComponent`），其他标签 400。
- 事件 watch 重放以 `metadata.uid` 为键，ADDED/MODIFIED 取最新、DELETED 移除，只取 `apiVersion=v1` 那一半。
- 日志：要求 Pod 在当前全量里，否则 404；不给 `container` 且 Pod 有多个应用容器时 400（同真实 API）；`previous=true` 400；`follow=true` 400；`sinceTime` 与 `sinceSeconds` 同给 400。一条记录正文里的换行拆成多行、共用时间戳，末尾换行不产生空行。时间戳格式照 Go `RFC3339Nano`（UTC，去掉小数尾零）。`limitBytes` 按字节截断，可能截在行中间（同真实 API）。
- 只认 GET，其余方法 405；Bearer token 用常量时间比较，不对返回 401。只能绑 IP 形式的回环地址（`localhost` 这类主机名也拒绝）。证书按 L14 用 `openssl` 生成一天有效的自签证书（SAN 为 127.0.0.1 与 ::1），私钥 0600，写进调用方给的目录，不入库。
- 对象体原样返回（含 `managedFields`，真实 API 也返回，A2 自己去掉）；列表项保留快照里的 `kind`/`apiVersion`。

偏离与已知限制：

- 没有 discovery（`/api`、`/apis`、`/version`），`kubectl` 兼容性仍未确认（已知风险 5）。
- 读日志按 `k8s.pod.name` 建索引，100 MB 级日志文件整份读进内存；未在完整真实文件上测过加载时长与内存。
- 坏行即拒绝启动（PR #603 审查后）：对象行缺 Body、Body 不是 JSON 对象、缺 `k8s.resource.name`、对象缺 `apiVersion`/`kind`/`metadata.name`，事件行缺 Body、watch 类型不是 ADDED/MODIFIED/DELETED、对象不是 Event 或版本既非 `v1` 也非 `events.k8s.io/v1`、既无 uid 也无 name，日志行缺 `k8s.pod.name`/`k8s.namespace.name` 或缺 Body 列，以及任何行时间戳解析失败，都抛 `SnapshotRowError`（带文件名与数据行号），不返回部分数据。日志正文为空串照常加载。真实片段 S16 上的对象、事件、日志开头一段都能通过；完整日志文件里有没有不带 Pod 属性的行未确认，若有则会拒绝启动，到时再定。

## 实施记录（#604：逐题编排驱动）

代码：`scripts/itbench_lite/driver.py`（编排与命令行）、`services.py`（docker 与子进程启动器）、`judge_trial.py`（判分子进程）；「冻结一个给定 Run」是 `scripts/run_freeze.py`（`collect`、`trace_roots`、`freeze_run`，命令行 `RUN_FREEZE_DSN=… python -m scripts.run_freeze --run-id … --experiment … --evidence-dir …`），A7 驱动复用。测试 `tests/test_itbench_lite_driver.py`、`tests/test_itbench_lite_judge_freeze.py`（替身启动器、合成数据），以及需 `OPSPILOT_LITE_PG=1` 的 `tests/integration/test_604_lite_intake_postgres.py`（本机一次性 PG 加真 web 进程）。决定表见 [r6](2026-11-12-tool-expansion-contract.md)。

命令行（租机手册阶段 4 引用）：

- 一题一次：`python3 -m scripts.itbench_lite.driver attempt --dataset <数据集根> --scenario <场景目录名> --trial 1 --round <轮次> --vocabulary <词表> --env-file <私有 KEY=value 文件>`
- 全量：`python3 -m scripts.itbench_lite.driver round --dataset <根> --round <轮次> --vocabulary-dir <目录，内含 S<id>.txt> [--scenarios 16,23] [--trials 3]`；已有 summary 的 trial 跳过
- 补判分：`… driver judge --round <轮次> --ground-truth <根>`；汇总给 `coverage.py`：`… driver scores --round <轮次> --out <文件>`
- 可调：`--load-budget-seconds`（灌入耗时上限，默认 600）、`--trace lab|off`、`--no-judge`、`--allow-dirty`、`--prometheus-account`、各端口 `--{pg,prometheus,jaeger-query,jaeger-otlp,fake-k8s,web}-port`

自行决定的可逆细节（每条一行理由）：

- 目标条目写 `health_profile_id: otel-demo-checkout`：预审写「不写」，但身份文件加载器把它列为必填（`opspilot/schema.py` `TARGET_ENTRY_FIELDS`），不写 web 起不来；自动观察已关，这个值不产生行为。
- `loaded_at` 是指标、trace、假 server 与 web 都就绪的时刻；worker 在其后起，起动时间算进 2 分钟间隔：worker 要用假 server 生成的 CA。之后立即投递（L-D7 按 r4 原文改正）；平移量以尝试开始时刻为锚，summary 记 `load_seconds`、`gap_seconds` 与 `data_end_to_delivery_seconds`（≈ 60 s + 灌入耗时 + worker 起动）。
- worker 的就绪判据是「起动 5 秒后仍存活」：健康监听按 L-D3 关闭后没有别的信号。
- 端口释放只核对本次尝试开始时确认空闲的端口：被别人占着的端口不是本次的，核对它会把基础设施失败误判成清理失败。
- 清理失败（含端口没释放）停整轮：状态不可信时继续跑会污染下一次尝试。
- 回放 Jaeger 用根地址（`OPSPILOT_OTEL_JAEGER_URL=http://127.0.0.1:<端口>`，工具在其后直接拼 `/api/...`，`otel_demo.py` 查询处），`MEMORY_MAX_TRACES=1000000`：lab 的 5000 按 trace 个数淘汰，回放一题远超此数。
- promtool 与 Prometheus 容器都用 `--user <宿主 uid:gid>`，promtool 加 `--network none`：避开镜像 nobody 用户写宿主目录的权限问题（预审风险 5）。
- 工作区「不干净」只看已跟踪文件，且不算本轮证据目录：续跑时新写的 summary 不应让第二次调用拒绝启动。
- 判分子进程的环境：基础路径、`PYTHON_DOTENV_DISABLED=1`、评测器位置、接收方选择与所选接收方的 key/地址/模型三个变量；输出丢弃。只持久化数值分数：匹配明细不写；评测器工作目录（暂存输出与 judge 结果，含由答案派生的 judge 文本）读出数值行后删除，judge 标签并入分数文件。judge 失败仍写确定性分（退出码 3），官方分留空，`driver judge` 补判。官方分加 `root_cause_entity_pass`，确定性分键名 `det_*`。
- 完成判定（审查 #606）：答案输出先写、summary 后冻结、判分后最后原子写 `tmp/…/trial-<n>/complete.json`。续跑只跳过「标记 + completed summary + 答案输出 + 含确定性分的分数文件」齐全的 trial，以及带原因的 `status: missing` 终态；summary 与输出在但没标记的只补判分、不重跑 Run；summary 在而输出缺的，旧证据目录移到 `tmp/…/trial-<n>/stale/` 后重跑。中途 Ctrl-C 不写 summary。代码异常（非已分类失败）清理后直接结束进程，不重试。
- 源树检查（审查 #606）：复制前按名称遍历整棵场景目录，不跟随链接；根目录是符号链接、任何一层有目录符号链接、任一目录列不出来，都拒绝。数据集目录本来就带答案文件，答案名只是永不被选中；白名单名字本身或其链接目标是答案名、或不是普通文件，拒绝。复用的副本重新核对：只能有白名单内的普通文件、没有链接、没有答案名。
- 驱动侧输入（`--env-file`、`--prometheus-account`、词表）的名字及解析后目标名命中答案名，停整轮。
- `run_freeze` 字段对照 F3 `collect.py`：报告、引用证据的工具名与存储视图、整份 F3 投影、事故的全部 Run、完整审计时间线、投递与投递结果、最终 `outcome`。与 F3 的差异：不含 F3 的 `frozen`（rubric 哈希，本场景没有）与 `lab_readings`（驱动另记内存读数）；Run 没到终态（等待超时）时 `outcome` 记 `{"available": false, "reason": …}`。
- LangSmith 的 key 可来自环境或 `--env-file`，只传给 worker 与回读 trace；web 不拿任何模型或 trace 凭据。

本机验证到的程度：编排顺序、7 处起动失败、作废线（POST 前与等待过久两种）、端口被占、确定性错误、目标无法解析停整轮、无报告与报告校验失败、等待超时、续跑、judge 重试、命令行，全部用替身测；假 server 用真子进程起停；一次性 PG 加真 web 进程上验证了驱动生成的条目与 webhook 能建出绑定 `m0-otel-20260909` 的 Run（含回退规则），`run_freeze.collect` 能在真实表结构上读出它。未在本机验证：docker 里的 promtool、Prometheus、Jaeger 1.53，worker 真调模型，LangSmith 回读，judge 真调，真实数据上的灌入耗时。

需裁决与风险：

- 作废线已按 r4 原文改为灌完即投。代价：快照末尾早于投递约「60 s + 灌入耗时」；灌入超过约 4 分钟时，以查询窗终点为时刻的即时查询可能落在数据之后（已知风险 1 的 5 分钟线），冒烟看 `data_end_to_delivery_seconds`。
- 入口告警没有 `service_name` 时按 lead 定的回退跑，产物里 `target_fallback: true`；S12 这类题调查对象可能是基础设施噪声（预审风险 1），是否改入口选择规则待定。
- Ubuntu 上 PostgreSQL 17 的 `initdb`、`pg_ctl` 需在 PATH 上（`LocalPgLauncher` 用 `shutil.which`）。
