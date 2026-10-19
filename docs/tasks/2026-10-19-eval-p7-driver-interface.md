# 评测 P7 执行驱动公开接口（chore，r2）

- 状态：接口草案，待 lead 审定「待裁决」节后交实现与验收测试作者。
- 依据：[P7 任务记录](2026-10-18-eval-p7-calibration.md) 合同 r1（P7-D1..D14，已裁决）；[PR-B3 接口](2026-10-14-eval-pr-b3-interface.md)（r2–r11）；[PR-A 接口](2026-10-13-eval-pr-a-interface.md)。代码基线 main `57dd8eb`。
- 用途：验收测试作者只凭本文与上述合同写测试，不看实现。实现者不得为通过测试改本文语义；确需改动按合同变更处理。
- 范围外（P7-D13 原文）：判分、P6 复核、匹配口径（P7-D9 严格精确相等）、D16 选择认证与数据出口；调查 loop、报告生成、恢复路径；任何 `passes`。本驱动只**调用** `judge_once`、`freeze_label_pack`、`run_calibration`、`load_label_pack`、`load_verified_calibration`，不改它们的语义。

## 0. 模块、调用形式与通用约定

| 项 | 约定 | 理由（可逆默认） |
|---|---|---|
| 模块 | `scripts/eval_p7.py`：CLI 与全部公开函数、DTO。不进 `opspilot/` 产品包 | 与 `scripts/eval_live.py`、`scripts/eval_structural_ci.py` 同为工程脚本；产品代码不新增评测执行入口 |
| 复用的私有函数 | 在原模块加**纯别名**公开名，不改语义：`opspilot.evaluation.storage.load_summary_capture(path, *, manifest, resolve) -> tuple[RunEvaluation, EvaluationCapture]`（= 现 `_load_summary(_read(path), …)`）；`opspilot.evaluation.calibration_flow.check_blind_label(item, label)`（= `_check_label`）、`is_candidate_model_identity(s) -> bool`（= `_candidate_annotator`）、`check_identifiers(value)`（= `_metadata_identifiers`） | 驱动必须复用 storage 解析/身份/私有引用核验与 r4/r9 标签、标识规则，不另写一套；别名比跨模块调私有名稳定 |
| CLI | `.venv/bin/python -m scripts.eval_p7 <子命令> --workspace W [选项]`；`W`、`--session`、`--run` 须 fullmatch `[a-z0-9][a-z0-9.-]{0,62}`（同 `eval_live.ROUND_PATTERN`）；`--round` 另须以 `eval-p7-` 开头；`--attempt` 须为小写 UUID；`--item`、`--provider`、`--slot` 只接受已定义值。任一不符 → 2/`USAGE`，在拼接任何路径之前检查 | 沿用现有 round 名规则；ID 不含 `/`，不能借路径片段跳出工作区 |
| stdout | 每次调用恰好打印一个 JSON 对象（`sort_keys=True`），至少含 `command`、`status`、`exit_code`、`reason`；只含 ID、hash、计数、错误码，**不含**报告正文、标签、裁决文本、凭据 | 同 `eval_live._final`；终端输出可能被转贴，私有内容只留在文件 |
| 错误 | 库函数只抛 `P7Error(code: str)`（`ValueError` 子类），`__cause__`/`__context__` 均为 None，消息只含固定码。下层 `StorageError`/`JudgeError` 的 `code` **原样**作为 `P7Error.code` | 同 B3 r2 第 6 项；测试只断言码 |
| 凭据 | 仅 debug/formal 需要：`--judge-env PATH`（绝对路径），每家调用 `load_judge_config(PATH, environ=os.environ, provider=p)`；驱动不读 `.env`，不打印、不落盘 `api_key` | B3 唯一凭据入口；`environ` 只做冲突检查 |
| 传输 | 真实运行用 `HTTPJudgeTransport()`；库函数参数 `transports: Mapping[str, JudgeTransport]` 供测试注入假 transport | 与 `run_calibration` 一致 |
| 时间 | 私有记录不写时间戳（成本记录除外，见 §7） | 记录字节可复现，测试可做字节比较 |

### 0.1 退出码（所有子命令共用）

| 码 | 含义 | `status` |
|---|---|---|
| 0 | 成功完成（debug：所有计划调用均已发出并落盘，judge 自身的 error/invalid 是数据不是失败） | `ok` / `selected` |
| 1 | formal 已跑完但结果为 `no_baseline`，或读回核验得到非预期码（`reason=READBACK_FAILED`，另带 `readback_code`） | `no_baseline` / `failed` |
| 2 | 用法错误（参数缺失/冲突/格式） | `usage` |
| 3 | 拒绝：前置校验失败，**未发出任何 judge 调用、未写任何文件**（或只读命令失败） | `refused` |
| 5 | 已开始写入或调用后写盘失败（`ARTIFACT_UNWRITABLE`、中途 `OUTPUT_EXISTS`、`EVIDENCE_WRITE_FAILED`）；可能留下不完整工件，不得视为完成 | `stopped` |
| 6 | formal 结果 `pending_user_decision`（P7-D14）：停在 pending，交用户 | `pending_user_decision` |
| 130 | 中断（SIGINT/KeyboardInterrupt）；已落盘文件保留，不回滚 | `interrupted` |

3 与 5 的分界：预检全部通过前的失败一律 3；第一笔写入或第一次 judge 调用之后的失败为 5（中断为 130）。理由：沿用 `eval_live` 的 3=未触碰、5=证据写失败。

## 1. 私有工作区与 ignore 核验（任务第 8 项）

### 1.1 布局

```
<repo>/tmp/eval-p7/<W>/                      0700
  workspace.json                             {schema:"opspilot-p7-workspace-1", workspace:W}
  items/<item_id>.json                       ItemRecord（原件与派生件同目录）
  assembled/plan.json                        用户/lead 提交的 SplitPlan 原样副本
  assembled/items.json                       {"items":[CalibrationItem…]}，codec.canonical_bytes
  blind/packet.json                          BlindPacket 规范字节（唯一权威）
  blind/annotator-1/packet.json              与 blind/packet.json 逐字节相同
  blind/annotator-2/packet.json              同上
  annotations/annotations-1.json             codec.canonical_bytes(AnnotationSet)（B3 r2 第 2 项）
  annotations/annotations-1.provenance.json  AnnotationProvenance
  annotations/annotations-2.json / .provenance.json
  adjudication/brief.json                    AdjudicationBrief
  adjudication/distribution-acceptance.json  用户书面接受分布偏差（仅 §9.1 需要时，由用户写入）
  adjudication/decisions.json                用户裁决原件副本
  adjudication/adjudications.json            {"adjudications":[Adjudication…]}
  debug/<session>/<provider>-NNN.json        DebugRecord，NNN 从 001 起
  label-pack/                                freeze_label_pack 输出（5 文件）
  calibration/<run>/                         run_calibration destination（label-pack/、provider-001..060.json×3、calibration.json）
  costs/<session>/<provider>-before.json / <provider>-after.json   BalanceRecord
<repo>/tmp/eval-p7/exposure.lock             跨工作区暴露锁（init 首次创建，0600，不删）
<repo>/tmp/eval-p7/exposure.jsonl            跨工作区暴露登记（init 首次创建，只追加）
```

