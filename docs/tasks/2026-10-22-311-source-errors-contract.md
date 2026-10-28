# #311 合同预审全文（r1）

> 收录自预审工件 `tmp/prereview-311.md`（2026-10-22）。U1–U6 用户裁决：按推荐（2026-10-22）。预审写于 #308 PR2 合并前，R5 的 `v9` 实施时改为 `v10`（#308 PR2 先合，main 已是 v9），见[任务记录](2026-10-22-311-source-errors.md)。


基线 `origin/main` `aa57f9a`；并行 #307 A 分支 `origin/feature/F3-307-prom-discovery`（PR #318，`261f7ac`）。上游 HolmesGPT `a0009195`。行号除特别说明外都按 main。

## 现状要点（已核对）

- 源错误的处理路径：传输层对 4xx 读取至多 `ERROR_BODY_BYTES=8192` 字节后返回 `_Fetched(code, body)`（`otel_demo.py:247,1049-1067`）；5xx 直接抛 `TransportUnavailable`，503 加 `errorType=timeout` 时抛 `TransportTimeout`（`:1053-1066`）。`_metrics` 和 `_traces` 把非 200 放进 `source_status`（`:1134-1137,1183-1186`）。执行器在 `_inspect` 里按 `classify` 映射成固定码，正文不进视图（`executor.py:1268-1274`；`registry.py:524-527`，未登记的码一律 `SOURCE_ERROR`），`_history` 也不保留（`executor.py:1406-1410`）。
- 目前的映射：metrics 400/422 判 `INVALID_PARAMS`（`otel_demo.py:563-569`），traces 只有 400（`:656-662`）。401/403/404/429 都落到 `SOURCE_ERROR`。#307 A 的 R9 把 401、403 明确写成 SOURCE_ERROR，并引入了 `PROMETHEUS_TOOLS` 集合（分支 `otel_demo.py:160-162,1392`）。
- 拒绝视图由 `_refuse` 生成，`trust: "gateway"`，`content: null`，可以通过 `detail` 附加固定字段（`executor.py:1606-1647`，`_refusal_detail` 在 `:1674-1692`）。证据视图的 trust 是 `"untrusted-evidence"`（`:1509`）。模型收到的是 `canonical(visible_view(view))`（`loop.py:836,864`）。trace 的 `gen_ai.completion` 导出完整 `model_view`（`tracing.py:525-533`）。页面步骤表只显示 `tool.reason`（`incident.html:318`）。
- 报告规则：error 视图不能支撑任何 fact，只能写进 gaps（`reports.py:75-77`）。`RUN_COVERAGE_TEMPLATE` 在最终请求里列出非 ok 视图（`:205-213`）。gaps 由模型书写，没有系统确定性生成的 gaps。
- 初始上下文：`evidence_context` 以 canonical JSON 作为一条 user 消息发出（`context.py:661-662`），并先经过字段白名单投影（`reports.py:701-765`，类型 `opspilot-evidence-context-v4` 在 `:26`）；构造点在 `otel_demo.py:775-789` 和 `fixture.py:180-188`，续接路径在 `context.py:1474`，eval 路径在 `evaluation/live.py:149`。仓库里没有任何启动期或提交期的数据源健康探测；profile 二选一（`profiles.py:80-87`）。
- revision：`TOOL_SCHEMA_REVISION` 对 TOOL_SCHEMAS、registry 指纹（含 `error_classes` 与描述）和 `PROJECTION_REVISION` 取哈希（`otel_demo.py:668-686`）；`PROJECTION_REVISION="m1-01-tool-view-v8"`（`outcomes.py:62`）；fixture 的 revision 是手工编号 `fixture-3`（`fixture.py:83`）。bump 之后，被重新领取的在途 Run 进入 `blocked(INCOMPATIBLE_STATE)`，规定不得为避免 blocked 而不 bump（C3 `technical-proposal…:191-193`）。F7 先例：v7→v8，fixture-2→fixture-3（`tests/acceptance/test_f7_step3_source_secrets.py:333-334`）。
- eval 录制校验 `_check_view` 拒绝拒绝视图上的任何额外键（`evaluation/tool_provenance.py:557-570,635`）。新字段不在这里放行，录制校验就会失败。
- 威胁：Observer 预审记录过 401/403 和代理错误页会回显 `Authorization: Bearer …`（`docs/tasks/2026-10-22-f7-observer-redaction-contract.md:25,124`）。`redact_credentials` 识别不了没有命名上下文的裸秘密（`executor.py:19-21`；`f7.md:30`）。非 JSON 文本里 JSON 转义的 `\/\/user:pw@host` 也漏（observer 记录 `:48`）。
- 上游：只有 400 和 429 回传 `HTTP {code}: {error}`，没有长度上限；其他状态码把整个 `response.content` 原样回传（`prometheus.py:1964-1981`）。缺失数据源方面，系统提示列出 DISABLED/FAILED 工具集及其 error，并指示模型告诉用户（`_toolsets_instructions.jinja2:25-36`）。
- 真实证据：多份 e-class 样本报告出现 INVALID_PARAMS（`docs/evidence/m1-01-e-class-attribution/samples/*/report.txt`），模型确实会写错查询，而且拿不到错误原因。

