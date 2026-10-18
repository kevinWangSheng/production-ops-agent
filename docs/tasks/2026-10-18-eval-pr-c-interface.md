# 评测 PR-C 公开接口（r3）：live kind/OTel 场景驱动

- 依据：[PR-C 合同 r1](2026-10-18-eval-pr-c-contract.md)（下称 C，U1–U3、A1–A11 均按 B 采纳）；[套件合同](2026-10-12-eval-suite-contract.md) D3/D4/D5/D10/D15、P2/P10/P12/P13；[PR-B2 接口](2026-10-14-eval-pr-b2-interface.md)；[PR-A 接口](2026-10-13-eval-pr-a-interface.md)；issue #234。
- 用途：供未参与实现的测试作者编写验收/合同测试。只写可观察行为与接缝，不写实现。第 13 节记录 lead 的 r2 裁决，第 14 节是 r3 实现对账（只收紧或补全，不放宽）。
- 离线原则：除真实 kind/flagd/Prometheus/Jaeger/PG/DeepSeek/LangSmith 调用本身外，全部行为都能用第 3 节的假对象离线测试。真实适配器只由 `real_deps()` 构造，测试不需要也不应调用它。

## 1. 模块

| 模块 | 内容 |
|---|---|
| `scripts/eval_live.py` | 唯一公开入口 `main(argv, *, deps: LiveDeps | None = None) -> int`；为写假 Investigator 公开 DTO `InvestigationRequest/InvestigationResult`；读取辅助 `attempt_resolver(...)`；轮策略模板 `template_scope(config=None)`（r5，第 3 节）。`run_round/recover_round/plan_slots/build_manifest/RoundConfig/RoundResult/SlotResult` 是内部实现，测试不应依赖其构造字段（r3 测试作者澄清 4） |
| `scripts/eval_live_lab.py` | flag 白名单、恢复意图与占用协议、预观察门槛、接缝 Protocol 与 DTO、`LabError`、`Interrupted`、真实适配器 |
| `scripts/eval_live_investigator.py` | 真实 Investigator（live only）；`real_deps()` 在 `scripts/eval_live.py` |
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
- `--dry-run`：只读场景目录，不调用任何接缝，不写文件，退出 0。stdout 为一个 JSON 对象，字段固定为 `round`（str）、`status`（恒为 `"dry_run"`）、`slots`（按执行顺序的数组，每项 `scenario_id`、`repetition`（从 1 起的 int）、`kind`（`fault|normal`）、`flag`（fault 为白名单 flag 名，normal 为 null））、`evidence`（`"docs/evidence/eval/<R>/"`）、`ledger`（`"tmp/lab-ledgers/<R>/"`）。
- 槽位顺序固定：先按 scenario_id 排序，再按 repetition 1..N（场景为主序）。
- 环境前提（真实运行时）：`OPSPILOT_TRACE=lab`，`M0_ENV_FILE` 提供 DeepSeek/LangSmith 凭据（读取方式同既有脚本，不打印），调查侧 Prometheus 账号变量已载入；独立观察使用 lab 账号（`kind_lab.lab_authorization`）。缺失时退出 3，原因码 `ENV_UNCONFIRMED`。这些检查经 `LiveDeps.env` 接缝（第 3 节）完成；注入完整 `LiveDeps` 时驱动自己不读 `M0_ENV_FILE` 或任何环境文件。

| 退出码 | 含义 |
|---|---|
| 0 | 全部计划槽位均已开始，且都是 `ran`；lab 已恢复且洗净 |
| 1 | 全部计划槽位均已开始，至少一个 `setup_failed`/`aborted`，lab 已恢复且洗净；或 setup 中发现异主恢复意图（`LAB_FAULT_FOREIGN`，第 5 节）而停轮，后续槽位 `not_started` |
| 2 | 参数错误（argparse、`--parallel≠1`、未知场景、非法 round/URL/N），未写任何文件 |
| 3 | 预检拒绝，未开始任何 attempt（不写 journal）：占用缺失/异主/过期、已有 `fault-active.json`、fault guard 被占、lab 身份不明、PG 端口已占用、flag 不在正常态、round 证据目录已存在、环境前提缺失 |
| 4 | 恢复或洗净失败：整轮停止，`fault-active.json` 保留，后续槽位记 `not_started` |
| 5 | 证据写入失败（journal/summary/lifecycle/aggregate，`--probe` 的 probe.json，`--recover` 的 recovery-*.json），lab 已恢复；整轮停止。r11：任何公开证据写入的 OSError 都转为此码，stdout 最后一行 JSON 照常输出（`reason="EVIDENCE_WRITE_FAILED"`），不以异常退出 |
| 130 | 收到 SIGINT/SIGTERM，且当前 attempt 已恢复、洗净；后续槽位 `not_started`。恢复失败时改为 4 |

退出码优先级（r13）：多个停止条件同时出现时取 **4 > 5 > 130**，即恢复/洗净失败（保留故障）压过证据写入失败，证据写入失败压过中断；`--probe` 同样适用。例如洗净失败后 summary 又写不出仍为 4；中断后 summary、aggregate 或 lifecycle 写不出为 5。被压过的条件仍记录在 lifecycle / 私有 ledger 中（写得出时）。

`--recover`：只处理 `round == R` 的保留故障状态，按第 5 节恢复规则执行并洗净。成功删除状态后退出 0；仍失败退出 4；状态不存在或属于其他 round 退出 3。它不修改已写的 summary 与 lifecycle，只追加私有 `recovery.jsonl`，并独占新建公开 `recovery-<UTC>.json`（`opspilot-eval-live-recovery-1`：round、attempt_id、recovered_at）。r10：`--recover` 总是只构造 lab 接缝（与 `--probe` 相同：flag store、flagd、telemetry、identity、占用/guard），不读 `M0_ENV_FILE`、不需要模型或 LangSmith 凭据，因此探测轮与正式轮遗留的故障都用同一条命令恢复；不接受与 `--probe` 组合。开始时先做 `env` 检查（只要求 lab Prometheus 账号，洗净要用），不满足退出 3（`ENV_UNCONFIRMED`），不写入任何东西。r11：同一秒内已有同名记录时依次独占创建 `recovery-<UTC>-2.json` … `-99`，绝不覆盖；lab 已恢复但公开记录写不出时退出 5（`EVIDENCE_WRITE_FAILED`），`fault-active.json` 已按恢复规则删除。

`--probe`（r8，A11 生命周期探测）：`--mode live --probe --round R --scenario ID [...] --n 1 --parallel 1 --holder H --batch B --flagd-url URL`，不需要 `--pg-port`。每个场景执行一次真实 lab 生命周期，不建产品 Run、不调模型、不启 PG、不查余额、不建 LangSmith 项目，也不写 journal/summary/aggregate（不冒充 attempt）：
- 预检：`env`（只要求 lab Prometheus 账号可用，不读 `M0_ENV_FILE`）→ `ROUND_EXISTS` → 占用 → 已有 `fault-active.json` → fault guard → identity → 全部 flag 正常；拒绝退出 3。通过后照常追加 `run-starts.jsonl`。
- fault 场景：持有 fault guard，按第 5 节写意图并注入（attempt_id 固定为 `probe-<scenario_id>`），等 flagd 生效，跑第 6 节 `fault_gate`，然后无论门槛结果如何都按第 5 节恢复并洗净。normal 场景：确认全部 flag 正常后跑 `normal_window`，不写 ConfigMap。
- 异主状态（r10）：预检之后某场景开始时出现不属于本场景的 `fault-active.json`（或注入入口检查时出现），与正式轮相同按第 5 节处理：ConfigMap 零写入、不改动该文件、该场景 reason=`LAB_FAULT_FOREIGN`，整轮停止，后续场景 `not_started`（`ROUND_STOPPED`），退出 1。
- 退出码：0 = 每个场景门槛通过且恢复、洗净完成；1 = 有场景门槛未通过但 lab 已恢复，或因 `LAB_FAULT_FOREIGN` 停轮；3 = 预检拒绝；4 = 恢复或洗净失败（保留 `fault-active.json`，后续场景不执行，可用 `--recover --round R` 恢复）；130 = 中断且已恢复。`--n` 只接受 1。
- 输出：私有 `<ledger_root>/<R>/probe-<scenario_id>/`（与 attempt 相同的 lifecycle.jsonl、flags-*.json、pre-observation.json、wash-*.json）；公开 `docs/evidence/eval/<R>/probe.json`（`opspilot-eval-live-probe-1`：每个场景的 scenario_id、kind、flag、gate{passed, reason, window, matched_span_ids, trace_ids, checkout_calls}、fault{original_sha256, injected_sha256, effective_at, restored, washed}、reason、closure_reason，以及 round_status、exit_code）。stdout 最后一行同第 2 节格式。