`<repo>` 为主工作树根（`git rev-parse --path-format=absolute --git-common-dir` 的父目录，同 lab ledger 根），从任何 worktree 运行都解析到同一处；`<private_root>` 固定为 `<repo>/tmp/eval-p7`，不提供覆盖参数。理由：`tmp/` 已被 `.gitignore:21` 忽略；集中一处便于清理与 worktree 删除前核查；暴露登记只有一个仓库级位置，换根或换 worktree 都绕不开（§9.2 第 7 步）。

### 1.2 写前核验（每个将要创建的文件与目录）

对每个目标路径 `p`（含尚不存在的路径；含 `run_calibration` 将创建的 `calibration/<run>/` 下全部 1+5+180+1 个路径，在第一次付费调用前一次性预检）：

1. `p` 解析后位于 `<repo>` 之内，且从 `<repo>` 到 `p` 的已存在组件无符号链接；否则 `PRIVATE_PATH_OUTSIDE_REPO`。工作区工件解析后还须严格位于 `<private_root>/<W>/` 之下，暴露锁与登记须恰为 `<private_root>/exposure.{lock,jsonl}`；否则 `PRIVATE_PATH_OUTSIDE_WORKSPACE`。
2. 运行 `git -C <repo> check-ignore -q -- <p>`：退出 0 → 通过；退出 1 → `PRIVATE_PATH_NOT_IGNORED`；其他退出码或无法执行 → `PRIVATE_PATH_UNVERIFIED`。不加 `--no-index`，因此已被跟踪的路径判为未忽略。目标是尚不存在的目录时，Git 对仅匹配目录的规则（如 `!dir/`）按文件判定，结果可能与目录存在后不同；此时允许在 `<private_root>` 内逐级创建空探测目录、判定后立即删除。空目录不被 Git 跟踪、不含内容，此探测不计为「写入」，不改变退出码 3「未写任何文件」与「登记是第一笔写入」的含义。
3. 文件以 `O_CREAT|O_EXCL|O_NOFOLLOW`、0600 独占创建，写后 flush+fsync、关闭、fsync 父目录；目录 0700 创建后 fsync 父目录（同 B3 r6）。目标已存在 → `OUTPUT_EXISTS`，从不覆盖；单文件 > 67108864 字节 → `ARTIFACT_UNWRITABLE`（同 r10）。

库函数通过 `Workspace.ignore_check: Callable[[Path], bool | None]` 注入 2 的实现（`None`=无法判定）；CLI 用真实 git。理由：预审已指出 0600 不等于不会被提交，必须由 git 规则本身确认；按文件逐个检查，防止 `!` 反向规则把个别文件放出。

### 1.3 `init`

`init --workspace W` 创建工作区与 `workspace.json`；私有根下 `exposure.lock`、`exposure.jsonl` 不存在时一并创建（空文件，经 1.2），已存在则不动。已存在的工作区 → 3/`OUTPUT_EXISTS`。其他子命令在工作区或 `workspace.json` 缺失/不合 schema 时 → 3/`WORKSPACE_INVALID`。

## 2. DTO（全部 frozen dataclass，经 `codec.canonical_bytes` 落盘、`codec.decode` 严格读回：未知/缺失字段 → `SCHEMA_INVALID`）

```python
ItemKind = Literal["original", "controlled_rewrite"]
REWRITE_LABEL = "controlled-rewrite-of-real-run-evidence"   # P7-D1「基于真实 Run 证据的受控改写」

SourceProvenance(round_id: str, attempt_id: str, product_run_id: str,
                 summary_sha256: str, recording_sha256: str, manifest_hash: str,
                 source_report_sha256: str)               # sha256(report.raw 的 UTF-8)，同 storage._report_fields

RewriteRecord(label: str,                                 # 恒为 REWRITE_LABEL
              source_item_id: str, source_report_sha256: str, derived_report_sha256: str,
              change_description: str, purpose: str,     # 非空（strip 后）
              author_identity: str, author_model_identity: str)   # r9 标识规则

ItemRecord(item_id: str, kind: ItemKind, scenario_id: str, source_family: str,
           input_hash: str, report_semantic_sha256: str,
           request: JudgeInput, provenance: SourceProvenance,     # 派生件沿用源件 provenance
           coverage: tuple[str, ...], coverage_note: str | None,
           rewrite: RewriteRecord | None)                 # original 必为 None，controlled_rewrite 必非 None

SplitPlan(families: Mapping[str, Literal["debug", "validation"]], items: tuple[str, ...])

BlindPacketItem(item_id: str, input_hash: str, p6_input: Mapping[str, Any])   # p6_input = safe_input(request)
BlindPacket(schema: str,                                  # "opspilot-p7-blind-packet-1"
            items_sha256: str,                            # assembled/items.json 原始字节 sha256
            judge_prompt: str, judge_prompt_sha256: str,  # 判据：opspilot.evaluation.judge.PROMPT 原文
            convention: str, convention_sha256: str,      # P7-D9 引文粒度与排序约定原文
            output_schema: Mapping[str, Any],             # BlindLabel/JudgedDiagnosis 字段说明（固定常量）
            items: tuple[BlindPacketItem, ...])           # 按 item_id 升序

BlindResponse(schema: str,                                # "opspilot-p7-blind-response-1"
              packet_sha256: str,
              labels: tuple[BlindLabel, ...])             # 标注者交回的文件

AnnotationProvenance(slot: Literal[1, 2], annotator_identity: str, model_identity: str,
                     packet_sha256: str, response_sha256: str, annotation_sha256: str,
                     evidence_note: str | None)           # 例如 Codex 会话 ID；程序不核实

AdjudicationBrief(schema: str,                            # "opspilot-p7-adjudication-brief-1"
                  items_sha256: str, packet_sha256: str,
                  annotation_hashes: tuple[str, str],     # (annotations-1, annotations-2) 文件字节 sha256
                  agreed: int,
                  differences: tuple[BriefEntry, ...])    # 按 item_id 升序
BriefEntry(item_id: str, input_hash: str, p6_input: Mapping[str, Any],
           label_a: BlindLabel, label_b: BlindLabel,      # A = annotations-1，B = annotations-2
           verdict_differs: bool,
           causal_keys_a: tuple[tuple[str, str], ...],    # factual_causal 诊断的 (location, quote)，排序去重
           causal_keys_b: tuple[tuple[str, str], ...])

Decision(item_id: str, choice: Literal["A", "B", "revised"],
         label: RevisedLabel | None, decision_record: str)
RevisedLabel(diagnoses: tuple[JudgedDiagnosis, ...], verdict: bool)
Decisions(schema: str, brief_sha256: str, decisions: tuple[Decision, ...])   # "opspilot-p7-decisions-1"

DebugRecord(session_id: str, item_id: str, input_hash: str, provider: str,
            config_hash: str, observation: JudgeObservation)

BalanceRecord(session_id: str, provider: str, phase: Literal["before", "after"],
              amount: str | None,                         # Decimal 字符串；None = 未知
              currency: str | None,                       # [A-Z]{3}
              source: Literal["manual", "deepseek_balance_api", "unknown"],
              shared_account_possible: bool)

FormalResult(run_id: str, pack_hash: str, config_hashes: Mapping[str, str],
             selection: SelectionRecord, readback: Literal["verified", "unresolved_as_expected", "failed"],
             readback_code: str | None)
```

`item_id` 规则：`"p7-" + input_hash[:24]`，由工具生成，调用方不能指定。理由：不透露场景、split、来源或原件/派生身份（P7-D7），且由内容决定、可复现。

`report_semantic_sha256` = `sha256(canonical_bytes(parsed_report(request.report).model_dump(mode="json")))`。用于拦截只改空白/JSON 格式的「伪唯一」（P7-D4）。

