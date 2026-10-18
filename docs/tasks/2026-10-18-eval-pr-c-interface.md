# 评测 PR-C 公开接口（r1）：live kind/OTel 场景驱动

- 依据：[PR-C 合同 r1](2026-10-18-eval-pr-c-contract.md)（下称 C，U1–U3、A1–A11 均按 B 采纳）；[套件合同](2026-10-12-eval-suite-contract.md) D3/D4/D5/D10/D15、P2/P10/P12/P13；[PR-B2 接口](2026-10-14-eval-pr-b2-interface.md)；[PR-A 接口](2026-10-13-eval-pr-a-interface.md)；issue #234。
- 用途：供未参与实现的测试作者编写验收/合同测试。只写可观察行为与接缝，不写实现。末节「待 lead 裁决」中的项目在裁决前按本文推荐理解。
- 离线原则：除真实 kind/flagd/Prometheus/Jaeger/PG/DeepSeek/LangSmith 调用本身外，全部行为都能用第 3 节的假对象离线测试。真实适配器只由 `real_deps()` 构造，测试不需要也不应调用它。

## 1. 模块

| 模块 | 内容 |
|---|---|
| `scripts/eval_live.py` | CLI `main(argv, *, deps=None) -> int`；`run_round(config, deps) -> RoundResult`；`recover_round(config, deps) -> RecoverResult`；`attempt_resolver(...)` |
| `scripts/eval_live_lab.py` | flag 白名单、恢复意图与占用协议、预观察门槛、接缝 Protocol 与 DTO、`LabError`、`Interrupted`、`real_deps()` |
| `scripts/kind_lab.py` | `fault inject|restore` 增加 `--flag`（第 4 节） |
| `opspilot/evaluation/live.py` | 纯函数 `evidence_from_recording()`（录制 → B1 可见证据），从 `opspilot.evaluation` 导出 |
| 产品代码 | `InvestigationRunner` 两个默认 None 的接缝；`opspilot.tools.otel_demo` 公开注册 DTO（第 8 节） |

## 2. CLI：`.venv/bin/python -m scripts.eval_live`

运行一轮：

```text
--mode live --round R --scenario ID [--scenario ID ...] | --scenario all
--n N --parallel 1 --holder H --batch B --flagd-url URL --pg-port PORT [--dry-run]
```

恢复：`--recover --round R --holder H --batch B --flagd-url URL`。

- `--mode` 必填，只接受 `live`。`--round` 匹配 `[a-z0-9][a-z0-9.-]{0,62}`，LangSmith project 为 `opspilot-lab-<R>`。
- `--scenario` 取 `evals/scenarios/` 下的 scenario_id，可重复；`all` 表示冻结的六例，不能与具体 ID 混用。`--n` 为每场景次数，必须 ≥1。`--parallel` 必填且必须为 1。
- `--holder/--batch` 是 A3 的占用身份。驱动只 **check** 已有预约，不 claim、不 release（第 5 节）。
- `--flagd-url` 是事先转发好的本机 flagd 评估端点，只接受 `http://127.0.0.1|localhost|[::1]:<port>`，不能带路径、凭据或查询串；驱动从不自己起 port-forward。
- `--pg-port` 是临时 PG 的显式端口，每个 attempt 在此端口新起一个实例（第 11 节）。
- `--dry-run`：打印 JSON 计划（槽位顺序、路径、flag），只读场景目录，不调用任何接缝，不写文件，退出 0。
- 槽位顺序固定：先按 scenario_id 排序，再按 repetition 1..N（场景为主序）。
- 环境前提（真实运行时）：`OPSPILOT_TRACE=lab`，`M0_ENV_FILE` 提供 DeepSeek/LangSmith 凭据（读取方式同既有脚本，不打印），调查侧 Prometheus 账号变量已载入；独立观察使用 lab 账号（`kind_lab.lab_authorization`）。缺失时退出 3，原因码 `ENV_UNCONFIRMED`。

