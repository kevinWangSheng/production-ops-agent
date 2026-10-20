# 评测 PR-D1：CI 结构重放入口（合同决定与公开接口 r1）

任务记录：[场景评测套件](2026-10-12-eval-suite.md)。分支 `feature/eval-d-ci-structural`，worktree `../poa-eval-d`。

## 决定表（2026-10-18 预审，Codex gpt-6-astra，全新上下文）

| 编号 | 决定 | 来源 |
|---|---|---|
| D01 | PR-D 拆为两步：本 PR（D1）只交付 CI 结构重放入口；第一轮基线入库连同真实 P7 校准、#250 归 M3，门槛不降（六场景 replay=5 / live=2、P10 完整性、P7 合格）。 | 用户 2026-10-18 裁决 |
| D02 | CI 不用真实录制。真实录制保持私有，不入库；fixture 全部为合成 CANARY 包。 | 推荐（B2 接口 :43、:71，ADR-0006） |
| D03 | 冻结的合成包是主证据，固定 SHA 入库；同时用生成器现场重新生成，作为漂移报警。 | 推荐 |
| D04 | 通用入口直接调用 B2 的 `load_recording` 和 `replay(mode="structural")`；不读取已提交的 `structural-replay.json` 冒充本次验证。 | 推荐 |
| D05 | 用 pytest 写验收，提供 `make eval-structural` 目标，并在 checks job 中加一个显式隔离的步骤；不新建 job。 | 推荐 |
| D06 | fail-closed：任何核验失败或没有执行任何项都让 CI 失败。期望 SHA 取自 fixture 声明，不从输入现场重算。未知 schema 一律拒绝。格式升级时新增 fixture 版本，旧版本保留原 hash。 | 推荐 |
| D07 | 零凭据、零网络由两层保证：CI 步骤在没有网卡的网络 namespace 中运行，环境只保留白名单变量，HOME 为临时目录；入口进程本身不读取环境变量或凭据文件，也不调用模型或执行器。隔离失效则该步骤失败。 | 推荐 |
| D08 | 验收测试由独立作者按本接口编写；red-proof 保持 report-only。 | 既有规则 |
| D09 | 结构结果单独列出，不计质量率，不算基线，不翻转 `passes`。 | PR-A 接口 :155–156 |

结构重放能证明什么：冻结的包能被当前 reader 逐件核验并通过身份检查，事件计数与声明一致。它不能证明模型质量、真实服务行为、live 录制的完整性，也不能证明当前模型或工具配置与录制兼容。

## 公开接口

### 1. Fixture 布局

`tests/fixtures/eval_structural/<name>/`：

- `recording/`：B2 录制包，即 `recording.json` 加 `blobs/`，由 `record()` 生成，字节冻结。
- `fixture.json`：键严格限定为 `schema`、`name`、`recording_sha256`、`attempt`、`manifest`、`expected_event_counts`。各键的值：
  - `schema` 为 `"opspilot-eval-structural-fixture-1"`。
  - `name` 等于目录名，限定 `[a-z0-9-]{1,64}`。
  - `recording_sha256` 是 64 位小写十六进制。
  - `attempt` 和 `manifest` 用 recording 模块的 `wire()` 编码 `AttemptRecord` 与 `Manifest`，其中 `attempt.mode == "structural"`、`product_run_id is None`，manifest 的 `replay_fixture` 维度为 `hash=<recording_sha256>`。
  - `expected_event_counts` 的键严格为 `model`、`http`、`tool`、`delivery`、`refusal`，值为非负整数。

至少提供一个 fixture：`canary-fault-v1`。它必须满足：

- 录制上下文 `synthetic=True`。
- 至少含一次 model→tool→delivery→model 的链条，以及一次成对的 http 事件。
- 所有服务名、操作名和标识使用 CANARY 风格的合成值。
- 不含真实主机名、trace ID、凭据或私有协议字段。

### 2. 生成器 `scripts/eval_structural_fixture.py`

`generate --name <name> --out <dir>`：

- 使用固定时钟、固定 ID、假模型和假执行器，调用 B2 的 `record()`，在 `<dir>/<name>/` 下生成 `recording/` 和 `fixture.json`。
- 目标已存在时以退出码 2 拒绝，不覆盖。
- 同一份代码运行两次，产出的整个目录逐字节相同（确定性）。
- 不读取环境变量，不访问网络，不加载凭据。
- 支持的 name 集合在代码中固定；未知 name 以退出码 2 拒绝。

### 3. 入口 `scripts/eval_structural_ci.py`