`--verify-replay`（r9，A11 零模型结构回放核验）：`--verify-replay --round R [--attempt ID]`，不需要也不接受 `--mode/--scenario/--n/--parallel/--pg-port/--probe/--dry-run`，`--holder/--batch/--flagd-url` 可省略。只读本地文件，不调用任何 `LiveDeps` 接缝，不发模型、网络、kubectl 或 PG 调用：
- 对 `docs/evidence/eval/<R>/runs/*/summary.json`（或 `--attempt` 指定的那一个）中 `capture.recording_ref` 非 null 的每个 attempt：按 `recording_ref.sha256` 对 `<recording_root>/eval-live-<R>-<attempt_id>/recording.json` 执行 `load_recording`（逐件核验），用 summary 的 attempt 与 `usage.epoch` 重做 `check_recording_binding`，再执行 `replay(mode="structural", investigator=None)`。
- 结构回放所用的 manifest 是 summary 中的轮 manifest，只把 `replay_fixture` 维度换成 `hash=<recording sha256>`；attempt 为 `mode="structural"`、`attempt_id=<原 id>-structural`、`product_run_id=None`、其余身份同原 attempt。
- 每项结果：`attempt_id, recording_sha256, loaded, binding_ok, structural_verified, error, event_counts{model,http,tool,delivery,refusal}`；任何一步失败以固定码填 `error`，不抛出。
- 退出码：0 = 至少一项且全部 `structural_verified`；1 = 有任一项失败；3 = 没有可核验的 summary（`NOTHING_TO_VERIFY`）或 round 证据目录不存在。stdout 输出一个 JSON 对象 `{"round","status":"verified|failed|refused","exit_code","results":[...]}`；另独占新建公开 `docs/evidence/eval/<R>/structural-replay.json`（`opspilot-eval-structural-replay-1`，同上内容），已存在则改写为 `structural-replay-<UTC>.json`；r10：再冲突时依次尝试 `structural-replay-<UTC>-2.json`、`-3` …，每次都是独占创建，绝不覆盖。仍无法写出（目录不可写或候选名用尽）时不抛异常：stdout 照常输出完整 JSON，另带 `"write_error": "EVIDENCE_WRITE_FAILED"`，退出码 5。

stdout 最后一行是一个 JSON 对象：`{"round", "status": "finished|stopped|interrupted|refused|recovered", "exit_code", "reason", "slots": [{scenario_id, repetition, attempt_id, status}]}`。不含凭据、原始遥测或报告正文。

## 3. 接缝（`scripts/eval_live_lab.py`）

全部时间为带 UTC 偏移的 `datetime`。超时与预算只按 `monotonic()` 计算，不使用跨时钟容差。

```python
class LiveClock(Protocol):
    def now(self) -> datetime: ...  # UTC
    def monotonic(self) -> float: ...
    def sleep(self, seconds: float) -> None: ...  # 假时钟：同时推进 now 与 monotonic


@dataclass(frozen=True)
class FlagConfig:  # flagd-config ConfigMap 的 demo.flagd.json
    raw: bytes
    uid: str
    resource_version: str
    fence: str | None = None  # r6：读到的 opspilot.dev/eval-live-restore-fence 注解值


class FlagStore(Protocol):
    def read(self) -> FlagConfig: ...  # 失败抛 LabError("FLAG_READ_FAILED")
    def replace(
        self, current: FlagConfig, new_raw: bytes, *, fence: str | None
    ) -> FlagConfig: ...


class FlagResolver(Protocol):  # flagd 守护进程解析，不代表应用已生效
    def resolve(
        self, flag: str, method: str, timeout: float
    ) -> str | None: ...  # variant 名；错误/未知为 None


@dataclass(frozen=True)
class ObservedSpan:
    trace_id: str
    span_id: str
    service: str
    operation: str
    start: datetime
    tags: Mapping[str, Any]


@dataclass(frozen=True)
class SpanQuery:
    spans: tuple[ObservedSpan, ...]
    complete: bool  # 截断/上限/错误时 complete=False


@dataclass(frozen=True)
class CallCount:
    value: float | None
    complete: bool


class TelemetryProbe(Protocol):  # 独立观察，不经产品工具
    def spans(
        self, service: str, start: datetime, end: datetime, timeout: float
    ) -> SpanQuery: ...
    def checkout_calls(
        self, start: datetime, end: datetime, timeout: float
    ) -> CallCount: ...
    # r13 可选：带阶段截止时刻的检索；实现了它的探针由门槛优先调用
    def spans_within(
        self,
        service: str,
        start: datetime,
        end: datetime,
        *,
        request_timeout: float,
        deadline: float,  # deadline 为 LiveDeps.clock.monotonic() 刻度
    ) -> SpanQuery: ...


class LabIdentity(Protocol):
    def verify(self) -> tuple[str, ...]: ...  # 空元组 = 身份已确认；否则为失败码


class PortProbe(Protocol):
    def in_use(self, port: int) -> bool: ...  # 127.0.0.1 上是否已有监听者


@dataclass(frozen=True)
class PgInstance:
    directory: Path
    port: int
    owner_dsn: str
    runtime_dsn: str
    web_dsn: str = ""


class PgLauncher(Protocol):
    def start(
        self, directory: Path, port: int
    ) -> PgInstance: ...  # 新建目录、initdb、启动、迁移
    def stop(self, instance: PgInstance) -> None: ...  # 只停本实例


class BalanceReader(Protocol):
    def balance(self) -> Decimal | None: ...  # DeepSeek 余额（CNY）；未知为 None


class Investigator(Protocol):
    def investigate(self, request: InvestigationRequest) -> InvestigationResult: ...


@dataclass(frozen=True)
class LiveDeps:
    clock: LiveClock
    flags: FlagStore
    flagd: FlagResolver
    telemetry: TelemetryProbe
    identity: LabIdentity
    ports: PortProbe
    pg: PgLauncher
    balance: BalanceReader
    investigator: Investigator
    ledger_root: Path  # 真实值：git-common-dir 的上级目录/tmp/lab-ledgers
    evidence_root: Path  # 真实值：docs/evidence/eval
    recording_root: Path  # 真实值：<worktree>/tmp/eval
    pg_root: Path  # 真实值：<worktree>/tmp/eval-live
    scenarios_root: Path  # 真实值：evals/scenarios
    flagd_url: str
    env: EnvChecker  # r3 测试作者澄清 6
    provenance: ProvenanceReader  # r5


class EnvChecker(Protocol):
    def check(self) -> str | None: ...  # None = 环境前提满足；否则 "ENV_UNCONFIRMED"


@dataclass(frozen=True)
class Provenance:
    commit: str | None  # 代码版本（git HEAD）；未知为 None
    clean: bool  # 已跟踪文件无未提交改动


class ProvenanceReader(Protocol):
    def read(self) -> Provenance: ...
```

