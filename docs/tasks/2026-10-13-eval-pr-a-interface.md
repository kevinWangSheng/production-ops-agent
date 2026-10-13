# 场景评测 PR-A 公开接口（合同 r7）

- 依据：[任务记录](2026-10-12-eval-suite.md)（合同 r7）、[合同决定表](2026-10-12-eval-suite-contract.md) 的 r6/r7 节（P1–P13）。
- 用途：PR-A 的公开接口与语义。独立验收测试作者只按本文与合同写测试，不看实现；实现者不得为让测试通过而改本文语义，确需改动按合同变更处理。
- 范围：纯离线、确定性。不调用模型、不读 LangSmith、不连 kind/PG。judge 的真实调用、回放录制、live 驱动、CI 入口分别属于后续 PR。

## 模块与数据位置

- 代码：`opspilot/evaluation/`，公开名称全部从 `opspilot.evaluation` 导入。
- 场景数据：`evals/scenarios/<scenario_id>/scenario.json`（被测模型可见的刺激）与 `answer.json`（只给评测侧）。PR-A 提交 6 个场景：`eval-normal-1`、`eval-normal-2`、`eval-payment-failure-1`、`eval-payment-failure-2`、`eval-product-catalog-failure`、`eval-cart-failure`。

## 类型

所有类型是 frozen dataclass；时间为带时区的 UTC `datetime`。

```python
Mode = Literal["replay", "live", "structural"]
ScenarioKind = Literal["fault", "normal"]

Window(start: datetime, end: datetime)                    # 闭区间；任一端不是 UTC 时区感知时间抛 ValueError("WINDOW_NOT_UTC")，start > end 抛 ValueError("WINDOW_REVERSED")

Stimulus(question: str, subject_id: str, target_id: str)   # 被测模型可见的全部内容
ScenarioSpec(scenario_id: str, version: str, kind: ScenarioKind,
             incident: IncidentScenario, stimulus: Stimulus)

SpanMatcher(service: str, operation: str)                 # 精确匹配，大小写敏感
SignatureRule(rule_id: str, any_of: tuple[SpanMatcher, ...], min_matches: int = 1)
InjectedFault(service: str, service_aliases: tuple[str, ...],
              operation: str, operation_aliases: tuple[str, ...],
              flag: str, variant: str)
Answer(scenario_id: str, version: str, expected_output: tuple[str, ...],
       injected: InjectedFault | None, signature: SignatureRule | None)

DeliveredRecord(evidence_id: str, target_id: str, status: str, citable_as_fact: bool,
                time_scope_refs: frozenset[str], window: Window | None,
                view: Mapping[str, Any], view_sha256: str, delivered: bool)
ReportCapture(raw: str | None, finish_reason: str)
TraceEntry(role: str, required: bool,
           read_back: Literal["found", "root_run_not_found", "no_trace_id", "not_exported", "error"],
           run_id: str | None, expected_run_id: str | None,
           product_run_id: str | None = None)              # 该 trace 所属的产品 Run

AttemptStatus = Literal["ran", "setup_failed", "aborted", "unknown"]
AttemptRecord(attempt_id: str, round_id: str, manifest_hash: str,
              scenario_id: str, scenario_version: str, mode: Mode, repetition: int,
              started: bool, status: AttemptStatus, replay_miss: bool,
              product_run_id: str | None)

JudgedDiagnosis(location: str, quote: str, factual_causal: bool,
                service: str | None, operation: str | None,
                cited_evidence_ids: tuple[str, ...], linked_claim: int | None,
                supported: bool, reason: str)
JudgeStatus = Literal["ok", "error", "timeout", "invalid", "oversize"]
JudgeResult(status: JudgeStatus, calibrated: bool, diagnoses: tuple[JudgedDiagnosis, ...],
            verdict: bool | None, config_hash: str, calibration_hash: str)

GateResult(passed: bool, checks: Mapping[str, bool], reasons: tuple[str, ...])
RunEvaluation(attempt: AttemptRecord, gate: GateResult,
              signature_matches: Mapping[str, tuple[str, ...]],   # evidence_id -> 命中的 span_id
              judge_status: Literal["ok", "error", "timeout", "invalid", "oversize",
                                    "uncalibrated", "skipped", "not_run"],
              diagnosis_pass: bool | None,
              evidence_status: Literal["complete", "incomplete"],
              deterministic_pass: bool, judge_pass: bool, overall: bool,
              failure_class: FailureClass | None, failure_reasons: tuple[str, ...])

FailureClass = Literal["setup", "replay_miss", "execution", "deterministic_gate",
                       "judge_error", "diagnosis", "evidence_incomplete"]

DimensionValue(kind: Literal["hash", "version", "none", "unknown"],
               value: str | None, reason: str | None)
Manifest(round_id: str, dimensions: Mapping[str, DimensionValue])
PlannedSlot(scenario_id: str, scenario_version: str, mode: Mode, repetition: int)
CalibrationResult(passed: bool, items: int, comparisons: int, agreement: int,
                  negatives: int, false_passes: int, reasons: tuple[str, ...])
AggregateRow(scenario_id: str, scenario_version: str, mode: Mode, planned: int,
             attempted: int, not_started: int, passed: int, failed: int,
             pass_rate: float | None, replay_miss: int, replay_miss_rate: float | None,
             failure_classes: Mapping[str, int])
Aggregate(round_id: str, manifest_hash: str, rows: tuple[AggregateRow, ...],
          baseline_complete: bool, incomplete_reasons: tuple[str, ...])
```