## 3. 入口 1：从 live capture 导入原件（P7-D2、D3、D12）

```python
import_capture(ws: Workspace, *, round_id: str, attempt_id: str, roots: SourceRoots,
               coverage: tuple[str, ...] = (), coverage_note: str | None = None,
               author_identity: str | None = None) -> ItemRecord
SourceRoots(evidence_root: Path, ledger_root: Path, recording_root: Path)
```

CLI：`import --workspace W --round R --attempt A [--evidence-root P] [--ledger-root P] [--recording-root P] [--coverage NAME ...] [--coverage-note TEXT --author-identity ID]`。缺省根与 `eval_live.real_deps` 相同：`<repo>/docs/evidence/eval`、`resolve_ledger_root(<repo>)`（即 Git 公共目录旁的 `tmp/lab-ledgers`）、`<repo>/tmp/eval`。注意 recording 根按**运行 live 的那个 worktree** 计算，跨 worktree 导入须显式给 `--recording-root`。

现有产物关系（核对自代码）：公开 `docs/evidence/eval/<R>/runs/<A>/summary.json`（`opspilot-eval-summary-1`）内含完整 capture（报告原文、delivered、outcome）与私有引用；`delivery_refs`（`deliveries/<sha>.json`）与 `ledger_ref`（`lifecycle.jsonl`）在 `<ledger_root>/<R>/<A>/`；`recording_ref`（`recording.json`、`blobs/*`）在 `<recording_root>/eval-live-<R>-<A>/`。账本目录按 round 而非 batch 分层（`eval_live.py:214-215`）。

处理顺序（任一步失败即 3，未写任何文件）：

0. 来源轮次核验（Q7，读取任何来源文件之前）：`R` 以 `eval-p7-` 开头（CLI 层已拒为 `USAGE`，库函数同样检查，不符 → `SOURCE_ROUND_INVALID`）；对 `<evidence_root>/<R>/` 目录、其 `aggregate.json` 与 `runs/<A>/summary.json` 逐个执行 1.2 第 1–2 步（`--evidence-root` 指向仓库外时 git 无法判定，即 `PRIVATE_PATH_UNVERIFIED`）。任一未忽略 → `PRIVATE_PATH_NOT_IGNORED`。理由：已被 Git 跟踪的来源报告已公开，不能作为未暴露的 validation 样本。
1. 读 `<evidence_root>/<R>/aggregate.json`，取 `capture.manifest`；缺失/坏 → `EVIDENCE_UNAVAILABLE`/`SCHEMA_INVALID`。
2. `load_summary_capture(<evidence_root>/<R>/runs/<A>/summary.json, manifest=该 manifest, resolve=eval_live.attempt_resolver(R, A, ledger_root=…, recording_root=…))`。这一步复用 storage 全部校验：manifest 绑定、身份、`view_sha256`、每个 delivery ref 的字节与 hash、recording/ledger ref 可解析、`evaluate` 重算与缓存 evaluation 一致、`report_parsed` 与原文重算一致。错误码原样透出（`IDENTITY_MISMATCH`、`HASH_MISMATCH`、`EVIDENCE_UNAVAILABLE`、`DERIVED_RESULT_MISMATCH`、`SCHEMA_INVALID`、`ATTEMPT_NOT_STARTED`、`JUDGE_IDENTITY_MISMATCH`）。驱动只使用 storage 重新校验后的 capture，不读取 summary 中的 `evaluation`、`report_parsed` 作为输入。
3. 来源条件：`attempt.mode == "live"`（否则 `SOURCE_NOT_LIVE`）；`attempt.status == "ran"`（否则 `SOURCE_NOT_RAN`）；`report`、`outcome`、`recording_ref` 均非 None（否则 `SOURCE_INCOMPLETE`）；`capture.judge is None`（否则 `SOURCE_ALREADY_JUDGED`，保证候选 judge 输出不进入样本）。
4. 重新核验 recording（同 `eval_live._verified_evidence`）：`load_recording(<recording_root>/eval-live-R-A/recording.json, expected_hash=recording_ref.sha256)`；`context.synthetic` 为真 → `IDENTITY_MISMATCH`；`usage["epoch"]` 不是 int → `IDENTITY_MISMATCH`；`check_recording_binding(recording, attempt, round_id=R, epoch=…, manifest=…)`；`recording.outcome != capture.outcome` → `IDENTITY_MISMATCH`；`evidence_from_recording(recording, outcome=…)` 的 `delivered`、`authorized` 与 capture 的逐项相等，否则 `DERIVED_RESULT_MISMATCH`。
5. 构造 `JudgeInput(spec, answer, report, delivered, authorized, outcome)`，全部取自 2、4 已核验的值。`check_identifiers(request)`（r11 范围，失败 `SCHEMA_INVALID`）；`input_hash = sha256(canonical_bytes(safe_input(request)))`（与 `calibration_flow` 第 463 行同算法；r7 私有槽 → `JUDGE_PRIVATE_FIELD`，报告解析失败 → `SCHEMA_INVALID`）。
6. gate：`run_gate(spec, answer, outcome, report, delivered, authorized).passed` 为假 → `GATE_NOT_PASSED`（见待裁决 Q4）。
7. 唯一性：同一 `(R, A)` 已导入 → `SOURCE_ALREADY_IMPORTED`；`input_hash` 与工作区任一 ItemRecord 相同 → `DUPLICATE_INPUT_HASH`；`report_semantic_sha256` 相同 → `DUPLICATE_REPORT`。
8. `source_family = f"live/{R}/{A}"`（满足 r8 正则）。理由：P7-D2 按原始录制划分，一次 live attempt 即一条录制；派生件继承此值，形成派生闭包。
9. coverage：名字须属 `COVERAGE` 七名，否则 `SCHEMA_INVALID`；coverage 非空时 `coverage_note` 与 `author_identity` 必填（`SCHEMA_INVALID`）。
10. 写 `items/<item_id>.json`（1.2）。stdout 带 `item_id`、`input_hash`、`scenario_id`、`source_family`。

只接受 live 源；real-model replay 补报告（P7-D3）本版不支持，回放 summary 一律 `SOURCE_NOT_LIVE`。理由：可逆默认「12 个来源一律新采」已覆盖需求，D3 只是允许不是要求。

## 4. 入口 2：登记受控改写（P7-D1、D4、D6、D7）

```python
register_rewrite(ws: Workspace, rewrite: Path) -> ItemRecord
```

CLI：`rewrite --workspace W --file PATH`。改写文件 schema：

```json
{"schema": "opspilot-p7-rewrite-1",
 "source_item_id": "p7-…", "source_report_sha256": "<64 hex>",
 "report_raw": "<新报告 JSON 字符串>",
 "change_description": "…", "purpose": "…",
 "coverage": ["borrowed_citation"], "coverage_note": "…",
 "author_identity": "…", "author_model_identity": "…"}
```

字段集严格（多/缺 → `SCHEMA_INVALID`）。文件**只能**提供报告层；其余层不可表达，由工具从源件复制。

机械校验（任一失败 → 3，不写文件）：