- **来源版本（r5）**：驱动核心自己不启动任何子进程、不发网络请求；git commit 与是否干净只经 `LiveDeps.provenance` 读取，真实实现（调用 `git`）只在 `real_deps()` 中构造。每轮调用一次，时机为预检全部通过之后、`begin_round` 之前，结果用于轮 manifest 的 `code` 维度：`commit` 非 None 且 `clean` 为真时为 `version=<commit>`，否则为 `unknown`（带原因，不拒绝本轮，但会阻止基线完成）。它抛 `LabError(code)` 时整轮以退出 3、该 code 拒绝，不写 journal。预检拒绝的一轮不调用 `provenance`。场景、答案、投影、评估器与 kind 配置文件的 SHA256 由驱动直接读文件计算，不是子进程或网络调用。
- **轮策略模板（r5，公开）**：`scripts.eval_live.template_scope(config: OtelDemoConfig | None = None) -> QueryScope`，纯函数，不读环境、不发调用。返回值是 `otel_demo_executor_factory` 为一个新的非告警 Run 签发的 `QueryScope`，其中 Run 身份为占位值。
  - **策略部分**（`scope_policy_hash` 保留，决定轮 manifest 的 `permissions`）：`subject_kind="incident"`；`registry_revision`、`tool_registry_revision`（由 `otel_demo_registrations(config)` 构造的注册表求得）；`target_ids={TARGET_ID}`；`tool_names={metrics_range_query, traces_search}`；`max_operations`、`max_tool_seconds` 取 `QueryScope` 默认值；授权框长度 `window.end - window.start = OBSERVATION_SECONDS`（24 h）；`default_query_window` 为 None。
  - **Run 身份部分**（占位，必须替换，不进入 `scope_policy_hash`）：`scope_id`（真实值 `otel-demo:<run_id>:<epoch>`）、`subject_id`、`run_id`、`control_generation`、`global_suspension_generation`、`target_suspension_generation`、`window` 的起止时刻（长度保持不变）、`deadline`。
  - 轮 manifest 的两维即 `registry = live.registry_hash(otel_demo_registrations(config).tools, .targets)`、`permissions = live.scope_policy_hash(template_scope(config))`；测试替身用 `dataclasses.replace(template_scope(), <Run 身份字段>...)` 与 `otel_demo_registrations()` 构造快照，即可满足第 13 节最后一条。

- `EnvChecker.check()` 在预检第一步调用，非 None 即退出 3（`ENV_UNCONFIRMED`），不调用其他接缝。真实实现在 `real_deps()` 构造时读取 `M0_ENV_FILE`；注入的 `LiveDeps` 由调用方负责，驱动不读任何环境文件。
- **轮开始（r4）**：`Investigator` 可选实现 `begin_round(round_id: str) -> None`。驱动只在预检全部通过之后、追加 `run-starts.jsonl` 与写第一个 journal 之前调用它一次；未实现该方法的假 Investigator 视为无操作。它抛 `LabError(code)` 时整轮以退出 3、该 code 拒绝，不写 journal。真实实现在这里创建 LangSmith 项目并设置保留期；`real_deps()` 只读本地环境文件，不发起任何网络调用。因此预检拒绝的一轮对 LangSmith、DeepSeek 余额、PG 与 Investigator 全部零调用；预检本身只调用拒绝之前已经用到的接缝（`env` → 占用文件 → identity → port → flags/flagd）。

- `FlagStore.replace` 是 CAS：只有当前对象的 uid、resourceVersion、raw 都等于 `current` 时才写入 `new_raw`。`fence` 非 None 时还要写入唯一注解，保证 resourceVersion 前进。返回写后的对象。条件不符抛 `LabError("FLAG_CAS_CONFLICT")`，结果未知抛 `LabError("FLAG_WRITE_UNKNOWN")`。驱动在每次 replace 后另行 `read()` 并核对 SHA，不把 replace 的返回值当作证据。
- `Investigator` 封装真实 Run：建 incident、运行 runner、录制、trace 回读。测试用假实现代替它，从而离线测试生命周期、计分与输出；真实实现的接线规则见第 8 节。
  - `InvestigationRequest(attempt: AttemptRecord, spec: ScenarioSpec, pg: PgInstance, recording_dir: Path, ledger_dir: Path, round_id: str, pre_observation: PreObservation, manifest: Manifest, submit_not_after: datetime | None)`。
  - `InvestigationResult(product_run_id, outcome: IncidentOutcome | None, report: ReportCapture | None, recording_ref: ArtifactRef | None, recording_error: str | None, traces: tuple[TraceRecord, ...], submitted_at, started_at, ended_at, usage: Mapping[str, Any], error: str | None, delivered={}, authorized={}, delivery_records={})`；末三项被驱动忽略（见第 14 节）。`error` 只放固定码，不放异常正文。
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

- 预检时已存在 `fault-active.json`（无论属于谁）：退出 3（`FAULT_RECOVERY_REQUIRED`）。
- 预检通过后、某个 attempt 的 setup 中出现不属于本 attempt 的 `fault-active.json`（r3 测试作者澄清 7）：ConfigMap 零写入，不改动、不删除该文件，不调用 Investigator；本 attempt 记 `setup_failed`，原因 `LAB_FAULT_FOREIGN`；整轮停止，后续槽位 `not_started`，退出 1。退出 4 只用于本 attempt 自己的恢复或洗净失败。
- 注入：先读取并核对全部白名单 flag 都处于正常态（ConfigMap 的 `defaultVariant` 和 flagd 解析都要满足，否则 `FLAGS_NOT_NORMAL`），再写意图（phase=intent），然后 CAS `replace(current, injected)`，再 `read()` 核对 SHA = `injected_sha256`，通过后把 phase 改为 injected，然后等待 flagd 解析为故障 variant。写入成功但回读失败、或结果未知时，意图已经落盘，直接走恢复。
- 恢复（幂等，正常结束、异常、超时、`Interrupted` 都会执行）：
  1. 状态的 `round/attempt_id` 必须属于当前调用，否则 `FAULT_BELONGS_TO_ANOTHER_RUN`，不写入。
  2. 读取 live ConfigMap。uid 不等于 `configmap_uid` 时报 `FLAG_OBJECT_REPLACED` 并停止。
  3. live = injected：以当前版本为条件、带本次写入专属 fence 的 CAS 写回 original。live = original：做一次带 fence 的 CAS（排除迟到的注入提交），内容不变。（r7：驱动对 ConfigMap 的每一次写入——注入与两种恢复——都带一个新生成的、只属于这次写入的 fence 值。）其他值：`FLAGS_CHANGED_REFUSE_RESTORE`，不覆盖他人改动，保留状态。
  4. 回读 SHA = `original_sha256`，flagd 解析回正常 variant（≤600 s），然后把 phase 改为 restored_unwashed，执行洗净（第 6 节，≤600 s；预算含端点，恰好在 600 s 结束的窗口照常判定）。洗净失败的原因码就是 `normal_window` 的 `reason`（`NORMAL_WINDOW_UNCONFIRMED`），因此 `closure_reason="RESTORE_FAILED:NORMAL_WINDOW_UNCONFIRMED"`（r5 更正：实现曾用未写入接口的 `WASH_FAILED`）。
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
- **提交新鲜度**：fault 场景创建 Run 后，`submitted_at - 300s` 不得晚于最新命中 span 的 `start`，否则 `PRE_OBSERVATION_STALE`（setup_failed）。normal 场景要求 `submitted_at - window.end ≤ 300s`。核对发生在两处：(a) Investigator 在建 incident/Run 之前比较当前时刻与 `submit_not_after`，超时返回 `error="PRE_OBSERVATION_STALE"`，此时没有任何产品状态；(b) Investigator 返回后，驱动比较 `result.submitted_at` 与 `submit_not_after`，超时也判 stale，此时临时 PG 中可能已有该 incident/Run，且 Run 可能已经跑完。两种情况都是 setup_failed（即使已调用 Investigator）；(b) 时 `product_run_id` 记入 attempt 与私有 ledger，保留 Investigator 返回的真实 Outcome，不伪造 handoff。
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
- **setup_failed**：setup 任一步失败，此时尚未调用 Investigator；唯一例外是 `PRE_OBSERVATION_STALE`（第 6 节），它即使在调用 Investigator 之后发现也记 setup_failed，若已建产品 Run 则带 `product_run_id`。若已经注入，仍执行完整的收尾。
- **aborted**：已经调用 Investigator，但它抛异常、返回 `error`、录制不完整（`RecordingError` 或 `recording_error` 非 None），或调查后的收尾失败。保留 Investigator 返回的原始 Outcome，不伪造 handoff。
- **unknown**：只出现在 setup 之前写的 journal 中（`started=True, status="unknown"`）。最终 summary 的 attempt 状态与 lifecycle 的槽位状态绝不是 unknown；summary 缺失时由 loader 合成 unknown 失败。
- **not_started**：恢复/洗净失败、证据写入失败或收到 `Interrupted` 之后剩下的槽位。不写 journal，只在 lifecycle 中单列，原因码 `ROUND_STOPPED`。失败不重抽、不补跑替换。
- 每个已开始的 attempt 都用 `write_summary` 写出一份 summary，失败的也写。`evaluate()` 推导的 failure_class/reasons 与 lifecycle 中的 `stage`/`reason` 并列保存。setup 或收尾失败照常继续下一槽；第 2 节退出 4/5/130 所列情况才停轮。
- PR-C 不调用 judge：`judge=None`，因此 `overall` 恒为假（`JUDGE_NOT_RUN` 或 `skipped`）。驱动验收不要求质量通过，也不翻转 `passes`。