| 退出码 | 含义 |
|---|---|
| 0 | 全部计划槽位均已开始，且都是 `ran`；lab 已恢复且洗净 |
| 1 | 全部计划槽位均已开始；至少一个 `setup_failed`/`aborted`；lab 已恢复且洗净 |
| 2 | 参数错误（argparse、`--parallel≠1`、未知场景、非法 round/URL/N），未写任何文件 |
| 3 | 预检拒绝，未开始任何 attempt（不写 journal）：占用缺失/异主/过期、已有 `fault-active.json`、fault guard 被占、lab 身份不明、PG 端口已占用、flag 不在正常态、round 证据目录已存在、环境前提缺失 |
| 4 | 恢复或洗净失败：整轮停止，`fault-active.json` 保留，后续槽位记 `not_started` |
| 5 | 证据写入失败（journal/summary/lifecycle），lab 已恢复；整轮停止 |
| 130 | 收到 SIGINT/SIGTERM，且当前 attempt 已恢复、洗净；后续槽位 `not_started`。恢复失败时改为 4 |

`--recover`：只处理 `round == R` 的保留故障状态，按第 5 节恢复规则执行并洗净。成功删除状态后退出 0；仍失败退出 4；状态不存在或属于其他 round 退出 3。它不修改已写的 summary，只追加私有 `recovery.jsonl`，并更新公开 lifecycle 中的 `recovered_at`。

stdout 最后一行是一个 JSON 对象：`{"round", "status": "finished|stopped|interrupted|refused|recovered", "exit_code", "reason", "slots": [{scenario_id, repetition, attempt_id, status}]}`。不含凭据、原始遥测或报告正文。

## 3. 接缝（`scripts/eval_live_lab.py`）

全部时间为带 UTC 偏移的 `datetime`。超时与预算只按 `monotonic()` 计算，不使用跨时钟容差。

```python
class LiveClock(Protocol):
    def now(self) -> datetime: ...           # UTC
    def monotonic(self) -> float: ...
    def sleep(self, seconds: float) -> None: ...   # 假时钟：同时推进 now 与 monotonic

@dataclass(frozen=True)
class FlagConfig:                            # flagd-config ConfigMap 的 demo.flagd.json
    raw: bytes; uid: str; resource_version: str

class FlagStore(Protocol):
    def read(self) -> FlagConfig: ...        # 失败抛 LabError("FLAG_READ_FAILED")
    def replace(self, current: FlagConfig, new_raw: bytes, *, fence: str | None) -> FlagConfig: ...

class FlagResolver(Protocol):                # flagd 守护进程解析，不代表应用已生效
    def resolve(self, flag: str, method: str, timeout: float) -> str | None: ...  # variant 名；错误/未知为 None

@dataclass(frozen=True)
class ObservedSpan:
    trace_id: str; span_id: str; service: str; operation: str
    start: datetime; tags: Mapping[str, Any]

@dataclass(frozen=True)
class SpanQuery:  spans: tuple[ObservedSpan, ...]; complete: bool   # 截断/上限/错误时 complete=False
@dataclass(frozen=True)
class CallCount:  value: float | None; complete: bool

class TelemetryProbe(Protocol):              # 独立观察，不经产品工具
    def spans(self, service: str, start: datetime, end: datetime, timeout: float) -> SpanQuery: ...
    def checkout_calls(self, start: datetime, end: datetime, timeout: float) -> CallCount: ...

class LabIdentity(Protocol):
    def verify(self) -> tuple[str, ...]: ...  # 空元组 = 身份已确认；否则为失败码

class PortProbe(Protocol):
    def in_use(self, port: int) -> bool: ...  # 127.0.0.1 上是否已有监听者

@dataclass(frozen=True)
class PgInstance: directory: Path; port: int; owner_dsn: str; runtime_dsn: str
class PgLauncher(Protocol):
    def start(self, directory: Path, port: int) -> PgInstance: ...  # 新建目录、initdb、启动、迁移
    def stop(self, instance: PgInstance) -> None: ...               # 只停本实例

class BalanceReader(Protocol):
    def balance(self) -> Decimal | None: ...  # DeepSeek 余额（CNY）；未知为 None

class Investigator(Protocol):
    def investigate(self, request: InvestigationRequest) -> InvestigationResult: ...

@dataclass(frozen=True)
class LiveDeps:
    clock: LiveClock; flags: FlagStore; flagd: FlagResolver; telemetry: TelemetryProbe
    identity: LabIdentity; ports: PortProbe; pg: PgLauncher; balance: BalanceReader
    investigator: Investigator
    ledger_root: Path      # 真实值：git-common-dir 的上级目录/tmp/lab-ledgers
    evidence_root: Path    # 真实值：docs/evidence/eval
    recording_root: Path   # 真实值：<worktree>/tmp/eval
    pg_root: Path          # 真实值：<worktree>/tmp/eval-live
    scenarios_root: Path   # 真实值：evals/scenarios
    flagd_url: str
```