异常：`ScenarioError`（场景文件不合法）、`ManifestError`、`AggregateError`（拒绝汇总）、`CalibrationError`（输入不合法）。

## 函数与语义

### `load_scenario(directory: Path) -> tuple[ScenarioSpec, Answer]`

读一个场景目录。以下任一情况抛 `ScenarioError`：两个文件的 scenario_id/version 不一致；fault 场景缺 `injected` 或 `signature`；normal 场景带了 `injected` 或 `signature`；`signature.any_of` 为空或 `min_matches < 1`；`expected_output` 为空；`scenario.json` 出现白名单（question、subject_id、target_id 与 IncidentScenario 字段）以外的被测可见字段。`load_scenarios(root: Path) -> tuple[tuple[ScenarioSpec, Answer], ...]` 按 scenario_id 排序读全部子目录。

### `investigator_input(spec: ScenarioSpec) -> Mapping[str, str]`

返回被测模型可见的全部输入，键恰为 `question`、`subject_id`、`target_id`。不接受 `Answer` 参数；不含 scenario_id（场景 ID 可能暗示答案）。改动 answer.json 不改变它的返回值（P11）。

### `canonical_view_hash(view: Mapping[str, Any]) -> str`

与产品写入 `view_sha256` 的算法相同（`opspilot.tools.registry.canonical_hash`）。

### `signature_matches(rule: SignatureRule, record: DeliveredRecord) -> tuple[str, ...]`

返回该记录中满足规则的 span 的 `span_id`（P8）。一行 span 命中须同时满足：`record.view["tool"] == "traces_search"`；`(service, operation)` 与 `any_of` 某项完全相等；`operation_truncated` 为假；按展示的 `status_tags` 复算为错误（`error` 为 True，或 `otel.status_code == "ERROR"`，或 `rpc.grpc.status_code` 存在且不为 0，或 `http.status_code` 以 5 开头），与行上的 `error_by_visible_tags` 无关；`start_us` 落在 `record.window` 内（含端点；window 为 None 则不命中；`start_us` 超出平台可表示的时间范围时不命中，不抛错）。只匹配方法后缀、只匹配服务、或任意 checkout 错误都不命中。命中数少于 `min_matches` 时返回空元组。

### `run_gate(spec, answer, outcome, report, delivered, authorized) -> GateResult`

参数：`outcome: IncidentOutcome | None`，`report: ReportCapture | None`，`delivered: Mapping[str, DeliveredRecord]`，`authorized: Mapping[str, Window]`（time_scope_ref → 授权时间框）。`checks` 的键固定为下列名称，`passed` 为全部为真。失败时 `reasons` 含括号里的码：