## 8. 产品接缝与录制接线（A5、A6）

`opspilot.investigation.runner.InvestigationRunner` 新增两个字段，默认 None，此时行为与现在逐字节相同。已领取 Run 上的默认等价性由既有 durable/loop-resume 测试证明（r3 测试作者澄清 3）：`tests/integration/test_m1_loop_resume_postgres.py` 与 `tests/integration/test_m1_otel_demo_contract_postgres.py`（共 37 例），以及 `test_m1_01_acceptance_postgres.py`、`test_m1_web_worker_postgres.py`、`test_m1_deadline_sweep_postgres.py`、`tests/acceptance/test_m1_04_alert_context.py`，均为 `M1_DURABLE_POSTGRES=1` opt-in：

```python
capture_delivery: Callable[[Mapping[str, Any], Mapping[str, Any]], None] | None = None
attempt_wrapper: (
    Callable[
        [Lease, InvestigationInput, ReadOnlyToolExecutor, Callable[[], RunnerOutcome]],
        RunnerOutcome,
    ]
    | None
) = None
```

- `capture_delivery` 原样传给每个 `InvestigationLoop`。
- `attempt_wrapper` 在每次 **已领取** 的 attempt 中最多调用一次。调用时机：executor 已按真实 lease/input 构造并通过 run_id 核对之后，pending 工具补跑、任何模型或工具调用之前。它必须调用 `proceed()` 至多一次，并返回 `proceed()` 的结果。`proceed()` 执行原有的剩余流程，fence、补跑、发布与 handoff 都不变。wrapper 自己抛出的异常按现有「executor/模型异常」路径处理：lease 保持到过期，不另行领取。
- `opspilot.tools.otel_demo` 公开 `OtelDemoRegistrations(tools: tuple[ToolRegistration, ...], targets: tuple[RegisteredTarget, ...])`（frozen）与 `otel_demo_registrations(config=None) -> OtelDemoRegistrations`。executor factory 使用同一份来源。DTO 只含 `credential_ref` 句柄，不含凭据。
- 真实 Investigator 的顺序：建 incident 与 Run → `runner.resume()` → wrapper 拿到真实 scope → `ToolSnapshot(tools, targets, executor.scope)` → `bind_tool_snapshots()` → 进入 `record()`。录制内依次接上 `hooks.input`（原始业务输入，不含答案/flag/场景 ID）、`hooks.model`、`DeepSeekClient.capture_http = hooks.http`、`hooks.tools`、`capture_delivery = hooks.delivery`，然后调用 `proceed()`，最后 `hooks.finish(clock.now())`。不预测 epoch，也不在已开始的 record 中补快照。每个真实 model 事件恰好有一个 http 子事件。
- 录制目录 `<recording_root>/eval-live-<R>-<attempt_id>/`。父目录不存在时以 700 权限创建；目标已存在为 `OUTPUT_EXISTS`（aborted）。
- 上限沿用产品冻结默认值：100 次模型请求、单次 360 s、7200 s backstop、8 MiB/2 MiB。setup/恢复耗时单独记录，不占 Run 额度。

`opspilot.evaluation.evidence_from_recording(recording: Recording, *, outcome: IncidentOutcome | None) -> LiveEvidence(delivered, authorized, delivery_records)` 是纯函数，对已核验的录制包求值：`delivered` 以 evidence_id 为键，只有当 delivery 的原样工具消息实际出现在后续物理模型请求中时 `delivered=True`（与 #234/B2 第 152 行同一规则）；`authorized` 是 Run 输入中的 time_scope_ref → 授权框；`delivery_records[eid]` 是该 DeliveredRecord 的 canonical 字节。

## 9. #234 两项（A7）

- **P1**：`validate_recording` 只从 delivery 的 `parent` 推出交付的 evidence ID。parent 是 tool 时取该 tool 的 evidence；parent 是 refusal 时不交付任何 evidence（拒绝只引用原 tool 已产生的证据，与修复前的行为一致；r2 更正）。不再按 view hash 扩展集合。缓存 delivery（`parent=None`）只有在 view 能唯一对应一个已录 tool 的 evidence 身份时才算交付（view 自带 `evidence_id` 时以它收窄候选）；有两个或以上候选时报 `IDENTITY_MISMATCH`。必须有的反例：两个 tool 事件 view 字节相同、evidence ID 不同，两个结果都产生后才依次交付，但只有第一个的工具消息进入后续模型请求，outcome 却引用两个 → `IDENTITY_MISMATCH`。对照：只引用已交付的那个则通过。缓存歧义 → 拒绝；缓存唯一 → 接受。
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
- summary 的 `EvaluationCapture` 字段：manifest（轮 manifest，见 Q1）、spec/answer（仓库已公开内容）、attempt、outcome、report、delivered、authorized、`traces`（`TraceRecord`，role=`investigation`、required=True，含 project/url/read_back/checked_at）、`delivery_refs`、`recording_ref`、`ledger_ref`、started_at/ended_at、usage（模型请求、token、工具次数与秒数、实际领取 `epoch`）、cost（`{balance_before_cny, balance_after_cny, delta_cny, status: measured|unknown, shared_key_possible: true}`）。
- `lifecycle.json` 每个槽位：`scenario_id, scenario_version, repetition, attempt_id|null, status, stage, reason, product_run_id, summary{path,sha256}|null, fault{flag, variant, original_sha256, injected_sha256, effective_at, restored_sha256, normal_confirmed_at, washed_at}|null, pre_observation{kind, window, matched_span_ids, trace_ids, checkout_calls, probes}|null, timings{started_at, ended_at, setup_s, investigation_s, closure_s}`。顶层：`schema="opspilot-eval-live-lifecycle-1"`、`round, manifest_hash, command`（不含环境值）、`round_status`、`reason`、`retained_fault`、`recovered_at`（恒为 null，恢复见 `recovery-*.json`）、`balances`。槽位另有 `closure_reason`。
- 私有，不入库：`<ledger_root>/<R>/round.jsonl`；`<ledger_root>/<R>/<attempt_id>/` 下的 `lifecycle.jsonl`（每行 `seq, at, mono, stage, event, detail`）、`flags-original.json`、`flags-injected.json`、`pre-observation.json`、`wash-<seq>.json`、`investigator.json`、`deliveries/<sha256>.json`、`recovery.jsonl`；录制包在 `<recording_root>/eval-live-<R>-<attempt_id>/`。文件 600，目录 700。
- ArtifactRef 路径：`recording_ref.path="recording.json"`；`ledger_ref.path="lifecycle.jsonl"`（summary 写入前冻结，之后的事件写 `recovery.jsonl`）；`delivery_refs[eid].path="deliveries/<sha256>.json"`。`attempt_resolver(R, attempt_id, *, ledger_root, recording_root) -> Resolver`：`recording.json` 与 `blobs/*` 解析到录制目录，其余解析到 attempt 私有目录。外置原件缺失时 loader 报 `EVIDENCE_UNAVAILABLE`，不能称可复核通过。

## 11. 冲突时 fail-closed（A4）