- `FlagStore.replace` 是 CAS：只有当前对象的 uid、resourceVersion、raw 都等于 `current` 时才写入 `new_raw`。`fence` 非 None 时还要写入唯一注解，保证 resourceVersion 前进。返回写后的对象。条件不符抛 `LabError("FLAG_CAS_CONFLICT")`，结果未知抛 `LabError("FLAG_WRITE_UNKNOWN")`。驱动在每次 replace 后另行 `read()` 并核对 SHA，不把 replace 的返回值当作证据。
- `Investigator` 封装真实 Run：建 incident、运行 runner、录制、trace 回读。测试用假实现代替它，从而离线测试生命周期、计分与输出；真实实现的接线规则见第 8 节。
  - `InvestigationRequest(attempt: AttemptRecord, spec: ScenarioSpec, pg: PgInstance, recording_dir: Path, ledger_dir: Path, round_id: str, pre_observation: PreObservation, manifest: Manifest)`。
  - `InvestigationResult(product_run_id, outcome: IncidentOutcome | None, report: ReportCapture | None, recording_ref: ArtifactRef | None, recording_error: str | None, delivered, authorized, delivery_records: Mapping[str, bytes], traces: tuple[TraceRecord, ...], submitted_at, started_at, ended_at, usage: Mapping[str, Any], error: str | None)`。`error` 只放固定码，不放异常正文。
- `class LabError(Exception)` 的 `.code` 与 `str()` 都只是固定码。`class Interrupted(BaseException)`：真实 `main` 把 SIGINT/SIGTERM 转成它；测试可以在任意假对象里抛出它，模拟中途退出。`KeyboardInterrupt` 按同样方式处理。

## 4. flag 白名单与 `kind_lab.py fault`

`FAULT_FLAGS`（两个模块共用一份）：

| flag | 故障 variant | 正常 variant |
|---|---|---|
| `paymentFailure` | `100%` | `off` |
| `productCatalogFailure` | `on` | `off` |
| `cartFailure` | `on` | `off` |

- 场景 `answer.json` 的 `injected.flag/variant` 必须正好命中白名单中的一行，否则该轮在预检阶段退出 2（`FAULT_NOT_WHITELISTED`）。normal 场景不注入。
- `kind_lab.mutate_flags(raw, variant, expected, *, flag=FLAG)`：flag 不在白名单、配置中没有该 flag、`variant` 不在其 `variants` 中、`defaultVariant != expected` 时都拒绝（`SystemExit`）。只改该 flag 的 `defaultVariant`，其余字节语义不变。`flag` 默认 `paymentFailure`，原调用不受影响。
- `kind_lab.py fault inject|restore [--experiment-id X] [--flag F]`：`--flag` 只接受白名单值，默认 `paymentFailure`，variant 由白名单决定，不提供任意 `--variant`。history 文件名与「回读验证后才写快照、恢复前核对注入快照」的语义保持不变；`fault-log.jsonl` 每行增加 `flag`。同一 experiment 已有 history 时仍拒绝再次注入。
- flagd 解析方法从实际 ConfigMap 中该 flag 的 variant 值类型推导：bool → `Boolean`，数值 → `Float`，字符串 → `String`。无法推导时抛 `LabError("FLAGD_METHOD_UNCONFIRMED")`。

