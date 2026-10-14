# 场景评测 PR-B1 公开接口（合同 r7，B5/B8）

- 依据：[PR-B 预审第 4 节](2026-10-14-eval-pr-b-contract.md)、[PR-A 接口](2026-10-13-eval-pr-a-interface.md)、P10/P12/P13。独立测试作者只按本文与合同写测试，不看实现。
- 范围：离线持久化边界、固定槽位、校准重算、基线表与 #222；不做录制、回放、judge transport 或 P7 盲标。只改当前 worktree，提交由 lead 负责。

## 模块、类型与签名

新增 `opspilot.evaluation.storage` 和 `opspilot.evaluation.baseline`，以下均从 `opspilot.evaluation` 导出。所有新 DTO 为 frozen dataclass，PR-A DTO 原样复用。

```python
ArtifactRef(path: str, sha256: str, bytes: int, media_type: str,
            visibility: Literal["private", "public"])
TraceRecord(entry: TraceEntry, attempt_id: str, evaluation_id: str,
            project: str | None, url: str | None, raw_read_back: str | None,
            checked_at: datetime | None)
EvaluationCapture(manifest: Manifest, spec: ScenarioSpec, answer: Answer,
                  attempt: AttemptRecord, outcome: IncidentOutcome | None,
                  report: ReportCapture | None, delivered: Mapping[str, DeliveredRecord],
                  authorized: Mapping[str, Window], traces: tuple[TraceRecord, ...],
                  judge: JudgeResult | None,
                  delivery_refs: Mapping[str, ArtifactRef],
                  recording_ref: ArtifactRef | None = None,
                  ledger_ref: ArtifactRef | None = None,
                  source_run_id: str | None = None, replay_run_id: str | None = None,
                  started_at: datetime | None = None, ended_at: datetime | None = None,
                  usage: Mapping[str, Any] | None = None,
                  cost: Mapping[str, Any] | None = None)
AttemptJournal(attempt: AttemptRecord, spec: ScenarioSpec, answer: Answer,
               started_at: datetime | None = None)
CalibrationCapture(labels: Mapping[str, bool], verdicts: tuple[tuple[str, bool], ...],
                   judge_config_hash: str, calibration_pack_hash: str)
RoundCapture(manifest: Manifest, planned: tuple[PlannedSlot, ...],
             journals: tuple[ArtifactRef, ...], summaries: Mapping[str, ArtifactRef],
             calibration_ref: ArtifactRef | None = None)
LegacySummary(payload: Mapping[str, Any], sha256: str,
              trace_entries: tuple[Mapping[str, Any], ...], verified: bool = False)
Resolver = Callable[[ArtifactRef], bytes]

write_summary(path: Path, capture: EvaluationCapture) -> ArtifactRef
load_summary(path: Path, *, manifest: Manifest, resolve: Resolver) -> RunEvaluation
write_attempt(path: Path, journal: AttemptJournal) -> ArtifactRef
load_attempt(path: Path) -> AttemptJournal
write_calibration(path: Path, capture: CalibrationCapture) -> ArtifactRef
load_calibration(path: Path, *, expected_pack_hash: str,
                 expected_judge_hash: str) -> CalibrationResult
write_round(path: Path, capture: RoundCapture, *, resolve: Resolver) -> ArtifactRef
load_round(path: Path, *, resolve: Resolver) -> Aggregate
read_legacy_summary(path: Path) -> LegacySummary
write_baseline(round_path: Path, *, resolve: Resolver, destination: Path) -> ArtifactRef
```

`path` 是本地输出/输入路径；writer 返回的 ref.path 为文件名，调用方负责包内唯一命名及 resolver 的包根。resolver 接受 ref 返回原始字节，不访问网络。新 reader 会自行核 bytes/SHA，不信 resolver。文件缺失/读错 → EVIDENCE_UNAVAILABLE。函数不写入被读文件。

## 文件格式（所有列出的字段必填，包括 nullable 字段）

JSON 顶层如下，未知 schema 或字段拒绝；writer 写出的 null 和默认字段也不得省略。