## 需用户裁决（模型可见面、数据合同；本 PR 属用户门）

| 编号 | 问题 | 选项与代价 | 推荐 | 影响面 | 裁决 |
|---|---|---|---|---|---|
| U1 | 哪些响应回传原文 | a. 只回传 `PROMETHEUS_TOOLS` 的 **400/422**，且 body 能解析成 Prometheus 错误信封 `{"status":"error","errorType":str,"error":str}`；其余 4xx 不回传原文。<br>b. 照搬上游 400+429。429 来自代理而非 Prometheus（Prometheus 不返回 429，**未确认**于官方文档本次未核），body 不是对模型查询的回显。<br>c. 加上 Jaeger 400：参数全部由网关校验（`otel_demo.py:1157-1170`），400 罕见，信封格式**未确认** | **a** | executor、otel_demo | |
| U2 | 不回传原文的 4xx（401/403/404/429）对模型和操作员是否可区分 | a. 视图只加数字 `http_status`，不带正文，reason 仍为 SOURCE_ERROR；这样「缺权限」变得可见（PRODUCT-CONSTRAINTS:24），而 401/403 正文有回显凭据的风险，不回传。<br>b. 新增 reason，例如 `SOURCE_PERMISSION_DENIED`：要改 `ERROR_REASONS`（`outcomes.py:88-97`）、`SOURCE_ERROR_REASONS`、provenance 的 allowed 集合（`tool_provenance.py:319-339`）和 #307 A 的 R9。<br>c. 不变 | **a** | 视图、provenance | |
| U3 | 字段放在哪、什么形状 | 在拒绝视图上新增嵌套对象 `source_error: {"trust":"untrusted-evidence","http_status":int,"error_type":str\|null,"error":str\|null,"error_truncated":bool}`，`content` 保持 null，`citable_as_fact` 保持 false。之所以用嵌套对象：外层视图 `trust:"gateway"` 是网关自己写的内容，源文本不能混进同一层（`executor.py:1626`）。不复用 `message` 键，它专指固定的网关文案（`_refusal_detail`）。代价：provenance 和 web 都要认这个新键 | **按此** | executor `_refuse`/`_inspect`、provenance | |
| U4 | 错误原文算不算证据 | a. 不登记 EvidenceRecord，没有 evidence_id，不能引用；只出现在视图里（随 step 行持久化，并进 trace）。与上游的「错误原文当证据」不同，理由仍成立：报告规定 error 视图不支撑 fact（`reports.py:75-77`）。<br>b. 作为未采纳的 history 证据登记：会推翻 `_history` 的先例（`executor.py:1406-1410`）和 `test_m1_tool_boundaries.py:1586` | **a** | | |
| U5 | 「不可用数据源」的表达方式与口径 | a. 在 `evidence_context` 新增白名单键 `data_sources`：按 profile 固定生成，列出 `available`（源→工具名）和 `not_integrated`（K8s 状态、pod 日志、变更历史），附一句固定指引（照搬上游 `:36`，改为「所需数据属于未接入源时，在 gaps 写明缺哪个集成，不得据此推断健康」）。随 Run 输入在提交时记录，续接和 eval 路径要同步。模板文字纳入 `tool_schema_revision` 哈希。<br>b. 写进 discipline 变体：bump `prompt_revision`，影响所有 profile，以及 F3 冻结比较的 versions。<br>c. 只靠报告 gaps 和 `RUN_COVERAGE`：模型不知道缺什么，不满足 issue 第 2 点。<br>口径：本 PR 只做 profile 静态的「未接入」，加上逐次调用的状态（U1/U2）。上游那种运行期 **FAILED**（启动探测鉴权或可达性）推迟另开 issue：工作台不持有源凭据（`profiles.py:13-17`、C3 §3），探测要么放在 worker，要么改输入记录时点 | **a**，FAILED 推迟 | reports 投影、otel_demo/fixture face、`context.py:1474`、`evaluation/live.py:149` | |
| U6 | 操作员在页面上能看到什么 | a. 步骤表在 reason 后显示 `http_status`，以及截断后的 `error` 文本（已脱敏）；页面另显示本 Run 的 `not_integrated` 列表。<br>b. 页面不改，只靠 trace。不满足 PRODUCT-CONSTRAINTS:24 的操作员可见要求 | **a** | `incident.html:316-318`、web service | |

