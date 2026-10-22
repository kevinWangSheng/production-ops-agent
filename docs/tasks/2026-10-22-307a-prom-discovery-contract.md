# #307 A 合同决定表（r2）

预审者：全新上下文 Claude Opus 5.5 子 Agent，只读，2026-10-22。原计划由 Codex gpt-6-astra 执行（会话 `01a16368-ec9c-7a30-90ac-aa160dded585`），但 gpt-6.1-sol、gpt-6-sol、gpt-6-astra 连续 5 轮均返回 at capacity，没有产出决定表。预审只要求预审者未参与实现，所以改由 Claude 子 Agent 执行。基线 `a4eea42`。

裁决：U1、U2 由用户 2026-10-22 裁决；U3、U4、R1–R12 按用户 2026-10-17 常设指示采纳推荐。

## 现状要点

- 只有两个工具（`opspilot/tools/otel_demo.py:148-149`）。指标名单写死在描述里（`:310-316`）。`tool_schema_revision` 是内容哈希（`:651-666`），新增工具必然 bump。
- 只认 `METRICS_TOOL` 的三处，新工具不改就会漏掉：basic auth（`:1011-1013`，lab 代理会返回 401）、503 加 `errorType=timeout` 判超时（`:1037`）、`fetch` 分派（`:999-1003`）。
- 工具名写死的位置：scope（`:1670`）、`scripts/eval_live.py:394` 的 `template_scope`、`opspilot/evaluation/tool_provenance.py:601`。
- 「恰好两个工具」被测试钉住：`tests/test_m1_otel_demo_contract.py:421-423,1238,1524`、`tests/test_m1_whole_view_tokens_contract.py:499`。
- 不完整标记：执行器从原始 payload 顶层读 `incomplete_marker`（`executor.py:1902-1906`）。`truncated` 专指视图省略了行，视图从不截断（`:1477-1483,1563-1577`）。
- 脱敏是通用的，原始内容和视图都会经过 `redact_payload`（`executor.py:1364-1384`），但没有命名上下文的裸秘密识别不了（`:19-21`）。审计时间线从 view 或 result 读工具名，不按名枚举（`acceptance_audit.py:200`）。
- lab 只读代理放行 `/api/v1/series`、`/api/v1/labels`，以及 `^/api/v1/label/[A-Za-z_][A-Za-z0-9_]*/values$`，方法只限 GET/HEAD（`scripts/kind_lab.py:326-365`）。Prometheus 版本是 `v3.5.0`（`docs/evidence/m1-02-lab/versions.txt`）。v3.5.0 按 `limit+1` 判截断，并加 warning `"results truncated due to limit"`。
- 上游：`get_metric_names` 的 `match` 必填，limit 100，默认窗口 1 小时（`prometheus.py:53,61,1040-1127`）；`get_label_values` 的 `match` 可选（`:1145-1207`）；instructions 要求先发现再查询（`prometheus_instructions.jinja2:6-14,36`）。

## 需用户裁决

| 编号 | 问题 | 裁决 |
|---|---|---|
| U1 | #307 审计评论（2026-10-22）把 A 扩大到 trace tag 发现，并要去掉写死的 `SERVICES` 和指标枚举 | **a，用户裁决**：本 PR 只做 Prometheus 发现，扩出来的部分另开 issue。 |
| U2 | 写死的 KSM 名单（F3 D3-a）与完成条件「D3 不再需要写死名单」冲突 | **a，用户裁决**：保留名单；本 PR 在 metrics 描述里加一句「名单以外的指标或标签先用 get_metric_names / get_label_values 发现，只用返回过或已列出的名字」。用户原话要点：issue 的目的是给 Agent 查询工具，不靠人工输入；名单先保留，等发现工具合入 main 后由评测（F3 第 5 步）检验，再决定名单去留。 |
| U3 | 与 F3 PR-3a 的合并顺序，以及被钉住的断言由谁改 | **按推荐**：两者都要在 PR-3b 之前合并；谁后合并，谁用 `git merge origin/main` 吸收对方。本 PR 的独立测试作者更新 main 上被钉住的两个合同测试文件（记为合同变更，实现者不改）。如果 PR-3a 先合并，它的 revision 常量 `test_f3_step5_competing_scenario.py:39,98` 也由本 PR 的测试作者更新；如果本 PR 先合并，由 PR-3a 的测试作者更新。traces 描述保持字节不变。 |
| U3 r2 | 实施中发现另两处钉住两个工具的断言（F7 第 5 步审计测试、M1-04 告警窗口测试） | 同归测试作者；告警窗口断言改为按带窗口参数的工具数推导 |
| U4 | lab 真实 Run 的通过判据 | **按推荐 a**：提问不点名任何工具。通过的条件：至少一次 `get_metric_names` 或 `get_label_values` 返回 `ok` 或 `no_data`，`result_count ≤ 100`，有 EvidenceRecord，`incomplete` 与 warnings 一致；附 trace 与 `summary.json`。模型没调用发现工具记为失败，可重跑一次，失败轮保留。 |

## 可逆默认