- lab 身份：`LabIdentity.verify()` 在预检和每个 attempt setup 时各调用一次，返回非空即拒绝（预检退出 3，attempt 内 setup_failed，`LAB_IDENTITY_UNCONFIRMED`）。真实实现核对三项：kube context 为 `kind-opspilot-m1`；Prometheus 拒绝匿名请求、接受 lab 账号；Jaeger 的服务列表含 `checkout`。18080/19090/16686 由预期 lab 占用属正常，驱动不探测、不抢占这些端口。
- PG：`ports.in_use(pg_port)` 为真时，预检退出 3，attempt 内 setup_failed（`PG_PORT_IN_USE`），不换端口、不杀进程。每个 attempt 的目录 `<pg_root>/<R>/<attempt_id>/pg` 必须不存在（否则 `PG_DIRECTORY_EXISTS`）。启动失败记 `PG_START_FAILED`。收尾只 `stop` 本 attempt 启动的实例；停止失败记入 lifecycle（`PG_STOP_FAILED`），不影响 lab 恢复判定。迁移只用 `owner_dsn` 对新库执行，Run 使用 `runtime_dsn` 的限权角色。
- 共享状态：遇到异主的 `occupation.json`/`fault-active.json` 一律拒绝，不覆写、不偷占过期预约，也不强制回滚他人的 flag 改动。

## 12. 只能 live 验证的项

真实适配器（kubectl CAS、flagd HTTP、Prometheus/Jaeger 查询、PG 启停与迁移、端口探测、DeepSeek 余额、LangSmith project/read-back）以及 C A11 的真实 Run，只能由 lead 排期 live 执行。离线测试不覆盖这些，也不应声称覆盖。新增两个 flag 的实际传播延迟与 flagd 方法尚未实测。

## 13. 裁决（r2，lead 2026-10-18：Q1–Q9 均按推荐采纳）

**Q1 补充条件。**

- 理由：B1 `_round` 要求 journal、summary 和轮 manifest 三者的 hash 一致；P10 要求在 setup 之前写 journal，而那时还没有 scope。per-Run 绑定的 manifest 只有领取之后才能算出，所以用它不能同时满足 B1 与 P10。因此 journal、summary 和 attempt 都使用轮 manifest：registry 维度取静态注册 DTO 的摘要，permissions 维度取去掉 run 身份字段（scope_id、run_id、三个 generation、deadline）后的 scope 策略摘要。
- 保证：每个录制包仍由它自己的 `source_manifest_hash` 绑定，B2 的录制与回放核验不放宽。此外，`opspilot.evaluation.check_recording_binding(recording, attempt, *, round_id, epoch, manifest) -> None` 交叉核对以下各项，任一不符即抛 `StorageError("IDENTITY_MISMATCH")`：
  - `recording.context.recording_id == f"eval-live-{round_id}-{attempt.attempt_id}"`；
  - `source_run_id == attempt.product_run_id`（不为 None）；
  - `scenario_id/scenario_version` 与 attempt 一致；
  - 录制包内有工具快照，且每个 `tool_snapshots[*].scope.run_id == attempt.product_run_id`、`scope_id == f"otel-demo:{run_id}:{epoch}"`；
  - `source_manifest` 的全部维度中，除 registry、permissions 外，都与轮 manifest 相同（per-attempt 的 source_manifest 的这两个维度本就与轮 manifest 不同，这是设计）；
  - 每个快照按第 14 节重算的 `live.registry_hash(snapshot.tools, snapshot.targets)` 等于轮 manifest 的 `registry` 值，`live.scope_policy_hash(snapshot.scope)` 等于轮 manifest 的 `permissions` 值（lead 2026-10-18 r5 裁决：`source_manifest_hash` 只证明录制自洽，这两项才证明它与本轮一致；静态工具集或 scope 策略不同的 attempt 一律 `IDENTITY_MISMATCH`）。轮 manifest 的这两个值由公开的 `template_scope(config)` 与 `otel_demo_registrations(config)` 决定（第 3 节「轮策略模板」）。
- 驱动在写 summary 前执行这项核对，核对失败按 aborted 处理。`epoch` 是 runner 返回的实际领取 epoch，记录在 summary 的 `usage.epoch`。

**Q2**：采用 `attempt_wrapper`，默认 None，不拆分 `resume`。**Q4**：occupation 由 lead claim/release，驱动只 check 并持有 `.active` 标记。

原歧义表与推荐（已全部采纳）：

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

## 14. r3 实现对账（2026-10-18，实现者；只收紧或补全）