## 5. 占用与恢复意图协议（A3、U2）

共享目录 `ledger_root` 沿用 F11 的同一组文件和 flock，不另起锁：`occupation.json`、`.occupation.guard`、`.fault.guard`、`.active-*.json`、`fault-active.json`、`run-starts.jsonl`。

**占用。** 预检要求 `occupation.json` 存在，且 `(holder, batch)` 等于参数、`expected_end` 晚于 `now()`；否则退出 3，原因码分别为 `LAB_RESERVATION_REQUIRED`、`LAB_OCCUPIED`、`LAB_RESERVATION_EXPIRED`。驱动不覆盖、不续期、不删除预约。整轮期间存在一个 `.active-<uuid>.json`（`{holder, batch, pid, round}`），它在占用 guard 内创建，进程正常或异常结束时删除；硬终止时残留，交人工处理。轮开始前按 F11 格式向 `run-starts.jsonl` 追加一行 `run_started`（round=R）；同名 round 已存在时退出 3（`ROUND_ALREADY_STARTED`）。

**fault guard。** 每个 attempt（normal 也一样）从 setup 开始到收尾结束，都非阻塞地持有 `.fault.guard`。被占时：预检阶段退出 3，attempt 内记 `setup_failed`，原因码 `LAB_OPERATION_IN_PROGRESS`。锁顺序固定：先在 occupation guard 内短暂检查，释放后再取 fault guard；持有 fault guard 时不再取 occupation guard。

**恢复意图 `fault-active.json`。** 在任何 ConfigMap 写入之前，于 fault guard 内独占创建（mode 600）。它与 F11 的 `restore_fault` 兼容，F11 的恢复逻辑能识别它：

```text
F11 兼容键: ledger(attempt 私有目录绝对路径), flag, normal, variant,
            original(原文本), injected(预期注入文本), planned_at, planned_restore_at=null
本驱动键:   owner="eval-live-1", round, attempt_id, holder, batch,
            configmap_uid, resource_version_before, original_sha256, injected_sha256,
            phase ∈ {intent, injected, restored_unwashed}
```

- 预检或 attempt setup 时已存在 `fault-active.json`（无论属于谁）：拒绝（`FAULT_RECOVERY_REQUIRED`）。
- 注入：先读取并核对全部白名单 flag 都处于正常态（ConfigMap 的 `defaultVariant` 和 flagd 解析都要满足，否则 `FLAGS_NOT_NORMAL`），再写意图（phase=intent），然后 CAS `replace(current, injected)`，再 `read()` 核对 SHA = `injected_sha256`，通过后把 phase 改为 injected，然后等待 flagd 解析为故障 variant。写入成功但回读失败、或结果未知时，意图已经落盘，直接走恢复。
- 恢复（幂等，正常结束、异常、超时、`Interrupted` 都会执行）：
  1. 状态的 `round/attempt_id` 必须属于当前调用，否则 `FAULT_BELONGS_TO_ANOTHER_RUN`，不写入。
  2. 读取 live ConfigMap。uid 不等于 `configmap_uid` 时报 `FLAG_OBJECT_REPLACED` 并停止。
  3. live = injected：以当前版本为条件 CAS 写回 original。live = original：做一次带 fence 的 CAS（排除迟到的注入提交），内容不变。其他值：`FLAGS_CHANGED_REFUSE_RESTORE`，不覆盖他人改动，保留状态。
  4. 回读 SHA = `original_sha256`，flagd 解析回正常 variant（≤600 s），然后把 phase 改为 restored_unwashed，执行洗净（第 6 节，≤600 s）。
  5. 洗净通过后才删除 `fault-active.json`。任一步失败都保留文件，本轮停止并退出 4，不释放占用。
- 传播等待：每 5 s 轮询一次，单次探测 ≤5 s，总预算 ≤600 s（monotonic）。超时原因码 `FLAG_PROPAGATION_UNCONFIRMED`。
- 硬终止不承诺执行 finally；保留的状态由 `--recover` 或 F11 的恢复逻辑处理。

## 6. 预观察门槛（U1）