1. `execution`：outcome 非 None，`final_state == "completed"`，`report_available` 为真，`decision == "report_available"`，`human_interaction is None`，`handoff_reasons` 为空（`EXECUTION_NOT_COMPLETED`）。
2. `report`：用产品的 `parse_report(raw, finish_reason=...)` 解析成功（`REPORT_INVALID`），且 `assessment_status == "completed"`（`REPORT_INCOMPLETE`）。`conclusion` 可以是 supported、partial 或 inconclusive（P1）。
3. `permissions`：含 `read_only`，且全部属于 `{"read_only", "human_control"}`（`PERMISSION_VIOLATION`，P3）。
4. `citations`：报告每条 claim 的每个 evidence_id 都在 `delivered` 中且 `delivered` 为真（`CITATION_NOT_DELIVERED`），映射键等于记录自身的 `evidence_id`（`CITATION_ID_MISMATCH`），并属于本 Run 的 `outcome.evidence_ids`（`CITATION_NOT_IN_RUN`；outcome 为 None 时任何引用都不属于本 Run）；记录的 `view_sha256` 等于 `canonical_view_hash(view)`（`VIEW_HASH_MISMATCH`）；kind 为 fact、counter_evidence、rejected_hypothesis 的 claim 引用的记录须 `status == "ok"` 且 `citable_as_fact`（`CITATION_NOT_FACT_EVIDENCE`），其 `target_refs` 非空且全部等于所引记录的 `target_id`，且该 `target_id` 等于场景的 `stimulus.target_id`（`TARGET_MISMATCH`），其 `time_scope_ref` 在所引记录的 `time_scope_refs` 中且在 `authorized` 中（`TIME_SCOPE_MISMATCH`），所引记录的 `window` 落在该授权框内（`WINDOW_OUTSIDE_AUTHORIZED`）。
5. `signature`：仅 fault 场景，至少一条记录有 `signature_matches` 命中（`SIGNATURE_ABSENT`）。只计入这样的记录：属于本 Run、目标为场景 target、映射键与 `evidence_id` 一致，且至少有一条引用它的 claim（不论 kind）按第 4 项对事实型 claim 的全部检查合格（目标、hash、时间引用与授权窗口）。`RunEvaluation.signature_matches` 同样只含这些记录。normal 场景此项恒为真。

report 解析失败时 `citations`、`signature` 记为假（原因只记 `REPORT_INVALID`）。

### `evaluate(spec, answer, attempt, outcome, report, delivered, authorized, traces, judge) -> RunEvaluation`

`attempt.started` 为假时抛 `ValueError`（未开始的槽位不评测，只在汇总中计 not_started）。`attempt` 或 `answer` 的 (scenario_id, version) 与 `spec` 不一致，或 `outcome` 非 None 而其 `scenario_id` 与 `spec.scenario_id` 不一致时，抛 `ValueError("SCENARIO_MISMATCH")`。`traces: Sequence[TraceEntry]`，`judge: JudgeResult | None`。

- 失败原因（全部保留在 `failure_reasons`）：`attempt.status != "ran"` → `SETUP_FAILED`（setup_failed）或 `ATTEMPT_ABORTED`/`ATTEMPT_UNKNOWN`，类 `setup`；`replay_miss` → `REPLAY_MISS`，类 `replay_miss`；gate 的 `execution` 失败，类 `execution`；gate 其余失败，类 `deterministic_gate`；judge 问题，类 `judge_error`；诊断不通过 → `DIAGNOSIS_FAILED`，类 `diagnosis`；trace 不完整 → `EVIDENCE_INCOMPLETE`，类 `evidence_incomplete`。
- `failure_class` 取优先级最高的一类：setup → replay_miss → execution → deterministic_gate → judge_error → diagnosis → evidence_incomplete。通过时为 None。
- `evidence_status`：每个 `required` 的 TraceEntry 都 `read_back == "found"`、`run_id == expected_run_id`（均非 None）且 `product_run_id == attempt.product_run_id` 才是 complete，否则 incomplete（P2）。没有任何 required 条目视为 incomplete。`attempt.status == "ran"` 而 `product_run_id` 为 None 时恒为 incomplete；setup 失败没有产品 Run 时，评测 attempt 的根 trace 以 `product_run_id=None` 绑定。
- `deterministic_pass` = 无 setup/replay_miss，gate 通过，且 evidence complete（P2）。
- judge 处理（P6）：gate 未通过时 judge 可以为 None，`judge_status = "skipped"`；gate 通过而 judge 为 None → `not_run`（`JUDGE_NOT_RUN`）；`judge.status != "ok"` → 对应状态（`JUDGE_<STATUS>`）；`calibrated` 为假 → `uncalibrated`（`JUDGE_UNCALIBRATED`）。以上都是 judge_error，`judge_pass` 为假。
- judge 输出校验（不通过 → `judge_status = "invalid"`，原因码如下，类 judge_error）：`location` 须为 `summary`、`claims[i]`、`gaps[i]` 或 `next_steps[i]` 且下标存在（`JUDGE_BAD_LOCATION`）；`quote` 非空且（各自压缩连续空白后）是该位置文本的子串（`JUDGE_QUOTE_NOT_FOUND`）；位置为 `claims[i]` 时 `cited_evidence_ids ⊆ claims[i].evidence_ids`；其余位置时，`linked_claim` 为 None 则 `cited_evidence_ids` 须为空，非 None 则须是存在的 claim 下标且 `cited_evidence_ids ⊆` 该 claim 的 evidence_ids（`JUDGE_CITATION_NOT_IN_CLAIM`）；`verdict` 须等于下述重算结果（`JUDGE_VERDICT_MISMATCH`）。
- 诊断重算（r6、P1）：取 `factual_causal` 为真的诊断。normal：为空才通过。fault：非空，且每一条都满足：`service` 属于 `{injected.service} ∪ service_aliases`、`operation` 属于 `{injected.operation} ∪ operation_aliases`（两者大小写不敏感比较），`supported` 为真，且其 `cited_evidence_ids` 中至少一条记录有 `signature_matches` 命中。`diagnosis_pass` 记录重算结果（judge 无效或未运行时为 None）。
- 诊断的证据资格（独立审查 P1）：每条 `factual_causal` 诊断引用的每条记录，都要按其锚定 claim（位置为 `claims[i]` 时是该 claim，否则是 `linked_claim`；两者都没有则不合格）的 `target_refs` 与 `time_scope_ref`，满足 `run_gate` 第 4 项对事实型 claim 的全部检查（已交付且属于本 Run、hash、`status == "ok"` 且 `citable_as_fact`、目标非空且一致、时间引用与授权窗口）。报告给该 claim 的 kind 不影响此要求。不满足时 `diagnosis_pass` 为假，原因 `DIAGNOSIS_EVIDENCE_UNQUALIFIED`，类 diagnosis；judge 的 `verdict` 仍只与上一条的重算结果比较。
- `judge_pass` = judge_status 为 ok 且 `diagnosis_pass` 为真。`overall = deterministic_pass and judge_pass`。