- **ran 的必要条件**：除第 7 节外，驱动按返回的 ref 重新 `load_recording`，要求 `synthetic=False`、`check_recording_binding` 通过、`recording.outcome == result.outcome`；summary 的 `delivered/authorized/delivery_refs` 一律由 `evidence_from_recording(已核验录制)` 推出，`InvestigationResult` 中同名字段被忽略。任一不符 → aborted，reason 为固定码（如 `IDENTITY_MISMATCH`）。因此离线假 Investigator 若返回 `recording_ref=None`，attempt 为 aborted（`RECORDING_INCOMPLETE`）；要得到 ran，必须给出真实非 synthetic 录制（实现者用 PG opt-in 测试覆盖，见 `tests/unit/test_eval_c_investigator_impl.py`）。
- **提交新鲜度**：驱动在 `InvestigationRequest.submit_not_after` 给出最晚提交时刻（fault：最新命中 span + 300 s；normal：窗口结束 + 300 s）。Investigator 超时应返回 `error="PRE_OBSERVATION_STALE"` 且不建 Run；驱动另核 `submitted_at > submit_not_after` 也判 setup_failed `PRE_OBSERVATION_STALE`。
- **失败原因不被覆盖**：attempt 的 `reason` 保留第一个失败；收尾失败另记 `closure_reason="RESTORE_FAILED:<code>"`，它同时是整轮停止原因（退出 4）。setup/调查中的非预期异常记 `DRIVER_ERROR`，仍执行恢复。
- **fault guard 覆盖整个 attempt**：从 setup 到恢复与洗净结束一直持有，不在调查后释放再重取。
- **恢复的写入结果未知**：恢复写入抛 `FLAG_WRITE_UNKNOWN` 时不立即失败，以回读决定：内容 SHA 等于 `original_sha256`、uid 不变且 resourceVersion 已前进才算恢复成功，否则 `FLAG_RESTORE_UNVERIFIED`。CAS 冲突（`FLAG_CAS_CONFLICT`）直接失败并保留状态。
- **确定冲突与结果未知（r6，独立审查 P2）**：只有真正不确定的结果（超时、传输错误、写后响应不可解析）才是 `FLAG_WRITE_UNKNOWN` 并进入回读判定；服务器明确拒绝（JSON patch `test` 失败、409 Conflict、resourceVersion 前置条件不符）一律是 `FLAG_CAS_CONFLICT`，恢复停止，`fault-active.json` 保留，绝不按成功处理。fence 写入结果未知时，回读还必须看到本次的 fence 注解（`FlagConfig.fence` 等于本次写入的值），只有 resourceVersion 前进（可能是他人写入）不算成功。`FlagConfig.fence` 默认 None，假对象可以不设；那样 fence 未知结果总判 `FLAG_RESTORE_UNVERIFIED`。
- **每次写入都带 fence（r7，独立审查复验 P2）**：注入、把 injected 写回 original、对 original 的 fence 写入，三者都以新生成的 fence 值调用 `FlagStore.replace(..., fence=<值>)`；驱动没有任何不带 fence 的 ConfigMap 写入。任何恢复写入的结果为 `FLAG_WRITE_UNKNOWN` 时，只有回读同时满足四项才算成功：内容 SHA = `original_sha256`、uid 不变、resourceVersion 已前进、`fence` 等于这次写入的值；否则 `FLAG_RESTORE_UNVERIFIED`，`fault-active.json` 与意图保留。因此 `FlagStore` 的实现（含假对象）须在写入时记录 fence 并在 `read()` 的 `FlagConfig.fence` 返回当前注解值，否则结果未知的恢复永远判为未核验。fence 注解写在 `opspilot.dev/eval-live-restore-fence`，写入时保留其他注解；F11 `restore_fault`/`patch_flags` 与 `kind_lab.py fault` 只比较 `demo.flagd.json` 数据（及 F11 自己 CAS 用的 resourceVersion），不读取这个注解，不受影响。
- **轮 manifest（Q1）的两个维度**：`registry = live.registry_hash(注册 DTO)`，`permissions = live.scope_policy_hash(scope)`；后者去掉 scope_id、subject_id、run_id、三个 generation、window、default_query_window、deadline，另加 `window_seconds` 与「是否有 default_query_window」。驱动用 `template_scope(config)` 预先算出，真实领取的 scope 必须得到同一值；`check_recording_binding` 按第 13 节最后一条核对（6610ff5 中「删除该比较」的更正已按 lead 裁决撤回）。
- **录制引用（r5）**：只有核验通过（第 14 节首条）的录制才写入 summary 的 `recording_ref`；未通过核验的 attempt 为 aborted，`recording_ref=null`，原始包仍留在私有录制目录。槽位的 `status/stage/reason` 以核验后的结果为准，与 summary 一致。
- **录制父目录（r5 澄清第 8 节）**：驱动在调用 Investigator 之前以 700 权限创建缺失的 `recording_root`；包目录本身仍由 `record()` 独占创建。
- **真实 Jaeger 探测（r11，PR #271 机器人 P1 更正）**：`LabTelemetry.spans` 只按 service 与时间窗检索，不再用 `tags={"error":"true"}` 服务端过滤；错误判定完全在本地按第 6 节 `span_error()`（error 标签、`otel.status_code=ERROR`、非零 grpc 状态、HTTP 5xx）。大小保护保留：每页上限 1500 条 trace、响应≤64 MiB；满页、超限、截断或解析失败都返回 `complete=False`，因此故障门槛不通过、正常窗口不判干净，绝不当作「无错误」。
  - **r12 自适应切分（live 负载下仍能完整）**：一页满（返回条数 ≥ 1500）说明该子窗口可能有缺失，丢弃这一页，把子窗口按微秒等分为 4 段再分别检索（相邻段不重叠：`[a, m]`、`[m+1, …]`），直到每段都不满页。只有全部子请求都成功且不满页时才 `complete=True`。上限：一次 `spans()` 最多 64 个子请求、累计响应 1 GiB、子窗口不小于 1 ms；任一触顶返回 `complete=False`（仍不当作干净）。
  - **返回内容**：只返回所查 service 自己的、`span_error()` 判为错误的 span，按 `(trace_id, span_id)` 去重（Jaeger 返回整条 trace，同一 span 可能在相邻子窗口重复出现）。门槛只对签名里每个 service 分别查询并只看错误 span，所以这一收窄不丢任何签名匹配；私有 `pre-observation.json` 里的原始记录相应只含这些错误 span。
  - **超时语义**：第 6 节的「单探测≤5 s」指每个 HTTP 子请求≤5 s；一次 `spans()` 的总耗时受 64 个子请求上限约束，门槛与洗净的总预算仍按 monotonic 计。
  - **阶段截止（r13，独立审查 P2）**：门槛把阶段截止时刻（`started + budget`，`LiveDeps.clock.monotonic()` 刻度）传给探针：探针实现了 `spans_within` 就调用它，否则退回 `spans(service, start, end, min(probe_timeout, 剩余预算))`；`checkout_calls` 的 timeout 同样取 `min(probe_timeout, 剩余预算)`。`LabTelemetry.spans_within` 在每个子请求前检查剩余时间，子请求超时取 `min(5 s, 剩余)`；剩余≤0 即停止并返回 `complete=False`。`fault_gate` 与 `normal_window` 在接受「通过」之前再核一次截止：已超出预算就不通过（`FAULT_NOT_OBSERVED` / `NORMAL_WINDOW_UNCONFIRMED`），绝不在超时后判通过或判干净。截止端点仍含在预算内（恰好用满不算超出）。
  - **量级**：300 s 窗口、某 service 每秒 r 条 trace 时，满页阈值 1500 对应子窗口约 1500/r 秒。r=45（约 13,500 条）时：300 s 满页 → 4×75 s（各约 3,375 条，满页）→ 16×18.75 s（各约 844 条）＝ 21 个请求，其中 5 页作废，传输约 21,000 条 trace。
- **lab 身份**：kube 身份改为经 lab kubeconfig/context 读取 `namespace/otel-demo`（`config current-context` 读的是文件默认 context，不能证明 `--context`）。
- **PG**：每 attempt 新 initdb，只监听 127.0.0.1 TCP（不建 Unix socket：worktree 路径可能超过 103 字节上限），owner 只做迁移；intake 用 `eval_web`（`opspilot_web` 成员），Run 用 `eval_worker`（`opspilot_worker` 成员）。r11（PR #271 机器人安全 P2）：不再用 trust；initdb 以 `--auth=scram-sha-256` 和每 attempt 随机的 owner 密码（pwfile 600，放在 attempt 私有目录，initdb 后删除）建库，两个登录角色各有独立的随机 SCRAM 密码；所有 DSN（owner/web/worker）都带密码，无密码连接被拒绝。密码只在进程内存与 `PgInstance` 中，不写日志、ledger 或证据。initdb、`pg_ctl start/stop` 用净化环境启动，只保留 `PATH`、`HOME`、`TMPDIR`、`LANG`、`LC_*`、`TZ`，不含任何 DEEPSEEK/LANGSMITH/Prometheus/口令/令牌类变量，postmaster 继承的就是这份环境。
- **非 synthetic 录制位置**：B2 要求位于本 worktree `tmp/eval/<recording_id>/`，因此 `recording_root` 的真实值必须是 `<worktree>/tmp/eval`。
- **收尾失败与逃逸兜底（r14，PR #271 机器人第三轮 + 全模式排查）**：
  - PG 停不掉（`pg.stop` 失败，或启动失败后清理时停不掉）：收尾不完整，`closure_reason="PG_STOP_FAILED"`，ran 改为 aborted，整轮停止，后续槽位 `not_started`，退出 1（与 `LAB_FAULT_FOREIGN` 同为「停轮、无保留故障」）。不用 4：4 表示 `fault-active.json` 保留并需 `--recover`，PG 停止失败不属于这种情况。第 11 节「不影响 lab 恢复判定」不变。
  - 私有 ledger 写不出（目录建不成或追加失败）：ledger 不再抛异常，而是记失败，恢复与 PG 停止照常完成。attempt.json 之后 ledger 就写不出时，该 attempt 不触碰 lab，记 setup_failed `EVIDENCE_WRITE_FAILED`，仍写 summary（`ledger_ref=null`）和 lifecycle，整轮退出 5。attempt 中途写不出时同样以 5 停轮。`--probe` 与 `--recover` 的私有 ledger 同此规则。
  - lab 协调文件（occupation/guard/active marker/run-starts）打不开：attempt 开始前视为预检拒绝（3，`LAB_LEDGER_UNAVAILABLE`）。attempt 内记 setup_failed；轮级记 stopped，退出 1，绝不报 0。
  - `--recover` 中断或异常：意图还在则退出 4（`RESTORE_FAILED:INTERRUPTED|<code>`）。本次恢复开始后意图消失，视为恢复完成：照写 recovery 记录，中断时退出 130。恢复开始前意图就消失（他人已恢复）则中断退出 130，否则退出 3 `NO_RETAINED_FAULT`。记录写入期间延后信号。
  - 兜底：run、`--probe`、`--recover` 在分发处统一捕获逃逸的异常与中断，照常输出最后一行 JSON。映射为：本轮意图仍在 → 4 `FAULT_RECOVERY_REQUIRED`；OSError/StorageError → 5；中断 → 130；LabError → 1（原因码）；其他 → 1 `DRIVER_ERROR`（stderr 只打印异常类型名）。run 与 `--probe` 收尾时若本轮意图仍在而未报 4，也改为 4。`--verify-replay` 中断时输出 status=`interrupted`、退出 130；单项失败一律填固定码（新增 `VERIFY_FAILED`），不抛出。参数解析与 dry-run 中断时输出 `{"status":"interrupted","exit_code":130}`。