```python
@dataclass(frozen=True)
class PreObservation:
    kind: Literal["fault", "normal"]
    passed: bool
    reason: str | None                   # 失败码
    flag_confirmed_at: datetime | None   # flagd 首次解析为目标 variant 的响应时刻
    window: Window | None                # 实际判定所用的窗口
    matched: tuple[ObservedSpan, ...]    # fault：命中签名的错误 span；normal：恒为空
    checkout_calls: float | None
    probes: int

def fault_gate(signature: SignatureRule, *, confirmed_at: datetime, deps: LiveDeps,
               budget_seconds=600, poll_seconds=5, probe_timeout=5) -> PreObservation
def normal_window(signatures: Sequence[SignatureRule], *, not_before: datetime, deps: LiveDeps,
                  window_seconds=300, budget_seconds=600, probe_timeout=5) -> PreObservation
```

- 错误判定只看展示标签：`error is True`，或 `otel.status_code == "ERROR"`，或 `rpc.grpc.status_code` 不是 `0`/`None`，或 `http.status_code` 以 5 开头。与 gate 的 `_error_by_tags` 相同。
- 签名匹配要求 `(service, operation)` 与某个 matcher 精确相等，不做后缀匹配。
- **fault**：每轮查询的窗口是 `[confirmed_at, now()]`。通过条件：至少一个 span 命中签名、判为错误、`start` 不早于 `confirmed_at`；同一窗口内 `checkout_calls > 0`；两次查询的 `complete` 都为真。不完整的一轮查询既不算通过也不算失败，继续轮询；预算耗尽记 `FAULT_NOT_OBSERVED`。
- **normal**：全部白名单 flag 确认正常后，窗口从 `not_before` 开始，等满 `window_seconds` 再查询 `[start, start+300s]`。通过条件：`checkout_calls > 0`，三类故障签名的错误 span 命中数为 0，所有查询 `complete`。窗口有命中或数据不完整时，从当前时刻开新窗口重来；预算耗尽记 `NORMAL_WINDOW_UNCONFIRMED`。normal 场景 setup 与故障恢复后的洗净都用这个函数。
- **提交新鲜度**：fault 场景创建 Run 后，`submitted_at - 300s` 不得晚于最新命中 span 的 `start`，否则 `PRE_OBSERVATION_STALE`（setup_failed）。normal 场景要求 `submitted_at - window.end ≤ 300s`。
- 原始探测结果只写私有 ledger 的 `pre-observation.json`，不进入模型输入。公开索引只记计数、span/trace ID 和窗口。门槛只证明这一有限目录，不构成全系统健康承诺。

## 7. attempt 状态与计分（U3、P10）

每个槽位依次经过：

```text
journal(started) -> setup(occupation, identity, guard, flags_baseline, pg,
                          inject, propagation, pre_observation, submit)
                 -> investigate -> closure(restore, propagation, wash, pg_stop)
                 -> freeze(ledger, summary)
```

- **journal**：setup 之前用 `write_attempt` 写入 `runs/<attempt_id>/attempt.json`，`started=True`、`status="unknown"`、`product_run_id=None`。没有产品 Run 的 attempt 也占这个槽。
- **ran**：Investigator 正常返回、`error is None`、录制 `record()` 成功（完整包），且收尾全部通过。产品 Outcome 是 handoff、gate 不过都不影响 ran；质量由 `evaluate()` 判定。
- **setup_failed**：setup 任一步失败，此时尚未调用 Investigator。若已经注入，仍执行完整的收尾。
- **aborted**：已经调用 Investigator，但它抛异常、返回 `error`、录制不完整（`RecordingError` 或 `recording_error` 非 None），或调查后的收尾失败。保留 Investigator 返回的原始 Outcome，不伪造 handoff。
- **unknown**：驱动从不写这个状态。summary 缺失时由 loader 合成。
- **not_started**：恢复/洗净失败、证据写入失败或收到 `Interrupted` 之后剩下的槽位。不写 journal，只在 lifecycle 中单列，原因码 `ROUND_STOPPED`。失败不重抽、不补跑替换。
- 每个已开始的 attempt 都用 `write_summary` 写出一份 summary，失败的也写。`evaluate()` 推导的 failure_class/reasons 与 lifecycle 中的 `stage`/`reason` 并列保存。setup 或收尾失败照常继续下一槽；第 2 节退出 4/5/130 所列情况才停轮。
- PR-C 不调用 judge：`judge=None`，因此 `overall` 恒为假（`JUDGE_NOT_RUN` 或 `skipped`）。驱动验收不要求质量通过，也不翻转 `passes`。