## 可逆默认（Agent 自定）

| 编号 | 决定 | 理由 |
|---|---|---|
| R1 | 处理顺序：先确认 body 是完整的 UTF-8 JSON 信封（`ERROR_BODY_BYTES` 截断后不可解析即不回传原文，与 U8-a 的失败即关闭一致，见 `test_f3_pr1_acceptance.py:104-110`），再取 `errorType` 和 `error` 解码后的字符串，经 `redact_credentials` 处理，最后截断 | 解码后再脱敏，可以绕开 JSON 转义 `\/\/` 的漏洞；先脱敏再截断，不会把凭据截成认不出的碎片 |
| R2 | `error` 截到 1024 个字符（按 code point），超出时 `error_truncated=true`，不在文本里拼标记；`errorType` 必须匹配 `^[a-z_]{1,32}$`，否则置 null | Prometheus 的解析错误通常远短于 1 KiB，查询上限是 `MAX_PROMQL_CHARS=2000`（`otel_demo.py:213`）。上游没有上限，这里有意收紧 |
| R3 | 只处理 `sent=True` 的真实响应。预发送拒绝（`_refused`，`sent=False`，`otel_demo.py:1271-1287`）保持原样 | 那类 body 是网关自己写的，已有 frame 细节 |
| R4 | 人工决定优先：暂停或取消落在请求途中时，照旧返回 denied 加决定码，不附 `source_error` | `executor.py:1020-1033` 规定的优先级 |
| R5 | `PROJECTION_REVISION` → v9，fixture → `fixture-4`，metrics 描述的 `limits` 补一句 400/422 原文语义（`otel_demo.py:500-507`）；traces 描述不变 | 视图形状变了；C3 §5 规定不得为避免 blocked 而不 bump；F7 先例 |
| R6 | provenance 只对 dispatched 的 (error, INVALID_PARAMS 或 SOURCE_ERROR, confirmed) 放行 `source_error`，校验形状、长度和 trust 值；它的缺席也合法，所以旧录制仍然有效 | 原始错误体不入库，provenance 无法复算文本，只能校验形状 |
| R7 | `data_sources` 不加 evidence_context 类型版本号（仍是 v4，新增可选键）；旧 Run 没有这个键，投影结果不变 | 白名单投影会丢弃未列出的键，所以兼容 |