| # | 检查 | 码 |
|---|---|---|
| R1 | 源 ItemRecord 存在 | `REWRITE_SOURCE_UNKNOWN` |
| R2 | 源为 `original`（不允许改写的改写） | `REWRITE_SOURCE_NOT_ORIGINAL` |
| R3 | `source_report_sha256 == sha256(源 report.raw)` | `REWRITE_SOURCE_HASH_MISMATCH` |
| R4 | 派生 `request = replace(源 request, report=ReportCapture(raw=新, finish_reason=源 finish_reason))`；并断言 `canonical_bytes` 下 `spec/answer/delivered/authorized/outcome` 与源逐字节相等、`finish_reason` 相等 | `REWRITE_NON_REPORT_CHANGE` |
| R5 | `parsed_report` 成功且 `assessment_status == "completed"`（不得用坏 schema 充当负例，P7-D6） | `SCHEMA_INVALID` |
| R6 | 每条 claim 的每个 evidence_id 满足 `delivered[id].delivered is True` 且 `id ∈ outcome.evidence_ids` | `REWRITE_CITATION_NOT_DELIVERED` |
| R7 | `run_gate(...)` 通过（gate 合格、诊断语义错误，P7-D6） | `GATE_NOT_PASSED` |
| R8 | `check_identifiers(request)`；`safe_input` 成功 | `SCHEMA_INVALID` / `JUDGE_PRIVATE_FIELD` |
| R9 | `input_hash` 由 `safe_input` 实际投影重算，且在工作区全局唯一 | `DUPLICATE_INPUT_HASH` |
| R10 | `report_semantic_sha256` 全局唯一（含不等于源件），拦截只改空白/格式 | `DUPLICATE_REPORT` |
| R11 | `change_description`、`purpose`、`coverage_note` strip 后非空；coverage 名属七名且可为空 | `SCHEMA_INVALID` |
| R12 | `author_identity`、`author_model_identity` 满足 r9 标识规则；`author_identity` 与已导入的任一标注者 casefold 不同 | `SCHEMA_INVALID` / `CALIBRATION_UNRESOLVED` |

通过后写 `items/<item_id>.json`：`kind="controlled_rewrite"`，`source_family` 与 `provenance` 继承源件，`rewrite.label = REWRITE_LABEL`，`derived_report_sha256 = sha256(新 raw)`。

程序不能证明的事项（写入文档，不做检查）：改写是否「语义上错误」、覆盖说明是否属实、同义改写或引用顺序重排是否构成实质不同——由 D6 覆盖说明与独立复核承担。

## 5. 组装 split（P7-D2、D5）

```python
assemble(ws: Workspace, plan: Path) -> str          # 返回 assembled/items.json 原始字节 sha256（items_sha256）
```

CLI：`assemble --workspace W --plan PATH`。plan 为 `SplitPlan` JSON（`{"schema":"opspilot-p7-split-plan-1","families":{family: split},"items":[item_id…]}`）。

校验：

1. `items` 恰 36 个、无重复、均为已存在 ItemRecord；每项的 `source_family` 都在 `families` 中，`families` 中无未用族 → 否则 `SPLIT_PLAN_INVALID`。
2. 每个场景恰 2 debug + 4 validation，六场景齐全（`SCHEMA_INVALID`，与 `final_labels` 同码）。
3. 每个场景的 debug 项全部来自同一族，validation 项全部来自同一个**另外的**族（P7-D2「一族供 2 debug，一族供 4 validation」）→ `SPLIT_PLAN_INVALID`；同一族跨 split → `CALIBRATION_SET_OVERLAP`。
4. validation 项 coverage 并集等于七名 → 否则 `CALIBRATION_COVERAGE_INCOMPLETE`。
5. 每项全量复核 ItemRecord 不变量（§9.3 I1–I6）。

生成 `CalibrationItem(item_id, scenario_id, split, source_family, input_hash, request, coverage)`，按 `item_id` 升序写入 `assembled/items.json`（`{"items":[…]}`）。已存在 → `OUTPUT_EXISTS`（重组须新工作区）。理由：items 一旦导出给标注者即固定，避免标注后换样本。

P7-D5 的「每场景 validation 1 正 + 3 负」此处**不检查**：标签此时未知，作者不能指定（见待裁决 Q2）。

## 6. 入口 3：盲标包导出与标注导入（P7-D7、D8、D9）

### 6.1 导出

```python
export_blind_packets(ws: Workspace, *, convention: Path) -> str     # 返回 packet_sha256
```

CLI：`export-blind --workspace W --convention PATH`（P7-D9 约定文本，UTF-8，非空）。约定原文（去掉首尾空白后）必须作为子串逐字出现在当前 `judge.PROMPT` 中，否则 3/`CONVENTION_MISMATCH`。理由：按 Q1，约定写进 `judge.PROMPT`，标注者与 judge 收到的须是同一份。

`BlindPacket.items` 每项**只有** `item_id`、`input_hash`、`p6_input`；`p6_input` 恰为 `safe_input(request)` 的九键（question、subject_id、authorized_windows、report_raw、cited_evidence、expected_output、scenario_kind、injected、signature_matches），即「P6 输入、expected answer、签名规则结果」。顶层另有判据（`judge.PROMPT` 原文）、约定、输出 schema。

包内**不得出现**：`scenario_id`、`split`、`source_family`、`coverage`、`coverage_note`、`kind`（原件/派生）、`rewrite` 任一字段、`provenance` 任一字段、其他标注、任何 judge 输出。验收可断言：对包做字符串全文扫描，不含任一 `source_family` 值、`round_id`、`attempt_id`、`product_run_id`、`change_description`、`purpose`、`coverage_note` 原文、七个 coverage 名作为 JSON 键或值出现在 `p6_input` 之外。

写 `blind/packet.json` 与两份逐字节相同的 `blind/annotator-{1,2}/packet.json`；item 顺序按 `item_id` 升序，重复导出（新工作区、同输入）得到相同字节。已存在 → `OUTPUT_EXISTS`。

### 6.2 导入

```python
import_annotation(ws: Workspace, *, slot: Literal[1, 2], response: Path,
                  annotator_identity: str, model_identity: str,
                  evidence_note: str | None = None) -> str     # 返回 annotation 文件 sha256
```

CLI：`import-annotation --workspace W --slot 1|2 --response PATH --annotator-identity ID --model-identity ID [--evidence-note TEXT]`。槽位与模型固定（P7-D8、D15，SPEC 只批准这两个数据出口）：slot 1 = Claude Opus 5.5（`--model-identity claude-opus-5-5`），slot 2 = Codex（`--model-identity gpt-6-astra`）；两者都按 Q8 隔离启动。身份由 lead 按实际启动参数填写，标注者自报的身份不采信。

校验（任一失败 → 3）：