- **r15（astra 独立审查 d1addce）**：
  - 预检在 `begin_round` 之前完成 `append_run_start` 的全部检查（`check_run_start`：batch-journal guard 能打开、哈希链有效、round 未开始、`run-starts.jsonl` 或其目录可写），不满足则退出 3 且零外部调用。`begin_round` 之后追加仍失败（预检后才出现的变化）不再报 3，改为 stopped、退出 1，全部槽位 `not_started`，不写 journal。
  - run 与 `--probe` 中私有证据/状态写入（pre-observation、flags-*、wash、录制父目录）抛 OSError 时，原因记 `EVIDENCE_WRITE_FAILED`（不再归为 `DRIVER_ERROR`），照常恢复，整轮停止，后续槽位 `not_started`，退出 5；故障仍保留时 4 优先。
  - 退出码统一按 **4 > 5 > 130 > 1** 取最高。开始后以 3 停轮（预约消失）与 130 同级；同级时保留先出现者。`PG_STOP_FAILED`、`LAB_FAULT_FOREIGN` 被压过时仍记录在 lifecycle 的 reason/closure_reason 中。

## 15. r16 协作式中断（#273，2026-10-18）

动机：r7–r15 中信号处理器在任意字节码位置 raise `Interrupted`，任何没有包进 `_deferred_interrupts()` 的窄窗口都可能把收尾或证据写入打断一半（#273 列出 5 处）。r16 把中断改为协作式，从机制上关闭这类窗口。退出码语义（第 2 节、r13、r15 的 4 > 5 > 130 > 1）不变。

- **信号处理器**：收到 SIGINT/SIGTERM 时只做一件事，置位 `stop_requested`（并记录首个信号与时间），绝不 raise。例外：第二次信号到达，且当前不在受保护段内时，按「强制中断」立即 raise `Interrupted`，让操作员能打断卡住的非收尾阶段；受保护段内的第二次信号同样只置位。
- **检查点**：只在检查点把 `stop_requested` 转成 `Interrupted`，检查点函数为 `checkpoint()`，位置至少包括：
  - 每个槽位开始前；
  - 注入前；
  - 每次预观察、洗净、gate 轮询的循环迭代；
  - 每次模型调用或工具调用前后（经 investigator 接缝）；
  - 每次外部命令（kubectl/pg_ctl/initdb/git）之前。
  受保护段内调用 `checkpoint()` 不抛出。
- **可中断等待**：真实 `Clock.sleep` 必须在置位后尽快返回，延迟不超过 1 秒（例如以 ≤1s 的片段睡眠并检查标志，或等待一个由处理器触发的 Event），返回后由调用方的下一个检查点抛出。外部命令照常受各自 timeout 约束，不在命令执行中途 kill。
- **受保护段**：以下整段不可中断，段内既不 raise，也不执行检查点：
  - 恢复与洗净：`restore_fault` 全程，从读取意图到 unlink 意图，再到最终 ledger 事件；
  - PG 的启动清理与停止：`LocalPgLauncher.start` 的失败清理、closure 中的 stop 及其重试；
  - closure 之后的冻结与证据写入：summary、journal、recording 封存、aggregate、lifecycle、probe.json、recovery 记录、`structural-replay.json`；
  - 占用与 active marker 的创建和删除，以及 occupation release；
  - `inject_fault` 中从写 ConfigMap 到记录 fault 元数据（含 hash 快照）这一段。fault 记录必须在等待 flagd 传播之前写入 attempt 状态，传播等待本身可以中断。
  段结束后不自动 raise；由下一个检查点或收尾逻辑读取 `stop_requested`，按既有退出码规则处理。
- **收尾读取**：run、`--probe`、`--recover`、`--verify-replay` 在写最终 JSON 前读取 `stop_requested`；若已置位且没有更高优先级的码，则退出 130。
- **兼容**：测试中假对象直接抛 `Interrupted` 的做法保留，等价于一个检查点被触发；`KeyboardInterrupt` 同样处理。既有验收测试的断言不修改。

#273 的 5 项与 r16 的对应：
1. start 中断时清理失败：清理处于受保护段，先完成 stop 并检查 stop_failed，再由检查点处理中断。
2. unlink 之后被中断：restore 全程受保护，不再报出虚假的 4。
3. 传播等待中失败导致 fault 记录为 null：fault 记录先于传播等待写入。
4. closure 之后 `clock.now()` 处于未保护窗口：冻结与证据写入都在受保护段内。
5. active marker teardown：marker 的删除在受保护段内，aggregate/lifecycle 照常写出。

### r16 预审裁决（astra 预审，lead 2026-10-18 全部按推荐采纳，无用户项）

- **R1 检查点补全**：flock 改为非阻塞重试，每次重试前做检查点；以下位置在调用前后都做检查点：每个服务的 HTTP 子请求、每次 DB 操作与迁移、余额读取、项目准备、trace flush 与 read-back、逐录制核验。区分两种情况：检查点处可以响应中断；在途调用只能等它自己的超时结束。≤1 秒的承诺只适用于可中断的 sleep。
- **R2 第二次信号**：SIGINT 与 SIGTERM 合并计数。第 2 次及以后的信号只在允许强制中断的位置 raise。外部命令执行期间不强制 raise，等命令完成或超时后由检查点处理。受保护段内收到的信号，在退出保护段时也不自动补抛，交给下一个检查点或收尾。强制 raise 之后照常走恢复和证据收尾。
- **R3 停止状态不消费**：`stop_requested` 在一次 CLI 调用内是永久状态，检查点和最终判码只读取、不清空。`_deferred_interrupts` 保留为兼容入口，与受保护段共用同一个可嵌套深度。捕获到直接抛出的 `Interrupted` 时，也要置位 `stop_requested`。
- **R4 保护完整的状态转换**：保护范围包括恢复结果的赋值、closure 的起止时间、PG stop 前后，以及证据的准备和写出。录制只保护封存阶段，不包住整个 `record(drive)`。marker 只保护创建和删除，不跨越它的 `yield`。occupation 在获取锁之前仍可中断。在受保护段内调用 checkpoint 是合法的，但不起作用。
- **R5 sleep**：非保护段按 monotonic 截止时间做 ≤1 秒的分片睡眠，收到停止请求后提前返回，调用方紧接着做检查点。保护段内照常睡到原截止时间。假时钟的语义不变。洗净窗口的完整 300 秒断言不修改。
- **R6 注入拆分**：注入拆成两步，先提交（持久化意图、写 ConfigMap、交付 hash 与请求时间等元数据，整段受保护），再等待传播（可中断）。run 和 probe 共用这套逻辑。传播尚未确认时，`effective_at` 保持为空。
- **R7 假对象异常**：假对象主动抛出的 `Interrupted` 或异常属于接缝故障注入，受保护段不屏蔽它，既有断言（例如恢复写入前抛异常 → 4）不变。新增测试另行验证真实处理器的置位行为。
- **R8 模型与工具包装**：检查点包装在录制代理外侧。已完成的物理调用先记录，再做检查点。中断要穿过普通的错误转换，进入收尾流程。
- **R9 PG 清理失败与中断同时发生**：两者都保留。closure_reason 记 `PG_STOP_FAILED`，后续槽位停止。没有 4 或 5 时退出 130。
- **R10 最终判码**：进入最终输出前先取快照，按既有排名（4 > 5 > 130 = 开始后的 3 > 1，同级先到者优先）合并后打印 JSON，并返回同一个码；快照之后到达的信号不再改变结果。预检拒绝（3，零外部调用）发生在任何信号之前时保持 3；预检期间若已收到停止请求，按 130 处理。

### r16 实现对账（bf3154c，lead 2026-10-18 裁决）

- **中断后的余额读取**：按 R1，attempt 结束后读取余额前先做检查点。停止请求已置位时跳过这次读取，该 attempt 的费用记为 `unknown`。这是已接受的行为，如实记录。
- **强制中断的已知残余**：第二次信号在进入受保护段之前的几条字节码内到达时，仍可能 raise（例如异常处理与 `_closure` 进入保护段之间），从而跳过恢复，留下 exit 4，需要 `--recover`。不屏蔽信号就无法在 Python 中关闭这个窗口。第一次信号不会造成这种情况。
3. **外部命令与终端信号**（astra 复验 c792c3e）：驱动的 6 处外部命令都在独立会话（`start_new_session=True`）中运行，终端 Ctrl-C 只到达驱动进程。产品代码 `opspilot/schema.py` 的 `migrate()` 调用的 pg_dump 仍和驱动同属一个进程组；但这个调用只在接管旧库时才会执行，驱动每次都新建空库，所以当前路径走不到它。这一项作为已知残余接受，不修改产品代码。