## 8. 产品接缝与录制接线（A5、A6）

`opspilot.investigation.runner.InvestigationRunner` 新增两个字段，默认 None，此时行为与现在逐字节相同：

```python
capture_delivery: Callable[[Mapping[str, Any], Mapping[str, Any]], None] | None = None
attempt_wrapper: Callable[[Lease, InvestigationInput, ReadOnlyToolExecutor,
                           Callable[[], RunnerOutcome]], RunnerOutcome] | None = None
```

- `capture_delivery` 原样传给每个 `InvestigationLoop`。
- `attempt_wrapper` 在每次 **已领取** 的 attempt 中最多调用一次。调用时机：executor 已按真实 lease/input 构造并通过 run_id 核对之后，pending 工具补跑、任何模型或工具调用之前。它必须调用 `proceed()` 至多一次，并返回 `proceed()` 的结果。`proceed()` 执行原有的剩余流程，fence、补跑、发布与 handoff 都不变。wrapper 自己抛出的异常按现有「executor/模型异常」路径处理：lease 保持到过期，不另行领取。
- `opspilot.tools.otel_demo` 公开 `OtelDemoRegistrations(tools: tuple[ToolRegistration, ...], targets: tuple[RegisteredTarget, ...])`（frozen）与 `otel_demo_registrations(config=None) -> OtelDemoRegistrations`。executor factory 使用同一份来源。DTO 只含 `credential_ref` 句柄，不含凭据。
- 真实 Investigator 的顺序：建 incident 与 Run → `runner.resume()` → wrapper 拿到真实 scope → `ToolSnapshot(tools, targets, executor.scope)` → `bind_tool_snapshots()` → 进入 `record()`。录制内依次接上 `hooks.input`（原始业务输入，不含答案/flag/场景 ID）、`hooks.model`、`DeepSeekClient.capture_http = hooks.http`、`hooks.tools`、`capture_delivery = hooks.delivery`，然后调用 `proceed()`，最后 `hooks.finish(clock.now())`。不预测 epoch，也不在已开始的 record 中补快照。每个真实 model 事件恰好有一个 http 子事件。
- 录制目录 `<recording_root>/eval-live-<R>-<attempt_id>/`。父目录不存在时以 700 权限创建；目标已存在为 `OUTPUT_EXISTS`（aborted）。
- 上限沿用产品冻结默认值：100 次模型请求、单次 360 s、7200 s backstop、8 MiB/2 MiB。setup/恢复耗时单独记录，不占 Run 额度。

`opspilot.evaluation.evidence_from_recording(recording: Recording, *, outcome: IncidentOutcome | None) -> LiveEvidence(delivered, authorized, delivery_records)` 是纯函数，对已核验的录制包求值：`delivered` 以 evidence_id 为键，只有当 delivery 的原样工具消息实际出现在后续物理模型请求中时 `delivered=True`（与 #234/B2 第 152 行同一规则）；`authorized` 是 Run 输入中的 time_scope_ref → 授权框；`delivery_records[eid]` 是该 DeliveredRecord 的 canonical 字节。

## 9. #234 两项（A7）