1. 响应严格解析为 `BlindResponse`；`schema` 正确；`packet_sha256` 等于当前 `blind/packet.json` 的 sha256 → 否则 `PACKET_STALE`。
2. 标签恰覆盖 assembled 的 36 个 item_id、无重复，每项 `input_hash` 与 item 一致 → `SCHEMA_INVALID` / `HASH_MISMATCH`。
3. `annotator_identity`、`model_identity` 通过 r9 标识规则（`SCHEMA_INVALID`）；`model_identity` 不等于该 slot 的固定值（slot 1 `claude-opus-5-5`、slot 2 `gpt-6-astra`，精确相等）→ `ANNOTATOR_NOT_APPROVED`；`is_candidate_model_identity(model_identity)` 为真 → `CALIBRATION_UNRESOLVED`（r9：`glm53/gpt61sol/deepseekflash` 片段，含装饰写法）；`annotator_identity` 与另一 slot 或任一改写作者 casefold 相同 → `CALIBRATION_UNRESOLVED`。
4. 每个标签 `check_blind_label(item, label)`；失败 → `CALIBRATION_LABEL_INVALID`，stdout 附 `invalid_items`（只列 item_id）。整份拒绝，不部分接收；标注者可交新响应文件重导入。
5. 构造 `AnnotationSet(annotator_identity, model_identity, labels)`，`labels` 按 `item_id` 升序（诊断顺序见待裁决 Q3）；写 `annotations/annotations-<slot>.json` = `canonical_bytes(AnnotationSet)` 与 provenance 侧文件。slot 已存在 → `OUTPUT_EXISTS`。
6. 读取核验（#285 机器人）：此后任何子命令（brief、adjudicate、debug、freeze、formal、summary）每次读取 `annotations-<slot>.json` 时，都须同时读取其 provenance 并核对：`annotation_sha256` 等于该文件原始字节 sha256 → 否则 `HASH_MISMATCH`；provenance 的 `slot` 等于文件槽位、`annotator_identity`/`model_identity` 与 `AnnotationSet` 中的相同、`model_identity` 等于该槽固定值 → 否则 `CALIBRATION_UNRESOLVED`；provenance 缺失 → `PACKET_STALE`。理由：导入后被改写或替换的标注不能冒充独立盲标进入 freeze 与正式校准。

## 7. 入口 4：差异简报与用户裁决（P7-D10）

```python
write_adjudication_brief(ws: Workspace) -> str                       # 返回 brief sha256
apply_decisions(ws: Workspace, decisions: Path) -> tuple[Adjudication, ...]
```

CLI：`brief --workspace W`；`adjudicate --workspace W --decisions PATH`。

简报：两份 annotation 均存在才可生成（否则 `CALIBRATION_UNRESOLVED`）。「差异」严格按 `final_labels` 口径：两个 `BlindLabel` 不相等（dataclass 全字段，含 reason 文本与诊断顺序）即为差异项；一致项只计数不列出。每个差异项给出完整 A/B 标签与 p6_input（与盲标包同一白名单，不含 coverage/split/来源/作者意图，见待裁决 Q5）。`annotation_hashes` = (annotations-1 文件 sha256, annotations-2 文件 sha256)。

裁决文件 `Decisions`：

1. `brief_sha256` 等于当前简报 → 否则 `BRIEF_STALE`。
2. 每个差异项恰一条 decision；缺失、重复、或对一致项给出 decision → `CALIBRATION_UNRESOLVED`。
3. `decision_record.strip()` 为空 → `CALIBRATION_UNRESOLVED`。
4. `choice="A"` → 取 annotations-1 该项标签原值；`"B"` → annotations-2；此两者 `label` 必须为 null，否则 `SCHEMA_INVALID`。`"revised"` → `label` 必填，生成 `BlindLabel(item_id, item.input_hash, diagnoses, verdict)` 并 `check_blind_label`，失败 `CALIBRATION_LABEL_INVALID`。
5. 生成 `Adjudication(item_id, annotation_hashes, decision_record, label)`，按 item_id 升序；以 `LabelPack(items, (ann1, ann2), adjudications)` 调用 `final_labels` 预检（`CALIBRATION_TOO_FEW_NEGATIVES` 等原样透出）；通过后写 `adjudication/decisions.json`（原件副本）与 `adjudications.json`。已存在 → `OUTPUT_EXISTS`。

stdout 额外给出最终标签分布：每场景 validation 正/负数、debug 正/负数（只计数）。

## 8. 入口 5：debug-only 调用（P7-D11、D9）

```python
debug_judge(ws: Workspace, *, session_id: str, providers: tuple[str, ...],
            item_ids: tuple[str, ...] | None,
            configs: Mapping[str, JudgeConfig], transports: Mapping[str, JudgeTransport],
            balance: BalanceReader | None = None) -> tuple[DebugRecord, ...]
```

CLI：`debug --workspace W --session S --judge-env PATH [--provider zai|openai|deepseek ...]（缺省三家）[--item ID ...]（缺省全部 12 个 debug 项）`。

1. 需要 `assembled/items.json`；`item_ids` 中任一项 `split != "debug"` 或不存在 → 3/`VALIDATION_EXPOSURE_FORBIDDEN` / `SCHEMA_INVALID`，**零调用**。
2. 机械防护：驱动内 `judge_once` 只有两个调用来源——本函数（候选集合先过滤为 split=debug，再逐项断言）与 `run_calibration`。验收可用假 transport 记录全部请求体，断言任何 debug 会话中都不出现 validation 项 `p6_input`（按 `input_hash` 重算比较）。
2a. 配置与判据预检（无副作用）：本次涉及的每家 `configs[p].provider == p` 且 `transports` 恰含这些 provider → 否则 `CONFIG_INVALID`；若已导出盲标包，按 §9.2 第 4 步同一规则核对约定仍逐字在当前 `judge.PROMPT`、items 与 packet hash 未变 → 否则 `JUDGE_PROMPT_CHANGED` / `PACKET_STALE`。理由：键名与配置错位会把费用和可用性证据记到错误的 provider；约定已失效时 debug 结果不能与标签比较，应在付费前拒绝。
2b. 来源族用途预检与登记（#286，跨工作区，原子）：私有根下 `exposure.lock`、`exposure.jsonl` 按 §9.2 第 7 步同样核验（缺失 → `EXPOSURE_REGISTRY_MISSING`，未忽略 → 1.2 对应码）。在第 3–4 步其余无副作用预检全部通过之后、第一笔其他写入之前，持 `exposure.lock` 的 `flock(LOCK_EX)`：本次 debug 项的 `source_family` 或 `(round, attempt)` 来源，与登记中任一 `kind="formal"` 行的 validation 族或来源、或任一工作区 `*/calibration/*/label-pack/items.json` 中的 validation 项有交集 → 释放锁，`VALIDATION_EXPOSED`（退出 3，未写任何文件）；否则在锁内以 `O_APPEND` 写入并 fsync 一行 `{kind: "debug", session, workspace, debug_families, sources, at}`，即本命令第一笔写入，然后释放锁。同一来源族可被多个 debug 会话重复登记。理由：同一来源族在不同工作区可能分别标为 debug 与 validation；候选 judge 在调优中见过的族不能再作为冻结 validation 计分（P7-D11）。
3. 成本前置（§10）：本次涉及的 zai/openai 须已有手填的 `costs/S/<provider>-before.json`，否则 3/`COST_BEFORE_MISSING`；deepseek 的 before 由本命令写，预检时须不存在，否则 3/`OUTPUT_EXISTS`。
4. 预检全部目标路径（1.2，在第 2b 步登记之前完成）后，第 2b 步登记为第一笔写入，随后写 deepseek before（若涉及 deepseek，§10），随后按 provider 顺序 zai→openai→deepseek、items 按 item_id 升序，串行：`obs = judge_once(item.request, config=configs[p], calibration=None, transport=transports[p])`；立即写 `debug/S/<p>-NNN.json`（`DebugRecord`，`config_hash=judge_config_hash(config)`），写成功后才发下一次。写失败 → 5，不再调用。
5. 结果不产生任何校准认证：不写 `calibration*`，`obs.passed` 恒为 False（`calibration=None`）。
6. stdout：每家 ok/invalid/error 计数；若裁决已完成，额外给出每家「`result.verdict` 与 debug 最终标签相同」数与「关键因果诊断 `(location, quote)` 精确命中」数（同 `_missing_diagnosis` 口径）。是否「大量误判须停下交用户改规则」（P7-D9）由 lead 判断，工具不设阈值。