#### 检查点排查清单（PR #276 第三轮，逐处一行）

记号：**前/后** = 调用前后有 `checkpoint()`；**保护** = 处于受保护段；**例外** = 有意不加，附理由。外部命令（kubectl/git/initdb/pg_ctl）经 `external_command()` 在调用前做检查点、运行在独立会话，执行中不强制 raise。

`scripts/eval_live_lab.py`
- `_guard` 阻塞锁重试循环：每次迭代开头（含 sleep 之后、flock 之前）检查点；非阻塞锁只试一次。
- `active_marker`：加锁前可中断；创建与删除为保护，`yield` 不保护。
- `append_run_start`：锁等待可中断，取得锁后写入为保护。
- `check_occupation` / `check_run_start`：只有锁等待，同 `_guard`。
- `await_variant` 轮询循环：每轮 resolve 前/后；sleep 可中断，下一轮开头的检查点处理停止请求。
- `confirm_all_normal` 遍历开关：每个开关进入 `await_variant`，同上。
- `commit_fault`：`flags.read`（kubectl）前/后，`confirm_all_normal` 同上；提交前检查点，提交到 `record` 回调为保护。
- `await_fault`：同 `await_variant`。
- `restore_fault` 全程保护（read/replace/read、传播等待、洗净窗口及其 HTTP、unlink、ledger 事件）；其中检查点不生效，sleep 睡满。
- `_query_spans` / `_checkout_calls`：每次 HTTP 前/后。
- `fault_gate` 轮询循环：每轮开头，以及每个服务的查询、checkout 查询前/后。
- `normal_window` 循环：每轮开头和 sleep 之后，以及每个查询前/后。
- `LabTelemetry.spans_within` 子请求循环：每个 Jaeger 子请求前/后。
- `LabTelemetry.checkout_calls`（Prometheus HTTP）：由 `_checkout_calls` 包前/后。
- `HttpFlagResolver.resolve`（flagd HTTP）：由 `await_variant` 包前/后。
- `_external`（kubectl 读）：前（external_command）/后。
- `_patch`（kubectl 写）：前（external_command）；**例外**：不加后检查点，写入只在保护段（commit、restore）内发生，写后 raise 会丢掉"写已发生"的信息。
- `GitProvenance._git`：前/后。
- `KindLabIdentity.verify`：kubectl 前/后，3 次 HTTP 各自前/后。
- `SocketPortProbe.in_use`（本地 TCP 探测，1 秒超时）：前/后。
- `LocalPgLauncher.start`：initdb、pg_ctl start、CREATE DATABASE、migrate、建角色各自前/后；失败清理为保护。**例外**：`schema.migrate` 内部的 pg_dump 子进程属产品代码，见上条。
- `LocalPgLauncher.stop`：前（external_command）；调用方（closure、start 清理）为保护。
- `SystemClock.sleep` / `interruptible_sleep`：可中断分片睡眠（≤0.5 秒），保护段内睡满。
- `DeepSeekBalance.balance`：由调用方 `_run_slot` 包前/后。
- `ProbeEnvChecker` / `LabEnvChecker`：只读本地环境与文件，**例外**：不加。

`scripts/eval_live.py`
- `preflight`：occupation 锁、identity、端口、`confirm_all_normal` 分别同上；`env.check`、`read_state`、`fault_for` 为本地读取，**例外**。预检中 raise 的 `Interrupted` 经 `_dispatch` 得 130。
- `_run_round`：`provenance.read`（git）同上；`begin_round`（LangSmith 项目准备）前/后；`append_run_start` 同上；槽位循环每次开始前检查点。
- `_run_slot`：journal/ledger 创建为保护；两次余额读取前/后；closure 之后的冻结与 summary 写出为保护。
- `_guarded`：入口、注入前、investigate 前检查点；pre-observation.json 写入为保护。
- `_closure`：全程保护。
- `_recover_round`：occupation、active marker 同上；restore 及结果赋值为保护；恢复记录写入为保护，之后读取停止请求（记录不含状态与退出码，读取在写后不会不一致）。
- `_main`：参数解析后检查点；`real_deps` 失败的拒绝经 `_refused`，在保护段内判码并打印。
- 先发布后判码：`_dispatch`、`_verify` 的最终 JSON 在保护段内取快照后打印。**例外**：`lifecycle.json`、`probe.json` 在保护段内写出后才读取停止请求；写入期间到达的信号使最终码为 130，而已写出的文件记的是写入前的状态。独立验收 `test_evidence_file_io_is_protected[lifecycle.json]` 要求这一行为（写入中收到信号 → 130），因此保留；`summary.json`、`aggregate.json` 不含轮级退出码，不存在不一致。

`scripts/eval_live_probe.py`
- `probe_preflight`：同 `preflight`。
- `_probe_one`：入口与注入前检查点；提交与 fault 记录为保护；传播等待与 gate 同上；pre-observation 写入为保护；`_restore` 为保护。
- `_run_probe`：槽位循环每次开始前检查点；probe.json 见上条例外。

`scripts/eval_live_investigator.py`
- `begin_round`：由 `_run_round` 包前/后。
- `investigate`：4 次 `install()` 各自前/后，`accept()` 前/后（之后才建模型客户端并启动 Run）。
- 模型与工具调用：`_Checkpointed` 在录制代理外侧包前/后（R8）。
- 录制封存（`_sealed_record`）：从 drive 返回到 `record()` 结束为保护。
- `investigator.json` 写入为保护。
- `_traces`：flush 前、flush 后/read-back 前、read-back 后检查点；read-back 内部（`lab_evidence.find_root_run`，最多 30 次 `list_runs`）逐次轮询前后都有检查点，轮询间隔用 `interruptible_sleep`，取链接（`run_url`）前也有检查点（第四轮）。flush 或轮询期间的停止结束 read-back，trace 如实记为 `error`/`interrupted`；已完成的 read-back 保留，停止请求留给驱动的下一个检查点。
- **例外**：Run 结束后的本地读取（`_outcome` 的 rebuild/control_audit/read_after、`_report`、`store.run_usage`、`_finish_reason` 读本地文件）不加检查点。它们是一次性 PG 上的有界读取，用来组装已经计费的 Run 的结果；停止请求在 investigate 返回后驱动的下一个检查点处理。
- `prepare_round`：只读本地文件，**例外**。

`scripts/eval_live_verify.py`
- `_verify_replay` 录制循环：每个录制的 load/replay 前/后检查点；构建最终记录前检查点。
- `structural-replay.json`：最终快照与写出在同一保护段内，记录的状态、退出码与最终 JSON 一致；之后到达的信号不再改变结果（`_verify` 对正常返回不再二次合并）。

被驱动调用的 `scripts/` helper（第四轮排查内部 sleep 与轮询）
- `lab_evidence.find_root_run` / `trace_evidence`：唯一有内部轮询的 helper。新增 `sleep` 与 `checkpoint` 参数，默认值保持原行为；investigator 传入 `interruptible_sleep` 与 `checkpoint`，见上条。
- `lab_evidence.prepare_lab_project` / `configure_lab_round` / `langsmith_client`：无 sleep 或轮询；`prepare_lab_project` 由 `_run_round` 包前/后。
- `kind_lab.get_json` / `kubectl` / `lab_env` / `lab_authorization` / `mutate_flags` / `OPENER`：单次请求或纯函数，无 sleep 或重试；`kind_lab` 中带 sleep 的循环只在 `cmd_up`（实验环境搭建 CLI），驱动不调用。
- `m1_context_latency.balance`：单次 HTTP，无重试；由 `_run_slot` 包前/后。
- `m1_live_flash_loop.load_langsmith_env` / `read_key`、`f11_u2_harness.resolve_ledger_root`：只读本地文件或路径，无 sleep。