- **P1**：`validate_recording` 只从 delivery 的 `parent` 推出交付的 evidence ID。parent 是 tool 时取该 tool 的 evidence；parent 是 refusal 时取 refusal 的父 tool。不再按 view hash 扩展集合。缓存 delivery（`parent=None`）只有在 view 能唯一对应一个可核验身份时才算交付；有两个或以上候选时报 `IDENTITY_MISMATCH`。必须有的反例：两个 tool 事件 view 字节相同、evidence ID 不同，只交付其中一个，outcome 却引用两个 → `IDENTITY_MISMATCH`。对照：只引用已交付的那个则通过。缓存歧义 → 拒绝；缓存唯一 → 接受。
- **P2**：`replay()` 成功时返回的 `id_map`，取自按返回 ref 重新 `load_recording` 的新包的 `initial_state.replay_source.id_map`，不读 `session.id_map`。driver 在运行中改写 `session.id_map` 不影响返回值。录制失败的路径无法核验，返回 `{}`。
- 旧录制包的 hash 不变。无法证明身份的包按新规则拒绝；历史摘要仍只读可展示。

## 10. 输出布局（A8、A9）

公开，入库：

```text
docs/evidence/eval/<R>/runs/<attempt_id>/attempt.json   # write_attempt，opspilot-eval-attempt-1
docs/evidence/eval/<R>/runs/<attempt_id>/summary.json   # write_summary，opspilot-eval-summary-1
docs/evidence/eval/<R>/lifecycle.json                   # opspilot-eval-live-lifecycle-1，脱敏索引
docs/evidence/eval/<R>/aggregate.json                   # write_round，opspilot-eval-aggregate-1
```

- `<evidence_root>/<R>/` 在轮开始时已存在 → 退出 3（`ROUND_EXISTS`）。所有文件都独占创建，不覆盖。
- summary 的 `EvaluationCapture` 字段：manifest（见 Q1）、spec/answer（仓库已公开内容）、attempt、outcome、report、delivered、authorized、`traces`（`TraceRecord`，role=`investigation`、required=True，含 project/url/read_back/checked_at）、`delivery_refs`、`recording_ref`、`ledger_ref`、started_at/ended_at、usage（模型请求、token、工具次数与秒数）、cost（`{balance_before_cny, balance_after_cny, delta_cny, status: measured|unknown, shared_key_possible: true}`）。
- `lifecycle.json` 每个槽位：`scenario_id, scenario_version, repetition, attempt_id|null, status, stage, reason, product_run_id, summary{path,sha256}|null, fault{flag, variant, original_sha256, injected_sha256, effective_at, restored_sha256, normal_confirmed_at, washed_at}|null, pre_observation{kind, window, matched_span_ids, trace_ids, checkout_calls, probes}|null, timings{started_at, ended_at, setup_s, investigation_s, closure_s}`。顶层：`round, manifest_hash, command`（不含环境值）、`round_status`、`retained_fault`、`recovered_at`、每轮余额。
- 私有，不入库：`<ledger_root>/<R>/round.jsonl`；`<ledger_root>/<R>/<attempt_id>/` 下的 `lifecycle.jsonl`（每行 `seq, at, mono, stage, event, detail`）、`flags-original.json`、`flags-injected.json`、`pre-observation.json`、`wash.json`、`deliveries/<sha256>.json`、`recovery.jsonl`；录制包在 `<recording_root>/eval-live-<R>-<attempt_id>/`。文件 600，目录 700。
- ArtifactRef 路径：`recording_ref.path="recording.json"`；`ledger_ref.path="lifecycle.jsonl"`（summary 写入前冻结，之后的事件写 `recovery.jsonl`）；`delivery_refs[eid].path="deliveries/<sha256>.json"`。`attempt_resolver(R, attempt_id, *, ledger_root, recording_root) -> Resolver`：`recording.json` 与 `blobs/*` 解析到录制目录，其余解析到 attempt 私有目录。外置原件缺失时 loader 报 `EVIDENCE_UNAVAILABLE`，不能称可复核通过。

## 11. 冲突时 fail-closed（A4）