判据/约定如何改变 judge 的行为见待裁决 Q1（当前 `judge._check_config` 拒绝任何非 `PROMPT` 的 prompt，`judge.py:183`）。

## 9. 入口 6：正式冻结、运行与读回（P7-D11、D14）

### 9.1 `freeze`

```python
freeze(ws: Workspace, *, accept_distribution: str | None = None) -> str   # pack_hash
```

CLI：`freeze --workspace W [--accept-distribution BRIEF_SHA256]`。先核对每场景 debug 最终标签恰 1 正 1 负（P7-D5，用于同时观测误放与漏判），不符 → `LABEL_DISTRIBUTION_MISMATCH`（退出 3），`--accept-distribution` 不豁免此项。再按 Q2 核对每场景 validation 实际标签分布：每场景恰 1 正 3 负才放行；否则无参数时 `LABEL_DISTRIBUTION_MISMATCH`（退出 3，stdout 附每场景计数与 `adjudications_sha256`，供用户写接受记录）。`accept_distribution` 必须等于当前 `adjudication/brief.json`（§7 `brief` 写出的同一文件）原始字节的 SHA256（即用户书面接受偏差时所见简报），且工作区存在用户写入的 `adjudication/distribution-acceptance.json`（字段 `brief_sha256`、`adjudications_sha256`、`distribution`、非空 `decision_record`；`brief_sha256` 等于参数，`adjudications_sha256` 等于当前 `adjudication/adjudications.json` 原始字节的 SHA256，`distribution` 等于由当前最终标签重算的每场景 validation `{positive, negative}` 计数）；读取前对该文件执行 1.2 第 1–2 步（不在工作区内、是符号链接或未被忽略 → 对应 1.2 码）；文件缺失或任一字段不符为 `ACCEPTANCE_MISMATCH`（退出 3）。接受记录随 pack 一同进入 `summary` 输出。理由：brief 生成于裁决之前，只绑定 brief 时，同一份接受记录可被另一组裁决产生的不同分布复用；绑定裁决字节与最终分布后，接受记录只对用户实际看到的那一个分布有效。分布符合时传该参数为 `USAGE`（退出 2），不得预先豁免。然后组装 `LabelPack(items（assembled 顺序）, (annotations-1, annotations-2), adjudications)`；复核 §9.3 I1–I6；`freeze_label_pack(W/label-pack, pack)`；再 `load_label_pack(W/label-pack, expected_pack_hash=…) == pack`，否则 `HASH_MISMATCH`。无付费调用。stdout 给 `pack_hash`。

### 9.2 `formal`

```python
formal(ws: Workspace, *, run_id: str, pack_hash: str,
       configs: Mapping[str, JudgeConfig], transports: Mapping[str, JudgeTransport],
       balance: BalanceReader | None = None) -> FormalResult
```

CLI：`formal --workspace W --run RUN --pack-hash H --judge-env PATH`。

预检（全部通过才发第一次调用；失败 3）：

1. `pack = load_label_pack(W/label-pack, expected_pack_hash=H)`。
2. 三家配置均由 `load_judge_config` 得到；`configs` 与 `transports` 的键都恰为 zai/openai/deepseek，且每个 `configs[p].provider == p` → 否则 `CONFIG_INVALID`（在此预检，不留给 `run_calibration` 在登记之后才拒绝）。
3. 可用性与冻结前置：存在至少一个 debug 会话，其中每家都有至少一条 `status == "ok"` 且 `config_hash` 等于本次 `judge_config_hash(configs[p])` 的记录 → 否则 `DEBUG_PRECHECK_MISSING`。理由：首次真实调用要核实 `gpt-6.1-sol` 等端点可用（B3 r3/r5 遗留），并保证正式运行用的是 debug 阶段已验证、已冻结的配置；若 Sol 不可用而直接正式跑，GLM 可能在没有有效对照的情况下被自动选中。
4. 判据绑定（P7-D9、Q1）：包的 `convention` 仍逐字出现在当前 `judge.PROMPT` 中 → 否则 `JUDGE_PROMPT_CHANGED`；包的 `items_sha256` 等于当前 `assembled/items.json` 字节 sha256，且两份 `AnnotationProvenance.packet_sha256` 都等于当前 `blind/packet.json` 字节 sha256 → 否则 `PACKET_STALE`。`judge_prompt_sha256` 与当前 PROMPT 不同不拒绝，二者都写入运行记录（`summary` 输出）。理由：标签判断的是诊断本身对不对，标注者所见的约定须与 judge 所收一致；P7-D11 先盲标、后只调 judge，调优改动约定之外的 PROMPT 措辞不改变标签含义。若调优需要改约定本身，约定校验失败，lead 停下交用户（重新盲标需新工作区）。
5. 成本前置（§10）：zai、openai 的 `costs/RUN/<p>-before.json` 均已存在 → 否则 `COST_BEFORE_MISSING`；`costs/RUN/deepseek-before.json` 须不存在 → 否则 `OUTPUT_EXISTS`。
6. 预检 `calibration/RUN/` 下全部将创建路径与 `costs/RUN/deepseek-before.json`（1.2，只做判定不写）；`calibration/RUN` 已存在 → `OUTPUT_EXISTS`。
7. 暴露检查与登记（跨工作区，原子）：私有根下 `exposure.lock`、`exposure.jsonl` 任一缺失 → `EXPOSURE_REGISTRY_MISSING`。以 `fcntl.flock(LOCK_EX)` 持有 `exposure.lock`，在锁内依次：读 `exposure.jsonl`，并扫描私有根下**所有**工作区的 `*/calibration/*/label-pack/items.json`；任一已登记（`kind="formal"` 行的 validation 族，或 `kind="debug"` 行的 debug 族，见 §8 第 2b 步）或已出现的 `source_family`，或同一 `(round, attempt)` 来源，与本 pack 的 validation 族或来源有交集 → 释放锁，`VALIDATION_EXPOSED`（退出 3，未写任何文件）。无交集则仍在锁内完成本运行的第一笔写入：以 `O_APPEND` 写入并 fsync 一行 `{kind: "formal", run_id, workspace, pack_hash, validation_families, sources, at}`，然后释放锁。并发的两个 formal 因此只有一个能登记同一来源族。

第 1–6 步都无副作用；第 7 步的登记是第一笔写入，此前任何失败都是退出 3 且不写文件、不占用来源族。登记之后：`calibration/` 父目录不存在则创建，再按 §10 写 deepseek before，然后才发第一次调用。登记及其后的写失败为 5（`ARTIFACT_UNWRITABLE`），不发调用；已登记的来源族不再可用（重试须换 validation 来源族，见 P7-D11）。驱动不提供删除或改写登记的入口。此登记是防止重跑挑结果的流程护栏，不是对能改私有目录者的安全边界。

执行：`selection = run_calibration(pack, configs=configs, transports=transports, destination=W/calibration/RUN)`；其中途 `JudgeError` 码原样透出，退出 5（`ARTIFACT_UNWRITABLE`/`OUTPUT_EXISTS`）或 3（发送前拒绝的码，如 `CALIBRATION_UNRESOLVED`、`CONFIG_INVALID`）。随后自动记录 DeepSeek 余额 after（§10）。

读回（只读、无付费调用）：