`[--fixtures DIR]`，默认 `tests/fixtures/eval_structural`。对 DIR 下的每个子目录按名字排序，逐个执行：

1. 严格解析 `fixture.json`，不合格记 `FIXTURE_INVALID`。
2. 调用 `load_recording(<sub>/recording/recording.json, expected_hash=fixture.recording_sha256)`。
3. 调用 `replay(recording, mode="structural", attempt=..., manifest=..., investigator=None)`。
4. 统计录制事件，比对 `expected_event_counts`，不一致记 `EVENT_COUNTS_MISMATCH`。

每项结果包含 `name, recording_sha256, loaded, structural_verified, counts_ok, error`。任何一步失败都以固定码填入 `error`（`StorageError` 用它自带的 code，其他异常用 `VERIFY_FAILED`），不向外抛出。

stdout 只输出一个 JSON 对象：`{"schema":"opspilot-eval-structural-ci-1","status":"verified|failed|refused","exit_code":N,"results":[...]}`。

退出码：

- 0：至少有一项，且全部满足 `loaded`、`structural_verified`、`counts_ok`。
- 1：任意一项失败。
- 3：DIR 不存在或没有子目录（`NOTHING_TO_VERIFY`）。

入口的约束：不写任何文件；不读取环境变量、`.env*` 和凭据加载器；不创建模型客户端、执行器或 socket。

### 4. Make 与 CI

- `make eval-structural`：执行 `.venv/bin/python scripts/eval_structural_ci.py`。
- `.github/workflows/ci.yml` 的 checks job 在 `make check` 之后增加一步「Structural replay (offline, no credentials)」：
  - 在新的网络 namespace 中以 runner 用户身份运行入口，例如 `sudo unshare --net` 后再降权；具体写法由实现者在 CI 实测确定。
  - 环境变量只保留 `PATH`、`LANG`，`HOME` 指向 `mktemp -d` 创建的临时目录。
  - 在同一个 namespace 里先做自检：向外部地址发起 TCP 连接必须失败，否则该步骤失败。
  - workflow 的 permissions 和 secrets 都不变。

### 5. 验收要点（供独立测试作者使用）

- 生成器确定性：生成到两个临时目录，结果逐字节相同；用当前代码重新生成 `canary-fault-v1`，结果与入库的 fixture 逐字节相同（漂移报警）。
- 入口在正常情况下：入库 fixture 返回 0，status 为 verified。
- 拒绝与失败：
  - 翻转任意一个 blob 的一个字节，返回 1，`error` 非空。
  - 篡改 `recording_sha256`，返回 1。
  - 未知 `schema`，或出现多余、缺失的键，返回 1（`FIXTURE_INVALID`）。
  - 计数不一致，返回 1（`EVENT_COUNTS_MISMATCH`）。
  - 目录为空或不存在，返回 3。
  - 只要有一项失败，即使其他项通过，也返回 1。
- 零外部调用：在 socket 创建被禁用、环境变量被清空或填入 CANARY 假凭据的条件下，入口结果不变；fixture 的全部字节中不含这些假凭据值。
- fixture 内容：`synthetic=True`；不含 `.env` 键名对应的值，也不含 `http(s)://` 开头的非 CANARY 主机。
- 结构结果不计质量率：把它送入 PR-A 的 aggregate 时，`pass_rate` 为 None，`baseline_complete` 为 False（`NO_QUALITY_SLOTS`）。

## r2（2026-10-18，实施中的 lead 裁决）

- 入口输出 JSON 的顶层增加 `reason` 字段：退出码 3 时为 `"NOTHING_TO_VERIFY"`，其他情况为 null，写法与 `eval_live_verify` 一致。
- 默认的 `--fixtures` 路径按仓库根目录解析；显式传入的路径原样使用。DIR 下的普通文件忽略，只有文件时退出码为 3。
- `fixture.json` 不合格时，`recording_sha256` 记为 null。错误按执行顺序只取第一个：FIXTURE_INVALID → StorageError.code → VERIFY_FAILED → EVENT_COUNTS_MISMATCH。
- fixture 内部不一致（`recording_sha256` 与 manifest 的 `replay_fixture` 对不上）报 FIXTURE_INVALID（见验收歧义第 9 条）。
- 秘密扫描：B2 工具事件的 `key` 字段是 tool_key 摘要，会触发 gitleaks 的 generic-api-key 规则。`scripts/check_secrets.py` 为此加了一条例外，规则、fixture blob 路径和具体值三者同时匹配才放行，沿用 m0-integration 的先例。