- summary：`{"schema":"opspilot-eval-summary-1","capture":<EvaluationCapture>,"evaluation":<RunEvaluation>,"report_parsed":<解析报告或null>,"report_sha256":<报告raw的UTF-8字节SHA或null>,"trace":{"entries":<TraceRecord数组>}}`。capture 中保存完整原始评测输入；顶层 trace.entries 是 capture.traces 的完整固定投影，loader 严格核对其结构与一致性；evaluation 是所有派生项（包含 gate 分项、signature_matches、judge/diagnosis/evidence/deterministic/overall/failure 及身份）。`report_parsed` 用 PR-A 的产品 ReportV2 解析结果（model_dump(mode="json")），解析失败为 null。
- attempt journal：`{"schema":"opspilot-eval-attempt-1","journal":<AttemptJournal>}`。执行器必须在 setup 前独占写入 started=True 的 journal。尚未启动也可冻结 started=False journal；不在原文件修改状态，后续结果另写 summary。
- calibration：`{"schema":"opspilot-eval-calibration-1","capture":<CalibrationCapture>,"result":<CalibrationResult>}`。标签是最终冻结标签，verdicts 是每项两次判定的 `[item_id,bool]` 数组。pack hash 绑定冻结验证包，judge hash 绑定配置；B1 不证明盲标/来源分布。
- round：`{"schema":"opspilot-eval-aggregate-1","capture":<RoundCapture>,"aggregate":<Aggregate>}`。journal refs 与 summary refs 指向上述文件；summary 映射键是 attempt_id。calibration ref 可为 null。缓存 aggregate 必须与重算逐项相等。
- 每个 delivery ref 的原始 JSON **直接是 DeliveredRecord DTO 对象**，没有 envelope；该 ref 必须与 capture.delivered 同键且对象内容完全一致，view_sha256 必须复算正确。recording/ledger ref 非 null 时也校验其字节/hash（不解读录制格式）。无实际交付时两映射都为空。

DTO 序列化：字段名严格等于 dataclass 的全部字段（包括 PR-A 默认字段）；嵌套递归展开，tuple/frozenset 是数组（frozenset 排序、不得重复），Mapping 是字符串键对象；Literal 仅接受列出的字符串；bool 必须是 JSON true/false（拒绝 0/1/"false"）；int 不接受 bool；float 只接受有限 JSON 数；datetime 是 UTC ISO8601 字符串，reader 接受 Z 或 +00:00，writer 用 +00:00。ScenarioSpec.incident 包含完整 IncidentScenario（也含 scenario_id），不同于场景目录的简写格式。PR-A 场景限制仍成立：fault 必须有 injected/signature，normal 两者为 null，expected_output 非空，signature.any_of 非空且 min_matches≥1。

`view`、usage、cost 为不限定业务键的 JSON 数据对象，所有层仍拒绝重复键/非有限数/非JSON类型；usage/cost 缺测为 null，不能默认为零。报告 raw 是字符串，报告 schema 错误作为 gate 失败保存，不把失败 Run 拒绝掉。

canonical JSON：`json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")`，无换行，与产品 canonical 算法在有限 JSON 上一致。ArtifactRef.sha256 为**实际文件字节** SHA256 的 64 位小写 hex；bytes 为非负整数；media_type 非空。manifest_hash 使用 PR-A `manifest_hash()`（不改 PR-A hash/version 字符串的既有语义）；view hash 用 `canonical_view_hash()`；report hash 按 raw 字节。refs.path 必须是非空 POSIX 包内相对路径，拒绝绝对路径、反斜线、空段、`.`、`..`；reader 拒绝本地路径的任一符号链接及非普通文件。resolver 必须同样拒绝包内符号链接和越界，B1 无法从 bytes callback 判定它访问了哪个 inode。

## 读回、身份与失败

- summary 从保存的原始输入调用 PR-A `evaluate()`。保存的 evaluation/report_parsed/report_sha256 逐项核对；缓存不能决定通过。attempt round_id、manifest hash、capture.manifest 必须绑定传入 manifest；保存evaluation内的attempt身份及judge hashes也须与原始输入相等，缓存aggregate的round_id/manifest_hash须相等；spec/answer/attempt/outcome 的场景版本与身份必须一致。trace.entry 属于来源角色（role 为 source/source_*）时不得 required；TraceRecord.attempt_id/evaluation_id 必须等于当前 attempt_id，entry 的产品 Run 不一致仍沿 PR-A 判 evidence_incomplete。非 null replay_run_id 必须等于 attempt.product_run_id。judge 的两 hash 必须与 manifest 的 hash 维度绑定（judge 为 null 无此要求）。
- load_calibration 固定调用 `calibrate()` 默认 P7 参数（24项、每项2次、46一致、12负例、0误通过）；与保存 result 全字段比较，再核 expected hashes，不信 passed。少样本可读回为失败结果；判定次数不对则结构错误。错误判定不得用非 bool 表示。
- load_round 逐一读 journal/summary/calibration，调用 `aggregate()`，比较保存 aggregate。journal 不得重复 attempt_id/槽位、不计划的槽位拒绝；summary 必须有相同 started=True journal，冻结 spec/answer、身份/槽位相同（status/replay_miss/product_run_id 可在执行后改变；journal 已有 product_run_id 则必须一致）。started=True 缺 summary 时，用 journal spec/answer 和 `replace(attempt,status="unknown")`，其余输入 None/空集合调用 evaluate，失败计分母；started=False 且无 summary 或完全没有 journal 的计划槽位 → not_started。present summary ref 读不到不能当缺 summary吞掉，必须 EVIDENCE_UNAVAILABLE。
- `aggregate()` 补防御：started 不是真正 True → AggregateError("ATTEMPT_NOT_STARTED")；校准计数须为非负非bool整数、comparisons==items*2、agreement≤comparisons、negatives≤items、false_passes≤2*negatives；按固定P7门槛的 passed/reasons 须自洽，否则 AggregateError("CALIBRATION_INCONSISTENT")。保留 PR-A 其余签名及语义。
- 旧无 schema summary 仅由 read_legacy_summary 展示：原 payload 和原字节 hash 不改；trace.entries 数组原样，旧 trace 单对象转换成一个展示项，缺 trace 为空。verified 恒 false。load_summary 对无 schema 旧件报 EVIDENCE_UNAVAILABLE；未知显式 schema 报 SCHEMA_INVALID，绝不自动升级。