| selection.status | 动作 | readback | 退出 |
|---|---|---|---|
| `selected` | `load_verified_calibration(W/calibration/RUN, pack=pack, config=configs[selected_provider])` 成功且 `result.passed` | `verified` | 0 |
| `pending_user_decision` | 用 `configs["zai"]` 调 `load_verified_calibration`，**期望** `JudgeError("CALIBRATION_UNRESOLVED")`（证明逐次件与缓存一致、只是未选定） | `unresolved_as_expected` | 6 |
| `no_baseline` | 同上，期望 `CALIBRATION_UNRESOLVED` | `unresolved_as_expected` | 1 |
| 任一情形得到非预期结果/码 | — | `failed`，`readback_code` = 实际码 | 1 |

pending 时驱动不提供任何写入 selection 或人工批准的入口（P7-D14；认证入口另行实现）。stdout 给 pack_hash、三家 config_hash、`selection.status/selected_provider/eligible`、每家 `CalibrationResult` 计数字段（items、comparisons、agreement、negatives、false_passes、reasons）。

现有语义提示（不改，供测试作者理解）：`_counts` 对 debug 项同样记录 `JUDGE_OBSERVATION_FAILED`、`KEY_DIAGNOSIS_MISSING`（`calibration_flow.py:735-763`），即正式运行中任一 debug 项出错也会使该家不合格；`observation.balance_delta` 非 None 会被判 `DERIVED_RESULT_MISMATCH`（同文件第 729 行），所以余额只写在 `costs/`，不写进观察记录。

### 9.3 工作区不变量（assemble、freeze 时对每个 ItemRecord 复核，失败 3）

- I1 `item_id == "p7-" + input_hash[:24]`，`input_hash == sha256(canonical_bytes(safe_input(request)))`（`HASH_MISMATCH`）。
- I2 `report_semantic_sha256` 重算一致且全局唯一；`input_hash` 全局唯一（`HASH_MISMATCH` / `DUPLICATE_*`）。
- I3 original：`rewrite is None`，`source_family == f"live/{round}/{attempt}"`；controlled_rewrite：`rewrite.label == REWRITE_LABEL`，源件存在且为 original，`source_family`/`provenance` 与源件相同，R4、R6 对源件复验通过（`REWRITE_*`）。
- I4 `run_gate` 通过（`GATE_NOT_PASSED`）。
- I5 `check_identifiers(request)`；coverage 名合法；派生件 coverage_note 非空（`SCHEMA_INVALID`）。
- I6 `scenario_id == request.spec.scenario_id`（`IDENTITY_MISMATCH`）。

导入时对 live 源文件的核验（§3 第 2–4 步）只在 import 时执行一次；之后工作区记录的真实性依赖私有目录不被改写。理由：freeze 时源文件可能已随 worktree 清理；测试作者可直接按本节 DTO 写 ItemRecord 文件来构造后续阶段的夹具。

## 10. 入口 7：费用记录

```python
record_balance(ws: Workspace, *, session_id: str, provider: str, phase: Literal["before", "after"],
               amount: str | None, currency: str | None,
               source: Literal["manual", "deepseek_balance_api", "unknown"]) -> None
BalanceReader = Callable[[str], Decimal | None]    # provider -> 余额；仅 deepseek 有实现
cost_report(ws: Workspace, session_id: str) -> Mapping[str, Any]
```

CLI：`balance --workspace W --session S --provider P --phase before|after (--amount D --currency CCY | --unknown)`；`cost --workspace W --session S`（只读）。

- zai、openai：现有代码没有余额查询，**由调用方手填**（`source="manual"`），驱动不新增外部调用；也可显式记 `--unknown`。
- deepseek：before/after 的唯一写入方是 `debug`/`formal`；`balance --provider deepseek` 一律 2/`USAGE`。before 在该命令全部无副作用预检通过之后（formal 还在暴露登记之后）、第一次付费调用之前写且只写一次，after 在最后一次调用之后写；用现有 `scripts.eval_live_lab.DeepSeekBalance(config.api_key).balance()`（CNY）记录 `source="deepseek_balance_api"`；返回 None 时记 `amount=None, source="unknown"`，不中断。预检时 deepseek before 已存在 → 3/`OUTPUT_EXISTS`（换新的 session/run ID）。`shared_account_possible` 对 deepseek 恒为 True（judge 与被测模型可能同账户，lab 同时运行会污染差值）。理由：沿用 `eval_live._cost` 的字段与 unknown 语义（`eval_live.py:1243-1256`）。
- 同一 `(session, provider, phase)` 只能写一次（`OUTPUT_EXISTS`）；金额须为有限 Decimal 字符串，货币 `[A-Z]{3}`（`SCHEMA_INVALID`）。
- `cost_report`：每家 `delta = before - after`（两者都已知且币种相同才计算，否则 `status="unknown"`），以及该会话观察记录 `usage` 的 prompt/completion/total_tokens 合计（缺值不计入并报告缺失数）、调用次数。

## 11. 汇总（只读）

`summary --workspace W` 打印可公开的状态：各场景/split/kind 项数、validation coverage 并集、items_sha256、packet_sha256、两份 annotation sha256 与模型身份、差异项数与裁决数、pack_hash、各 formal run 的 selection 与计数、各会话费用差。不含报告、标签或裁决文本。理由：任务记录与 PR 只需要 hash 与结果（B3「公开仓库只记录允许的 hash/结果」）。

## 12. 入口与合同条款对照

| 入口 | 落实条款 |
|---|---|
| `import` | D2（来源族=原始录制）、D3（只认 live 源）、D4（input_hash 由 P6 投影重算）、D12（源 round 记录在 provenance，供基线排除） |
| `rewrite` | D1（标记与私有绑定）、D4（只改报告层、只引已交付证据、不凑唯一）、D6（gate 合格、覆盖说明） |
| `assemble` | D2（每场景 debug 族与 validation 族分离）、D5（数量分布；正负比见 Q2） |
| `export-blind` / `import-annotation` | D7（白名单字段、两份相同）、D8（记录实际模型身份、拒绝候选名）、D9（约定原文随包下发） |
| `brief` / `adjudicate` | D10（逐项完整差异、绑定两份 hash、一致项不得裁决） |
| `debug` | D11（只调 debug 项、结果私有、无认证）、D9（同一约定） |
| `freeze` / `formal` | D11（冻结后正式、validation 暴露检查）、D14（pending 停下、不写 selection）、D13（read-back） |
| `balance` / `cost` | D13（费用记录）；AGENTS「费用按供应商余额差对账」 |
| 1.2 ignore 核验 | D13 数据出口不扩大；预审「0600 ≠ 不会被提交」 |

## 13. 验收测试作者须知（夹具来源）

- 真实格式的 live 源：用 `tests/acceptance/eval_c_support.py` 的 `Harness` 以假 deps 跑 `scripts.eval_live`，得到 summary/recording/deliveries；`--recording-root`、`--ledger-root` 指向 `tmp_path`。
- 36 项 JudgeInput 与标签：可用 `tests/acceptance/eval_b3_support.py` 的 `build_world()`、`annotation()`，按 §2 DTO 直接写 `items/*.json`（§9.3 说明此路径合法）。
- 假 judge：`eval_b3_support.FakeTransport` / `ok_reply`；凭据文件用 `write_env`（0600）。
- ignore 核验：注入 `ignore_check`；另需至少一个用真实 `git init` 临时仓库 + `.gitignore` 的端到端用例（含 `!` 反向规则放出单个文件的情形）。
- 不得联网；`DeepSeekBalance` 以注入的 `BalanceReader` 替代。

## 已裁决（P7 合同 r2）