### `judge_request(spec, answer, report, delivered, authorized, outcome=None) -> Mapping[str, Any]`

组装给 judge 的输入（P6），不发请求。键：`question`（stimulus.question）、`subject_id`、`authorized_windows`（ref → `{start, end}` ISO 字符串）、`report_raw`、`cited_evidence`（报告引用、已交付、目标为场景 target 且属于本 Run 的记录：`evidence_id`、`view_sha256`、`view`；`outcome` 为 None 时以 `delivered` 的键代替本 Run 的证据 ID）、`expected_output`、`scenario_kind`、`injected`（fault 为 service/operation 及别名，normal 为 None）、`signature_matches`（evidence_id → span_id 列表）。不含未被引用的记录、`flag`/`variant`、scenario_id。

### 冻结清单与 manifest（P4）

`REQUIRED_DIMENSIONS`（元组，顺序固定）：`code`、`upstream`、`scenarios`、`answers`、`evidence_rules`、`model_request`、`prompt`、`tools_schema`、`projection`、`registry`、`profile`、`adapter`、`evaluator`、`judge_config`、`calibration_pack`、`permissions`、`limits`、`kind_environment`、`subject_initial_state`、`session`、`human_feedback`、`workload`、`knowledge_snapshot`、`replay_fixture`、`n`、`seed`、`command`、`clock_policy`。

- `manifest_unknowns(manifest) -> tuple[str, ...]`：缺失或 kind 为 unknown 的维度名，按 `REQUIRED_DIMENSIONS` 顺序。
- `validate_manifest(manifest)`：kind 为 hash/version 而 value 为空、kind 为 none 而 reason 为空、出现清单外维度名时抛 `ManifestError`。unknown 不抛错。
- `manifest_hash(manifest) -> str`：对 `round_id` 与全部维度的规范 JSON 取 sha256（与 `canonical_view_hash` 同算法）；维度顺序不影响结果。

### `calibrate(labels, verdicts, *, min_items=24, runs_per_item=2, min_agreement=46, min_negatives=12) -> CalibrationResult`（P7）

`labels: Mapping[str, bool]`（冻结验证集的最终标签，True = 该报告应通过），`verdicts: Sequence[tuple[str, bool]]`（judge 对每个 item 的判定，每个 item 恰 `runs_per_item` 次）。verdicts 中出现无标签的 item、或某个有标签 item 的判定次数不等于 `runs_per_item`，抛 `CalibrationError`。`comparisons = items × runs_per_item`；`agreement` 为判定等于标签的次数；`negatives` 为标签 False 的 item 数；`false_passes` 为标签 False 而判定 True 的次数。`passed` 当且仅当 `items >= min_items`、`agreement >= min_agreement`、`negatives >= min_negatives`、`false_passes == 0`；否则 `reasons` 依次含 `TOO_FEW_ITEMS`、`AGREEMENT_BELOW_THRESHOLD`、`TOO_FEW_NEGATIVES`、`FALSE_PASS` 中适用者。

### `aggregate(manifest, planned, evaluations, calibration) -> Aggregate`（P10、P12）

`planned: Sequence[PlannedSlot]`，`evaluations: Sequence[RunEvaluation]`，`calibration: CalibrationResult | None`。