StorageError(ValueError) 的 `.code` 与 str 都为以下固定码，错误正文/异常链不携带原始数据、路径、供应商异常或凭据：

| 码 | 条件 |
|---|---|
| SCHEMA_INVALID | 重复JSON键、非法JSON/版本/字段/类型/有限性、DTO约束、ref路径/hash格式、槽位重复或未计划、原始校准输入的判定次数不合法（直入aggregate的校准DTO错误另见AggregateError） |
| ATTEMPT_NOT_STARTED | summary attempt.started=False，或未开始journal却提供summary |
| EVIDENCE_UNAVAILABLE | 本地文件/外置ref不可读、resolver异常、旧件不足以重算 |
| HASH_MISMATCH | ref实际字节长度/SHA、delivery view hash、report raw hash或attempt manifest hash不符 |
| IDENTITY_MISMATCH | 场景/版本/轮次/manifest对象/attempt/产品Run/trace绑定错或delivery映射键与evidence_id错 |
| JUDGE_IDENTITY_MISMATCH | judge/calibration两hash与期望冻结身份不一致或维度非hash |
| DERIVED_RESULT_MISMATCH | 保存的evaluation、报告解析、calibration result、aggregate与纯函数重算不一致 |
| OUTPUT_EXISTS | writer目标已存在（含符号链接），并发仅一个成功 |

每个 writer 先验证全部数据、在目标同目录私有临时普通文件写完并 fsync，再 hard-link 独占发布、fsync目录、删除自身临时文件。读者只见完整文件；不覆盖既有目标，不用 replace。非重复的写IO错误报 EVIDENCE_UNAVAILABLE（发布后目录fsync失败可能已留下完整文件，重试仍OUTPUT_EXISTS）；多文件调用没有事务承诺。

## 基线表与调整理由

write_baseline **重新 load_round**，生成 UTF-8 Markdown 表并独占原子写入：每scenario/version/mode列 planned、attempted、not_started、passed/attempted、failed、质量率、replay_miss/attempted、miss率、失败分类；零分母率为 `null`，structural质量率为 `null`，miss率只为replay列出。replay_miss沿PR-A计失败分母；注明「D5：回放5次、live2次；样本量有限，置信不足」，以及baseline_complete与incomplete_reasons，不把低成功率当基线未完成。

最小调整：增加 write_attempt/write_calibration/write_round 与 DTO envelope，理由是预审只有reader签名，测试/驱动需要可复现的冻结写入口；load_calibration直接返回重算 CalibrationResult，B3再引入盲标认证类型。summary把P12身份统一放capture.attempt，理由是避免重复身份产生两套权威；trace.entries 保持P13字段路径，capture.traces供evaluate原始输入使用，两者逐项比较；旧trace转换仅用于展示。原始交付存ref并保存对照DTO，理由是缺外置证据时明确fail-closed；writer不自动创建包/录制文件。B1不证明P7盲标、录制完整性或真实生产观察。

PR-A 兼容处置（实际测试揭示）：失败 CalibrationResult 的 agreement 与 false_passes 联合关系不另加 aggregate 拒绝门，理由是既有验收用48一致/1误通过表示失败；保留失败与阻止基线的语义。仍检查各计数范围、comparisons及P7 passed/reasons；新持久化件完整重算，不接受矛盾计数。