## 与 #307 A、#308 的组合

- #307 A（PR #318）新增的 `get_metric_names`/`get_label_values` 属于 `PROMETHEUS_TOOLS`，400/422 映射也一致（R9），U1 自动覆盖它们，实现时按集合判定，不按工具名。#307 A 明写 `PROJECTION_REVISION` 不变，#311 会 bump：谁后合并，谁 `git merge origin/main` 并重算被钉住的 revision 常量（`test_f3_step5_competing_scenario.py:39,98,109`、`test_f7_step3_source_secrets.py:333-334,352`、`test_m1_series_note_contract.py:102`），由独立测试作者改。
- #308 也会改 `_refusal_detail` 和 `_check_view`，并 bump projection。两个都 bump 到 v9 会撞号，后合并的一方改用 v10。`source_error` 不是证据，#308 的回读工具读不到它，属预期。
- #309（PR #322，并行工具）与本项无交集，**未确认**合并后的执行顺序是否影响录制。

## 允许改动的文件

`opspilot/tools/executor.py`、`opspilot/tools/otel_demo.py`、`opspilot/tools/fixture.py`（只动 revision 与 face）、`opspilot/tools/outcomes.py`（只动 revision）、`opspilot/investigation/reports.py`（只动投影白名单）、`opspilot/investigation/context.py`（续接时携带）、`opspilot/evaluation/tool_provenance.py`、`opspilot/evaluation/live.py`、`opspilot/web/templates/incident.html` 及其 service 投影、任务记录与证据。不改 discipline、REPORT_CONTRACT、redaction 规则、Observer。

## 给独立验收测试作者的可观察行为

1. 真实 socket 桩返回 400 `bad_data` 信封：结果为 (error, INVALID_PARAMS, confirmed)，`evidence` 为 None；视图的 `source_error.error` 包含桩里的 parse error 文本，`http_status=400`，`error_type="bad_data"`，`trust="untrusted-evidence"`，`content` 为 null。422 `execution` 同样处理。
2. 错误文本里夹带 `password=…`、`Bearer …` 和 URL userinfo：视图、trace 属性和 step 存储里都看不到合成秘密。
3. 2000 字符的 error：截成 1024 个字符，`error_truncated=true`。非 JSON body 或被截断的 body：结果码不变，`error` 为 null。
4. 401（正文回显 Authorization）和 403、404、429：视图里只有 `http_status`，没有正文文本，reason 为 SOURCE_ERROR。
5. 5xx（含 503 timeout）的视图与现在逐字节相同，不出现 `source_error`。traces 400 不带正文文本。
6. 请求途中暂停：返回 denied/SUSPENDED，不附 `source_error`。
7. 初始上下文消息里有 `data_sources`，列出两个可用源和三个未接入源；续接 Run 也带着；伪造键被投影丢弃。
8. 页面步骤表显示 `http_status` 和脱敏后的文本，并显示未接入列表。
9. `TOOL_SCHEMA_REVISION` 可以重算并且已变化，`PROJECTION_REVISION` 为 v9，fixture 为 `fixture-4`；带 `source_error` 的录制能通过 provenance 校验，形状非法时被拒绝。

## 最小真实证据（B 批：本 PR 不做质量验收）

- lab 直连一次：用真实执行器经 lab 只读代理发一个故意写错的 PromQL，记录真实 Prometheus v3.5.0 的 400 信封经过代理后的视图（证明代理没有改写 body）。
- 一次有界的真实 Run（提问不点名工具）：附 LangSmith trace 和 `summary.json`，证明 `data_sources` 进了初始上下文、视图形状合法。模型是否自然触发 400 不作为判据；触发了就引用那一处，没触发如实写「未触发」。

## 未确认

- Prometheus 是否会返回 429，以及 400/422 的官方语义：本次没有查官方 API 文档。
- B2 冻结录制和 #300 是否依赖当前 projection revision。
- Jaeger 的 400 信封格式。