以下 Q1–Q8 是合同 r1 未覆盖的语义点，2026-10-19 按用户常设指示全部采纳推荐（Q1 A、Q2 A、Q3 B、Q4 A、Q5 A、Q6 A、Q7 A、Q8 A），裁决记录见 [P7 任务记录](2026-10-18-eval-p7-calibration.md)。Q7 的前缀修订为 `eval-p7-`，`.gitignore` 规则 `docs/evidence/eval/eval-p7-*/` 已随 PR #282 加入；已开跑的来源轮次 `eval-p7-src-20261019` 在该规则合入前写在主工作树未跟踪目录，采集结束后由 lead 移入 `tmp/` 私有目录，未提交。Q6 的暴露检查按 PR #282 机器人意见扩为跨工作区登记（§9.2 第 4 条）；Q2 的接受参数写入 §9.1。下文保留选项原文供追溯。

**Q1 判据与 P7-D9 约定如何到达 judge（影响 debug 调优能否进行）。** 事实：`judge._check_config` 要求 `config.prompt == PROMPT`（`judge.py:183`），`load_judge_config` 不读 prompt，所以 debug 调优和「给三家 judge 同一份引文粒度与排序约定」只能改 `judge.PROMPT` 常量，而这属于 judge 模块代码。
- A（推荐）：约定写进 `judge.PROMPT`，每次调优是一个独立小 PR（改 PROMPT 即改 config_hash），本驱动不提供 prompt 覆盖；盲标包的 `judge_prompt` 取当时的 `PROMPT`，`convention` 取同一份约定原文；调优只改约定之外的措辞，正式运行前核对约定仍逐字在 PROMPT 中（§9.2 第 4 步）。代价：每轮调优走一次 PR；好处：冻结配置有代码与 hash 两重证据，不扩大驱动权限。
- B：驱动接受 `--prompt-file`，放宽 `_check_config` 的 prompt 检查。代价：改动 B3 已审的配置安全边界，属合同变更。
- C：约定只给标注者、不给 judge。违背 P7-D9「同一份」。

**Q2 P7-D5「每场景 validation 1 正 + 3 负」是硬门还是目标。** 标签由盲标与裁决产生，作者不能指定；`final_labels` 只强制 validation 负例 ≥12。裁决后若某场景是 0 正 4 负或 2 正 2 负怎么办？
- A（推荐）：作为目标。`adjudicate` 报告实际分布；若某场景 validation 正例为 0 或与 1+3 不符，`freeze` 拒绝（`LABEL_DISTRIBUTION_MISMATCH`），交用户决定是补新来源族重做（新族需重新盲标），还是书面接受偏差后重跑 freeze（需加 `--accept-distribution <brief_sha256>`）。
- B：硬门，不符一律重做，不提供接受入口。
- C：只执行 `final_labels` 现有检查，不看场景分布。风险：某场景无正例时无法测到漏判。

**Q3 标签相等判定会被非语义差异放大。** `final_labels` 用 dataclass 全等（`calibration_flow.py:499`），两位标注者的 `reason` 自由文本几乎必然不同，诊断顺序不同也算差异，实际上 36 项会全部进入用户裁决。
- A：保持原样（D10 已接受最坏 36 项）；简报标出「verdict 与 (location, quote) 集合均相同、只差 reason/顺序」的项，便于用户批量处理（每项仍需非空 decision_record）。
- B（推荐）：A + 导入时把每个标签的 diagnoses 按 `(location 下标顺序, quote)` 稳定排序后再生成 AnnotationSet（不改任何文字；排序本身属 D9 排序约定的机械执行）。
- C：B + 约定标注者 `reason` 一律留空。会失去裁决时的理由信息，不推荐。

**Q4 原件是否也必须 gate 通过。** D6 只说覆盖样本须「gate 合格」。gate 不过的原件：生产中 judge 会被跳过，用它校准 judge 意义不大；而且 `check_diagnoses` 会让这类项只能标成「诊断重算为 False」。
- A（推荐）：36 项一律要求 `run_gate` 通过，原件不过则 import 拒绝（`GATE_NOT_PASSED`），该来源族只能用于派生件以外的用途或弃用。
- B：只对派生件和带 coverage 的项要求。

**Q5 裁决简报对用户是否同样盲。** D7 约束的是标注者；D5 要求「最终标签由盲标与裁决决定，作者不能指定」。若简报展示 coverage、作者意图或改写说明，用户裁决可能被作者意图带偏。
- A（推荐）：简报只含盲标包同一白名单 + A/B 标签；用户需要来源信息时另用 `summary` 查看，不进入裁决文件。
- B：简报附 coverage 与改写说明，方便用户理解改写目的。

**Q6 formal 重跑口径（P7-D11「validation 暴露后修 judge 须换新的 validation 来源族」的边界）。** 未覆盖：(a) 配置不变、因网络或写盘失败中途中止后，能否用同一 pack 重跑；(b) 配置不变、结果不满意时能否「再跑一次」。
- A（推荐）：任何已出现在某个 `calibration/*` 目录（含中途中止）的 validation 来源族都不得再进 formal（`VALIDATION_EXPOSED`）；中止属于事故，按换族处理。最严格，可防止反复重跑挑结果。
- B：允许「配置 hash 完全相同且上一次 run 未写出 `calibration.json`」的情形重跑一次，记录前一 run 目录；其他情形同 A。
- C：只在配置 hash 变化时拒绝。允许同配置反复重跑，存在挑结果风险。

**Q7 P7 来源 round 的 summary 是否进公开仓库。** 事实：`eval_live` 固定把 `summary.json`（含报告原文与 delivered 视图）写到 `docs/evidence/eval/<round>/`（`eval_live.py:1886`），该目录未被忽略、此前的 round 已入库；P7 计划第 2 步要求「原件放 ignored 私有目录」。
- A（推荐）：本 chore PR 在 `.gitignore` 增加 `docs/evidence/eval/eval-p7-*/`（已随 PR #282 加入），P7 来源 round 名必须以 `eval-p7-` 开头，`import` 对 summary 所在 round 目录也做 1.2 ignore 核验，不通过则 `PRIVATE_PATH_NOT_IGNORED`。不改 `eval_live`。
- B：照常写入 `docs/evidence`，跑完后把整个 round 目录移到 `tmp/` 再导入（`--evidence-root` 指过去）。中间存在误提交窗口。
- C：允许 P7 来源 summary 公开入库（validation 报告原文公开，标签不公开）。

**Q8 盲标隔离的实际手段。** 工具只能保证包内字段；标注 Agent 若有仓库文件系统读权限，仍可读到 `items/`、改写记录或另一份标注。r9 已承认「记录身份不证明全新上下文」。
- A（推荐）：流程约定，不改代码：标注前把 `blind/annotator-N/packet.json` 复制到仓库外的临时目录交给标注 Agent，两位标注者同等隔离。Codex 以该目录为工作目录、只读沙箱启动。Claude 不用 lead 会话内的子 Agent（会继承父上下文与仓库读权限），而是另起一个全新的 Claude Code 非交互进程：不恢复任何会话，工作目录为该副本目录，文件访问限于该目录（或禁用全部工具、经 stdin 传入包内容）。`evidence_note` 记录会话 ID、启动参数与工作目录。做不到上述隔离的标注不得作为 P7 证据导入。
- B：工具提供 `export-blind --out <仓库外路径>`。与第 8 项「所有工件只写 ignored 私有目录」冲突，需要放宽该约束。