| 编号 | 决定 | 依据 |
|---|---|---|
| R1 | 只做 `get_metric_names`、`get_label_values`，名字照搬上游，版本 `v1`，verb `list`。不加 series、all_labels、rules、metadata | 上游 `:1025,1145`；代理不放行 rules 和 metadata |
| R2 | `match`：metric_names 必填，label_values 可选；必须非空，长度 ≤ `MAX_PROMQL_CHARS`。不复用 `promql_problem`（它会误拒字符类）；语法交给 Prometheus 解析，400 判 INVALID_PARAMS；发送时用 `match[]` | `otel_demo.py:213,869-880` |
| R3 | 时间窗复用 `_query_window`：start、end 要么都给、要么都不给；默认取 `default_query_window`，否则取帧末尾往前 1 小时；帧外请求拒绝；以 unix 秒发出 | `otel_demo.py:1216-1251` |
| R4 | label 名发送前按 `^[A-Za-z_][A-Za-z0-9_]*$` 校验，允许 `__name__`，不合格返回 `INVALID_LABEL`，不发送 | 与代理正则一致 |
| R5 | label 名形似凭据（命中 `_authentication_name`）时发送前拒绝，返回 `CREDENTIAL_LABEL_REFUSED`，映射 INVALID_PARAMS | F7；`executor.py:19-21`；`registry.py:34-45` |
| R6 | 固定发送 `limit=100`，模型不可调；原始内容 ≤ 1 MiB、视图 ≤ 25k token、单次超时与现有工具一致 | 上游 `:53`；`otel_demo.py:243,533` |
| R7 | `incomplete_marker="warnings"`；超限时照常返回前 100 条，`incomplete=true`、`truncated=false`；视图加 `incomplete_reason`（有 warning 时提示用更具体的 match 缩小范围，否则为 null）和 `limit: 100`；任何 warning 都会把 incomplete 置真 | `executor.py:1902-1906`；Prometheus `api.go:866-870` |
| R8 | 原始内容存 exact bytes，`result_path=("data",)`，新鲜度 unknown；`cannot_prove`：名字出现不证明窗口内有样本、不证明健康或数值；不在列表里也不证明不存在 | 上游 jinja `:36`；F3 R2 |
| R9 | 400、422 判 INVALID_PARAMS；401、403 判 SOURCE_ERROR；503 加 `errorType=timeout` 判 TOOL_TIMEOUT（把 `:1037` 的判断扩到所有 Prometheus 类工具，F3 U8-a）；其余 5xx 判 SOURCE_UNAVAILABLE；空 data 判 `no_data` | F3 U8-a |
| R10 | basic auth 对所有 Prometheus 类工具发送；新工具名加进 scope、`eval_live.py` 和 `tool_provenance.py`，最好从 registrations 派生；审计不改 | 见现状 |
| R11 | 「先发现再查询」写进工具描述，不改 discipline 和 `prompt_revision` | C3 §5；`discipline.py:29-30` |
| R12 | fixture profile 不加新工具 | `fixture.py:78-84` |

## 接口草案

- `get_metric_names(match: str, start?: ISO-8601, end?: ISO-8601)` → `GET /api/v1/label/__name__/values?match[]=…&start=…&end=…&limit=100`
- `get_label_values(label: str, match?: str, start?, end?)` → `GET /api/v1/label/{label}/values?…`
- 注册项：source `otel-demo`，verb `list`，`result_path=("data",)`，`max_result_bytes` 1 MiB，`max_view_tokens` 25 000，`incomplete_marker="warnings"`，`may_contain_secrets=False`。
- 错误码：`QUERY_OUT_OF_WINDOW`、`MATCH_INVALID`、`INVALID_LABEL`、`CREDENTIAL_LABEL_REFUSED`、400、422 → INVALID_PARAMS；503 → SOURCE_UNAVAILABLE（`errorType=timeout` 时 → TOOL_TIMEOUT）。
- 视图：通用字段（`content` 为名字列表，加上 `result_count`、`incomplete`、实际查询窗口 `window`），以及 `incomplete_reason`、`limit`。
- 描述：五字段齐全，不写 URL 和路径；`limits` 写明 100 条上限和超限语义。metrics 描述加 U2 那句引导。
- 常量：`DISCOVERY_LIMIT = 100`。

## 验收可观察行为

1. 走真实 urllib 加本地 HTTP 桩：请求是 GET，路径正确，带 `match[]` 和 `limit=100`，start/end 等于预期窗口，带 Basic 头。
2. 缺 `match` 或 `match` 为空、label 非法（`a/b`、`..`、`foo-bar`）、凭据样式 label、帧外窗口：都在发送前被拒，状态与原因符合 R9。
3. 桩返回 100 条加 truncation warning：全部返回，`incomplete=true`，`incomplete_reason` 非空，`truncated=false`；没有 warning 时 `incomplete=false`。
4. 空 data 返回 `no_data`；400 返回 INVALID_PARAMS；503 加 timeout 返回 TOOL_TIMEOUT，且源已被联系上；其他 5xx 返回 SOURCE_UNAVAILABLE；超过字节或 token 上限返回 RESULT_TOO_LARGE，且不带内容。
5. 值里含 `password=xyz` 时，原始内容和视图都被脱敏。
6. `TOOL_SCHEMA_REVISION` 已变化，可从代码重新算出；`PROJECTION_REVISION` 和 `prompt_revision` 不变；traces 描述字节不变。
7. 工厂产出的 scope 和 `template_scope` 都包含 4 个工具名。
8. lab Run 按 U4 判定。

## 未确认与风险

- lab 当前实际跑的 Prometheus 版本：真实 Run 时附一次 `/api/v1/status/buildinfo` 的结果。
- bump 之后，在途 Run 被重新领取时会 `blocked(INCOMPATIBLE_STATE)`；旧的评测录制在这个维度上不可比。#300 是否依赖当前 revision，未确认。
- PR-2 的覆盖缺口节怎么处理发现类工具的结果，未确认（PR-2 未开工）。