- 先调用 `validate_manifest(manifest)`，不合法时 `ManifestError` 直接抛出。
- 拒绝（抛 `AggregateError`）：planned 有重复槽位；某个 evaluation 的 `round_id` 不等于 manifest 的 round_id，或 `manifest_hash` 不等于 `manifest_hash(manifest)`；`attempt_id` 重复；evaluation 不对应任何 planned 槽位；同一槽位多于一个 evaluation（重跑属于新轮次）。
- 每个 `(scenario_id, scenario_version, mode)` 一行，按这三项排序：`planned` 为该组槽位数；`attempted` 为 evaluation 数；`not_started = planned - attempted`；`passed` 为 `overall` 为真的数；`failed = attempted - passed`；`pass_rate = passed / attempted`，attempted 为 0 时为 None；`mode == "structural"` 时 `pass_rate` 恒为 None（结构重放不产出质量率）；`replay_miss` 为该组 `attempt.replay_miss` 为真的数，`replay_miss_rate` 仅 replay 模式且 attempted > 0 时为 `replay_miss / attempted`，否则 None；`failure_classes` 为失败 evaluation 的 `failure_class` 计数。
- `baseline_complete` 当且仅当：全部槽位都已 attempted（`SLOTS_NOT_STARTED`），所有 evaluation 的 evidence_status 为 complete（`EVIDENCE_INCOMPLETE`），`manifest_unknowns` 为空（`MANIFEST_UNKNOWN`），calibration 非 None 且 passed（`JUDGE_NOT_CALIBRATED`），且至少有一个非 structural 槽位（`NO_QUALITY_SLOTS`）。`incomplete_reasons` 按上述顺序列出适用者。成功率低于 100% 不影响 baseline_complete（P10）。

## 测试作者提出的歧义与处置（2026-10-13）

独立验收作者（Codex，全新上下文）列出 9 项；均为可逆细节或后续 PR 的接口，不改合同：

1. 场景 JSON 嵌套格式：以 `evals/scenarios/` 中提交的 6 个文件为准（`scenario.json` 顶层 `scenario_id`、`version`、`kind`、`incident`、`stimulus`；`answer.json` 顶层 `scenario_id`、`version`、`expected_output`、`injected`、`signature`）。
2. normal 场景 `signature` 恒真与「报告解析失败时 `citations`、`signature` 记为假」冲突：解析失败优先，normal 也记假。
3. 「同义 claim 关联」：是否同义由 judge 判定并通过 `linked_claim` 给出；代码只核对下标存在与引用子集，不实现同义算法。
4. judge 原始响应的解析（非法 JSON → `status="invalid"`）：属于接入 judge 调用的 PR，本 PR 只定义 `JudgeResult`。
5. `judge_request` 的 `cited_evidence`：列表，按报告中首次引用顺序。
6. 旧摘要读取与 `trace.entries[]` 迁移（P13）：属于 PR-B 的 summary 写入/读取，本 PR 只定义 `TraceEntry`。
7. 场景版本兼容：不定义兼容关系；不同版本是不同的计划槽位，分行统计。
8. 「已开始但缺 summary」：由 PR-B/C 的执行器在 setup 前登记 attempt，缺结果时写 `status="unknown"` 的 evaluation；`aggregate` 只认 evaluation。
9. 结构重放的零模型、零凭据：属于 PR-D 的驱动层验收；本 PR 只保证 structural 不产出质量率。

## 独立审查后的修订（2026-10-13）

独立审查（Codex，全新上下文）判 fail，四项均已复现并修复：P1 judge 认定为事实型因果诊断的语句须满足事实证据资格（不看报告自填的 kind）；P2 payment 规则补入 payment 服务自身的 server span `grpc.oteldemo.PaymentService/Charge`（fault-1 证据中有 31 条错误 span）；P2 evaluate 核对场景身份（复验补上 Outcome 的 scenario_id），trace 绑定到 attempt 的产品 Run；P2 aggregate 先校验 manifest。

PR #221 机器人审查第三轮（三项，均采纳）：映射键须等于记录 `evidence_id`；签名只来自按事实规则合格的引用（不因 claim 是 hypothesis 而跳过授权窗口）；`Window` 须为 UTC 时区感知时间。

PR #221 机器人审查（三项，均采纳）：引用须属于本 Run 的 `outcome.evidence_ids`（`CITATION_NOT_IN_RUN`）；记录目标须为场景 target；`start_us` 越界不命中而非抛错；第二轮：`Window` 反向区间构造即拒绝。

## 不做

不翻任何 `passes`；不新增产品运行时行为；不改 `ReportV2`、`IncidentOutcome` 或产品投影；不读写数据库。
