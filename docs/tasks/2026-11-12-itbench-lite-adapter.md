# ITBench-Lite SRE 离线快照评测适配

- 状态：实施中（#581 本地实现完成待推送；L6 已由用户 2026-11-13 裁决为 a）
- 更新日期：2026-11-13
- 依据：[工具面拓展任务记录](2026-11-12-tool-expansion.md)、[决定表 r4](2026-11-12-tool-expansion-contract.md)（L1 至 L16）；用户 2026-11-12 的四条对齐；ITBench 官方 agent 仓库 `30673b23` 与评测器 `ITBench-Evaluations@14f026fc`（查于 2026-11-12）；数据集 `ibm-research/ITBench-Lite` SRE `v0.2`。issue #571（35 题真实运行）、#581（数据转换与告警投递）、#582（报告转答案与判分）、#583（假 API server）。两份回放探针（2026-11-04 指标与 trace、2026-11-12 集群对象与日志）和开工前预审都在仓库外。
- 工作区：#581 在 `/Users/shenghuikevin/dev/AI/poa-lite-581`，分支 `feature/itbench-lite-581-convert`（起点 `origin/main` 52d640a0）。

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

## 待用户裁决：L6

官方 LLM judge 用哪个模型、发给哪家。发出去的是我们的报告转换结果和公开数据集的标准答案，没有非公开数据。

- a：用仓库已经在用的接收方（Z.ai、OpenAI、DeepSeek 之一）。不新增外部服务；结果要写明「judge 与官方示例不同」。
- b：照官方示例经 OpenRouter 用 `gemini-2.5-pro`。与官方示例可比；OpenRouter 是新的数据接收方。

官方榜单实际用哪个 judge 未确认。裁决前其余部分照常开工，judge 只是最后一步的一个配置。

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

L6 裁决后写入决定表。实现会话可先做不依赖 A 系列的部分；假 server 在 A2 接口合并后做。

## 实施记录（#581：快照转换与告警投递）

代码在 `scripts/itbench_lite/`：`snapshot.py`（白名单加载器）、`shift.py`（统一平移）、`metrics.py`、`traces.py`、`alerts.py`；测试 `tests/test_itbench_lite_{snapshot,shift,metrics,traces,alerts}.py`，全部合成数据。各模块可用 `python -m scripts.itbench_lite.<模块>` 单独跑，真实灌数据与投递等租机。

自行决定的可逆细节：

- 平移量取整秒并向下取整：所有时间戳保留原有小数位与格式，最晚结束时刻落在「投递时刻 − 60 s」之前不到 1 秒。
- 各类的结束时刻：指标取有值行的时间戳；trace 取 span 开始加时长，再与 span 事件时间取最大；告警取所有告警文件里的 `activeAt`；对象只取采集时间列（对象体里可能有过期日之类的未来值）；事件取采集时间与事件体里的 RFC 3339 时间；日志取 `Timestamp`。
- 对象与事件行的平移改写 `Timestamp`、`TimestampTime` 和 `Body` 里所有恰好是 RFC 3339 的字符串，Go 零时刻不动；日志只改时间列，正文不改。
- 答案文件检查只看名字，大小写不敏感，覆盖整个目录树（含同名目录）和白名单文件的符号链接目标；名单：`ground_truth.yaml/.yml`、`assertion.json`、`status.json`。
- 指标去重保留第一个值并计数冲突；空值、坏 tags、非法指标名或标签名的行跳过并计数；派生 `calls_total` 替换有对应 `+Inf` 桶的原序列，没有桶的原序列保留；无标签序列不写花括号。
- trace 任何一行转换失败就报错退出（不静默丢 span）；SpanKind 与 StatusCode 同时认 ClickHouse 的 `Server`/`Error` 写法和 proto 的 `SPAN_KIND_*`/`STATUS_CODE_*` 写法，未知值报错；按服务、资源属性、scope 分组，带上 links。
- 入口告警只取 `state=firing`；同一时刻按排序后的标签打破平局。webhook 的 `startsAt` 保留原小数位，`endsAt` 用零时刻，不带 `generatorURL`（Prometheus 告警 API 没有这一项，所以问句里不会出现 PromQL）。
- 目标条目配置只传 `match.labels`；选出的告警不带这些标签就报错，不投递。默认配置用告警的 `service_name`。
- 投递与 trace 灌入只允许回环地址。2 分钟作废线提供成纯函数 `delivery_gap_ok`，由驱动调用。

偏离与已知限制：

- 指标 TSV 的列名（`timestamp`、`metric_name`、`value`、`tags`、`namespace`、`pod_name`、`service_name`）沿用 11-04 原型，本机没有真实指标样本可核对；trace 列名同理。对象与事件的平移在本机真实片段（S16，4601 行对象、872 行事件）上跑通。
- 对象体里恰好是 RFC 3339 的业务值（例如 ConfigMap 数据）也会被平移；日志正文里的时间不平移。
- 告警文件的三种外形（API 信封、`{"alerts": [...]}`、裸列表）都接受，真实文件的外形未在本机核对。