- lab 身份：`LabIdentity.verify()` 在预检和每个 attempt setup 时各调用一次，返回非空即拒绝（预检退出 3，attempt 内 setup_failed，`LAB_IDENTITY_UNCONFIRMED`）。真实实现核对三项：kube context 为 `kind-opspilot-m1`；Prometheus 拒绝匿名请求、接受 lab 账号；Jaeger 的服务列表含 `checkout`。18080/19090/16686 由预期 lab 占用属正常，驱动不探测、不抢占这些端口。
- PG：`ports.in_use(pg_port)` 为真时，预检退出 3，attempt 内 setup_failed（`PG_PORT_IN_USE`），不换端口、不杀进程。每个 attempt 的目录 `<pg_root>/<R>/<attempt_id>/pg` 必须不存在（否则 `PG_DIRECTORY_EXISTS`）。启动失败记 `PG_START_FAILED`。收尾只 `stop` 本 attempt 启动的实例；停止失败记入 lifecycle（`PG_STOP_FAILED`），不影响 lab 恢复判定。迁移只用 `owner_dsn` 对新库执行，Run 使用 `runtime_dsn` 的限权角色。
- 共享状态：遇到异主的 `occupation.json`/`fault-active.json` 一律拒绝，不覆写、不偷占过期预约，也不强制回滚他人的 flag 改动。

## 12. 只能 live 验证的项

真实适配器（kubectl CAS、flagd HTTP、Prometheus/Jaeger 查询、PG 启停与迁移、端口探测、DeepSeek 余额、LangSmith project/read-back）以及 C A11 的真实 Run，只能由 lead 排期 live 执行。离线测试不覆盖这些，也不应声称覆盖。新增两个 flag 的实际传播延迟与 flagd 方法尚未实测。

## 13. 待 lead 裁决（实现前按推荐理解）

| # | 歧义（依据） | 推荐 |
|---|---|---|
| Q1 | B2 的 `bind_tool_snapshots` 把含 run_id/epoch 的 per-Run scope 写进 manifest 的 registry/permissions 维度，返回每个 attempt 各自的 manifest（`tool_provenance.py:136–147`）。但 P10 要求 setup 前就写 journal（那时还没有 scope）；B1 `_round` 又要求 journal、summary 和轮 manifest 的 hash 一致（`storage.py:_round/_binding`）。三者无法同时满足 | 用 **轮 manifest** 建 journal/summary/attempt：registry = 静态注册 DTO 摘要（hash），permissions = scope 策略（去掉 run 身份字段）的摘要（hash）。绑定后的 per-attempt `source_manifest` 只保存在录制包内，经 `recording_ref` 关联。这偏离 B2「调用方须以它建立源 attempt」的字面要求；替代做法是改 B1 loader，超出 PR-C 范围 |
| Q2 | A5 允许「分离准备/执行」，但没有规定接缝形状 | 采用第 8 节的 `attempt_wrapper(lease, input, executor, proceed)`：保持 runner 单一流程，领取与 fence 不动。替代方案是把 `resume` 拆成 prepare/execute 两个公开方法，改动面更大 |
| Q3 | P2 要求「setup 失败无产品 Run 时保留评测 attempt root trace」，但目前没有发出这种 trace 的机制 | r1 不发 attempt root trace。setup_failed 的 summary `traces=()`，在 `SETUP_FAILED` 之外另带 `EVIDENCE_INCOMPLETE`；后续另开 issue |
| Q4 | A3 的「claim/check/release」由谁执行 | D15 的会话空闲确认后，由 lead 用 F11 CLI claim；驱动只 check，从不 release |
| Q5 | 新增两个 flag 的 flagd 方法未确认（C U2 依据） | 按第 4 节从 variant 值类型推导，不硬编码 |
| Q6 | A4 的「未知监听者」如何识别：colima/kind 端口转发的进程身份未实测 | 改用第 11 节的 HTTP/凭据/context 身份核对；只对 PG 端口做监听探测 |
| Q7 | #234 P2 没有规定失败路径的 `id_map` | 失败路径返回 `{}`，因为无法从已核验包推出 |
| Q8 | U1 中「调查窗内」的含义 | 按第 6 节解释为提交新鲜度：fault 要求 `submitted_at - 300s ≤ 最新命中 span`；Run 授权框仍沿 D12 |
| Q9 | PG 按 attempt 还是按轮启动 | 按 attempt（A4 写的是「目录非本 attempt 所有即失败」）；端口在 attempt 之间串行复用 |
