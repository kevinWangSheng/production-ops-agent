> 裁决（r1，2026-10-22）：lead 按用户 2026-10-17 常设决定「预审待决项按推荐」采纳 U1-a、U2-a、U3-a、U4-a、U5-a 与 R1–R9；合并仍为用户门（数据流与凭据）。任务记录见 [2026-10-22-f7-observer-redaction.md](2026-10-22-f7-observer-redaction.md)。

# #254 合同预审：Observer 恢复观察 / 发布观察原始读数持久化前脱敏

- 预审者：未参与实现的 Agent，只读；仓库 `main` @ `8f1f49c`（与 origin/main 同步）
- 日期：2026-10-22
- 依据：issue #254；任务记录 `docs/tasks/2026-10-15-f7-hostile-input.md` 待决 1（l.65-67）与实现选择（l.21-29）；`docs/tasks/2026-10-15-f7-contract.md` U7（l.68）与第 6 类（l.51）；C3 §10「采样与提交」、§11「部署与 schema 迁移」、§12「数据出口」（l.447）；PRODUCT-CONSTRAINTS l.38、l.48
- 未执行：make check / 全量 pytest（按要求）。只跑过一条定向 python 片段验证 `redact_credentials` 对镜像 digest 与标签名的行为（结果见 §1.6）。

## 1. 核对到的事实（路径:行号）

### 1.1 存储与写入点

- 恢复读数表 `opspilot_observation_signal_readings`：`query`、`source`、`raw_sha256`、`raw bytea`（可空，CHECK `octet_length(raw) <= 131072` 且 raw 非空时 raw_sha256 非空）——`opspilot/migrations/versions/0003_observation_store.py:318-336`、`:142`。Observer 角色只有 `SELECT, INSERT`（`:375`）。
- 写入：`ObservationStore.submit_sample` → `opspilot/observation/store.py:2115-2134`，原样写 `item.raw`、`item.raw_sha256`；`SignalReading` 校验 `raw_sha256 == sha256(raw)`（`store.py:230-237`）。
- `raw` 内容是 Observer 的 bundle（`opspilot/observer/sampler.py:423-498`）：`format`、窗口、`sample_time`，每个 kind（query/coverage/freshness）一份 `expr`、`status`、`detail`、`http_status`、`body_complete`、`body_sha256`、`body_b64`（**Prometheus 响应体原字节**）、`sent_at`。构造在 `sampler.py:388-404`，`take_sample` 在 `sampler.py:212-249` 汇总后提交。
- 发布读数表 `opspilot_release_query_results`：`query`、`detail`、`raw bytea NOT NULL`、CHECK `octet_length(raw)<=30720 AND raw_sha256=encode(sha256(raw),'hex')`——`0015_release_observation_sessions.py:114-121`；Observer 只能 INSERT 指定列（`:19`、`:183`）。
- 写入：`submit_release_sample` → `opspilot/observation/release_samples.py:585-595`（`q.model_dump()` 原样）；`raw` 来自 `opspilot/observer/release_sampling.py:126-139`，即 `PrometheusReadOnlySource.instant_vector` 的 `result.body`。`ReleaseQueryResult.raw` `max_length=30720` 且校验 sha（`opspilot/observation/release_types.py:56-73`）。
- 先例：发布 webhook 接收已在存储前脱敏 `audit_text`，`raw_sha256` 覆盖原字节（`opspilot/observation/releases.py:458-461`）；#253 的工具证据在执行器 `_inspect` 脱敏后再由脱敏字节重新解析 rows（`opspilot/tools/executor.py:1364-1384`），`raw_sha256` 对存储字节计算（F7 任务记录 l.23）。

### 1.2 哪些字段可能含凭据

| 字段 | 来源 | 风险 | 依据 |
|---|---|---|---|
| bundle `body_b64`（恢复）/ `raw`（发布） | 数据源返回 | **主要风险**：200 响应的标签值（URL query、`token`/`session` 标签、URL userinfo）、401/403/代理错误页回显、超时/协议中断的部分体 | `prometheus.py:225-298`、`:379-403` |
| 请求头 `Authorization` | Observer 自己构造 | **不入库**：只发送不存储 | `prometheus.py:211-216`、`:313-318` |
| Prometheus base URL | 环境变量 | 不入库；带 userinfo 直接拒绝启动 | `prometheus.py:152-163`、`observer/__main__.py:126-143` |
| `detail` | 固定码 | 无（「fixed codes only: never a provider message」） | `prometheus.py:94-95`、`release_types.py:64` |
| `reading_error`/`error_type` | 固定码 / 异常类型名 | 无（显式不写异常文本） | `sampler.py:501-534` |
| `query`/`expr`/`signal_name`/`source` | HealthProfile 与发布查询计划（配置） | 低；运维配置文本，不是数据源返回 | `sampler.py:212-215`、`release_sampling.py:126-129` |
| 发布 `release_evaluation` Jsonb（pod 名、镜像 digest） | 由响应体标签派生的强类型字段 | 低；若在解析前脱敏则同源 | `release_types.py:97-101`、`release_samples.py:574` |

### 1.3 哈希、幂等、重放对原文的依赖

- 恢复重放：`replay_sample` 先比 `sha256(raw) == raw_sha256`，再逐 kind 比 `body_sha256`，再从 body 用 `_single_value` 重算值并重判（`sampler.py:576-670`）；`fold_history` 另有 `raw_mismatches`（`store.py:975-980`）。web 恢复页与 `python -m opspilot.observer.replay` 都走这条（`web/service.py:1753-1765`，`docs/development.md:189-198`）。
- 发布重放：逐行比 `sha256(raw)`，用 `raw` 重算 `evaluate_release_sample`，与存储的 `release_evaluation` 比，再重算 `sample_fingerprint` 与 `release_evidence_sha256` 比（`opspilot/observer/release_replay.py:363-412`）。
- 发布幂等：同一 `sample_id` 重交时比 `sample_fingerprint`（含 `raw.hex()`），不同即 `SAMPLE_IDENTITY_CONFLICT`（`release_samples.py:91-100`、`:381-393`）。
- 发布一致性检查比的是 `query` 文本与计划（`release_evaluation.py:500-505`）——**`query` 不能在存储前改写**。
- 推论：只要「判定、bundle、`raw_sha256`、`body_sha256`、fingerprint」都基于**同一份已脱敏字节**，重放与幂等对新行自洽；若判定用原文而存储脱敏，命中凭据的样本会在重放中报 `STORED_OBSERVATION_INTEGRITY_MISMATCH`/`SAMPLE_DECISION_MISMATCH`。
- F13 watermark 的 `evidence_snapshot_sha256` 只覆盖 `opspilot_evidence`，不含读数（`opspilot/knowledge/store.py:1528-1546`）；恢复会话 `sample_id` 每次 `uuid4()`（`sampler.py:225`），无基于 raw 的去重。**已有键不受影响**（旧行不改，新行自洽）。

### 1.4 判定是否会因脱敏改变

- 恢复：`_single_value` 只看 `status`、`resultType`、序列条数和 `value` 数字（`prometheus.py:818-846`）。脱敏只改字符串值/键；数值串（`"0.5"`）与数字时间戳不受规则影响；有改动时 `redact_json_bytes` 规范化重写 JSON（`redaction.py:494-522`），Python float repr 可往返。→ 推断：恢复判定对脱敏不变（需测试证明）。
- 发布：标签集合是序列身份（`release_evaluation.py:300-335`），用到 `namespace/pod/uid/replicaset/owner_kind/owner_name/container/image_id` 等（`:366-445`）。定向检查：这些标签名都不命中凭据键规则；`image_id="docker-pullable://busybox@sha256:…"` 会被 URL userinfo 规则改成 `docker-pullable://[REDACTED_CREDENTIAL]@sha256:…`（误报），但 digest 正则 `(?:[^\s]+@)?(sha256:…)`（`:441`）仍取到同一 digest。唯一会改判定的情形：两条序列只差一个凭据形态的标签值，脱敏后身份相同而值不同 → `ValueError` → `QUERY_FAILED`（`:332-338`），发布结果偏向 unknown（失败闭合）。
- 非 UTF-8 响应体：`redact_json_bytes` 原样返回（`redaction.py:494-498`），即**不脱敏**；而 `json.loads(bytes)` 能自动识别 UTF-16/32，所以理论上一个 UTF-16 的合法 JSON 体今天可以判为 ok 且带未脱敏凭据入库。Prometheus 实际只返回 UTF-8（未确认是否有测试覆盖 UTF-16）。

### 1.5 展示、导出与 F13

- web 证据页对恢复读数做展示时脱敏：bundle 各 body 解码后 `redact_json_bytes`、非文本 body 不显示、`value`/`query` 过 `redact_payload`，有改动才带 `display_redacted: true`，`hashes_verified` 仍针对存储字节（`opspilot/web/app.py:697-698`、`:711-808`）。现有单测 `tests/test_redaction_payload.py:166、278、329`。
- web 不展示发布原始读数（`opspilot/web` 无 `release_query_results`/`release_history` 引用）；发布原始读数只被重放与 F11 验收读取。
- 重放 CLI 输出「不含原始字节」（`docs/development.md:198`）。
- **F13 复盘输入不含原始字节**：`read_input` 只取读数的 `signal_name,status,value,sample_count,window_*,source,raw_sha256`，不取 `raw` 和 `query`（`opspilot/knowledge/jobs.py:595-601`）；生成层模块说明「with its sample readings (no raw bytes)」（`opspilot/knowledge/generation.py:15`），并对所有行派生字符串做读时 `_scrub`（`generation.py:274-285`、`:600-630`）。→ issue 里「这些读数会进入 F13 模型输入」只在元数据层成立；原始 body 从未进入模型，旧行也已有读时脱敏。#254 不需要改 F13。

### 1.6 定向验证（唯一执行的命令）

`.venv/bin/python -I -c …`：`redact_credentials('docker-pullable://busybox@sha256:aa…')` → `docker-pullable://[REDACTED_CREDENTIAL]@sha256:aa…`；`docker.io/library/nginx@sha256:…`、`sha256:…` 不变；`redact_json_bytes` 把 `"session":"abc"` 标签值整体替换、`value:[1700000000.123,"0.5"]` 不变；上述 release 用到的标签名对 `_credential_name` 全部不命中。

### 1.7 与在途 PR 的文件重叠（`gh pr diff --name-only`）

- #299（F2 告警关联）：新增迁移 `0018_alert_correlation.py`，改 `persistence/grants.py`、`web/service.py`、`web/store.py`、`docs/development.md`（l.50 附近）、`tests/integration/test_f11_release_observation_postgres.py`、`test_f11_release_store_postgres.py`、`test_schema_*`。
- #298（F11 发布接入）：改 `opspilot/web/app.py`（import 区 l.40-60、l.152-182、l.587 起的接收端点）、`scripts/f11_u2_harness.py`、`scripts/kind_lab.py`。
- #303（F11 校准策略）：改 `observation/policies/release-calibrations.json`、`tests/f11_release_support.py`、`tests/integration/test_f11_pr2b_*`、`test_f11_release_observation_postgres.py`。
- 推荐方案（§3）只改 `opspilot/observer/prometheus.py`（可能加 `sampler.py` 一处）+ 新测试文件 + 新任务记录，**与三者无文本重叠**。冲突风险：
  1. 若改为加迁移，编号必须在 #299 的 0018 之后，且碰 `test_schema_*`/grants 测试 → 冲突。推荐不加迁移。
  2. 若顺手改 web 展示逻辑，与 #298 同改 `web/app.py`（import 区）→ 冲突。推荐不动 web。
  3. 新测试若写进 `test_f11_release_observation_postgres.py` → 与 #299、#303 三方冲突。放新文件。
  4. 语义耦合：`scripts/f11_u2_harness.py:48、2477` 直接用 `PrometheusReadOnlySource`；U2 校准正在 kind 上跑，harness 在 capture 和 fold 两个阶段各快照全部 `opspilot/**/*.py` 并记录哈希（`f11_u2_harness.py:985-1040`、`:703-711`）。未找到强制 capture/fold 代码一致的检查（未确认完整）。干净响应体经脱敏字节不变，所以行为不变，但 #254 在一轮 U2 的 capture 与 fold 之间合并会让两阶段代码快照不同。建议在当前 U2 轮次 fold 完成后合并，实施期间不在本机跑 make check。

## 2. 九类必答

1. **时间**：已定。不引入新时间量。`sent_at` 在请求发出时读（`prometheus.py` `_Sent`），`sample_time` 在全部查询返回后读（`sampler.py:207-212`）；脱敏是进程内 CPU（≤30 KiB/体，线性，`redaction.py:401-470` 注释），在 `sample_time` 之前完成，可忽略。
2. **计数与口径**：已定。`issued`、`max_samples`、`used_sample_count` 不变；`sample_count` 来自 coverage 查询数值，不受标签脱敏影响（§1.4）。
3. **状态转移**：恢复判定不变（推断，需测试）。发布判定只在「两条序列仅差凭据形态标签」时从可判定变为 `QUERY_FAILED`→unknown；非 UTF-8 体的处理也可能把 ok 变 failed。→ **待决 U2**（判定基于脱敏后内容）。
4. **主体与权限**：已定。脱敏在 Observer 进程内；Observer 角色在两表上的 INSERT 列不变（`0003:375`、`0015:183`），web 的 SELECT 不变（`0015:182`），不加授权。从 `opspilot.redaction` 导入（纯模块，只依赖 `json`/`re`，`redaction.py:3-6`），不碰 Observer 禁止导入的 `opspilot.tools`（`tests/test_m1_observer.py:1237`、`:1279`）。
5. **数据合同**：无 schema 变化即可落地：恢复 raw 上限由 bundle 截断循环保证（`sampler.py:470-498`）；发布 raw 有 `≤30720` 与 sha CHECK → 脱敏后变长需处理（可逆默认 R3）。不 bump HealthProfile revision、发布策略 revision 或 `PROJECTION_REVISION`（都不覆盖观察读数）；重放不检查 bundle `format`（`sampler.py` 只写不读 format）。部署顺序按 C3 §11（全部运行时停机、`verify_head`）——无迁移时就是普通代码发布。→ 是否需要迁移列为 **U3**（推荐不需要）。
6. **旧数据与已归档证据**：推荐不回写（与 F7 合同第 6 类「已归档证据不回写」一致，`2026-10-15-f7-contract.md:51`）：回写会让旧行 `raw_sha256`/`body_sha256`/fingerprint/`release_evaluation` 与重放不符，F6 第 5 步与 F11 重放会报完整性不一致；`docs/evidence/` 与 `tmp/lab-ledgers/` 里的归档按原哈希引用。旧行继续靠 web 展示时脱敏（`display_redacted: true`）；F13 已有读时脱敏且不读 raw（§1.5），**不需要新增 F13 读时处理**。跨部署的同一会话会出现新旧行混合，重放逐行验哈希，互不影响。发布样本同一 `sample_id` 跨版本重交只在内存里的证据跨重启时才会发生；Observer 重启后会取新租约（未确认 release sampler 是否持久化未提交证据）。→ **U4**。
7. **并发与水位**：已定无影响。不改锁、租约、水位、`adopted_sequence`。
8. **外部调用**：已定。不加模型调用和新数据源；对 Prometheus 的请求、超时、`IN_FLIGHT_LIMIT` 不变；失败语义只在 R3 的变长边界上新增一种失败闭合（TOO_LARGE）。
9. **验收投影与 `passes`**：F6/F7/F13 `passes: true`、F11 `false`（`feature_list.json`）。#254 不改验收步骤文字，也不翻 `passes`；它收紧了 F7 第 3 步「evidence exports … secrets are absent」在 Observer 存储侧的覆盖（以前只靠展示兜底）。需要复跑 F6 重放验收、F7 第 3 步、F11 重放相关测试，证明没有回退。是否需要真实 Run 列为 **U5**。

## 3. 决定表

### 3.1 需用户裁决（不可逆、合同、权限、验收）

| 编号 | 问题 | 选项与代价 | 推荐 | 影响面 | 裁决 |
|---|---|---|---|---|---|
| U1 | 脱敏还是省略原文 | a. **全部响应体脱敏**（`redact_json_bytes`，200 体与错误/部分体都脱敏），只有非 UTF-8 体省略为空字节。保留诊断，现有「错误体保留 2048 字节前缀」「中断体保留前缀」的合同测试不用改（`tests/test_m1_observer.py:990-993、1043-1085、1533-1541`，干净前缀脱敏后字节不变）。代价：错误体是先在读取时截到 2048 字节再脱敏的（`prometheus.py:709`），跨边界的令牌可能留下不足 8 字符的碎片（`redaction.py` 已知限制，F7 记录 l.29）。<br>b. 完整 200 体脱敏，错误/部分/超时体一律省略。更严，与 #253「数据源错误体不登记」先例一致（`executor.py:1404-1410`）；这些体从不参与判定（恢复 `sampler.py:646-653`，发布 `release_evaluation.py:290-298`），所以判定不变。代价：推翻 PR #131/#133 的「保留前缀」合同，约 5 个已有测试要按合同变更重写，诊断信息丢失。<br>c. 整个 raw 省略 → F6 第 5 步与 F11 重放无法重建，**不可行**。 | a | Observer 存储表示；F6/F11 重放；现有 Observer 合同测试 | |
| U2 | 判定基于脱敏后的内容（在解析前脱敏） | a. **在 `PrometheusReadOnlySource` 收到响应体后、`_single_value`/`_vector_series` 之前脱敏**：在线判定、bundle、`raw_sha256`/`body_sha256`、发布 fingerprint、重放都基于同一份字节，构造上一致（与 #253 由脱敏字节重算 rows 的做法相同）。代价：敌意输入下发布判定可能变化——仅差凭据形态标签的两条序列合并 → `QUERY_FAILED` → unknown；非 UTF-8 体（含理论上的 UTF-16 JSON）变成 failed。两者都是失败闭合。<br>b. 用原文判定、只在落库前脱敏：在线判定不变，但命中凭据的样本在重放里报完整性不一致（恢复 `STORED_OBSERVATION_INTEGRITY_MISMATCH`、发布 `SAMPLE_DECISION_MISMATCH`/`RAW_SHA256_MISMATCH`），F6 第 5 步「独立重建判定依据」对这些样本不成立。<br>c. 存原文哈希 + 脱敏字节（像 webhook 的 `audit_text`）：需要新列或 bundle 新字段、迁移和重放改造，范围大。 | a | 观察判定语义（敌意输入边界）；F6 第 5 步；F11 重放 | |
| U3 | 是否需要迁移或 revision bump | a. **不迁移、不 bump**：现有列与 CHECK 能容纳脱敏后字节（R3 处理变长）；没有 revision 覆盖观察读数；web 用「展示是否改动」区分新旧行，不需要标记列。<br>b. 加标记列（例如 `redacted_at_persistence`）或把 bundle `format` 升到 `/3`：新旧行区分更直观，但迁移要排在 #299 的 0018 之后，并碰 schema/grants 测试。 | a | 部署、#299 冲突面 | |
| U4 | 旧数据处理 | a. **不回写**；旧行继续展示时脱敏并带 `display_redacted: true`；F13 维持现有读时 scrub（已覆盖旧行，且不读 raw）。<br>b. 回填迁移重写旧行：破坏旧行哈希、fingerprint 与重放，与 F7 合同第 6 类冲突；还需要 web 角色以外的写权限。 | a | 旧证据、归档、F6/F11 重放 | |
| U5 | 是否需要真实 Run / LangSmith trace（AGENTS「触碰调查 loop、报告校验或恢复路径」） | Observer 不在调查 loop，不产生 LangSmith trace，也不调模型；「恢复路径」在 AGENTS 原文里可能指故障恢复（C3 §7），也可能指恢复观察，存在歧义。<br>a. **不要求真实 Run/trace**：证据为真实 PG 集成 + 本机 HTTP 假 Prometheus 走真实 socket（`_fetch`）+ 重放 CLI 一致；kind 实验正被 U2 校准占用。<br>b. 要求一次有界的 kind Observer 会话（恢复 + 发布各一次）：要等 U2 轮次空出来，本机 swap 压力大（用户记忆）。 | a（等 U2 空出来后可选补 b，不阻塞合并） | PR 证据要求、排期 | |

### 3.2 可逆默认（Agent 自定，各一行理由）

| 编号 | 问题 | 选项与代价 | 推荐 | 影响面 | 裁决 |
|---|---|---|---|---|---|
| R1 | 脱敏放在哪 | 客户端（单点，覆盖恢复、发布与 U2 harness）vs sampler/store 各写一处 | 在 `PrometheusReadOnlySource.instant`/`instant_vector` 读完响应体后统一处理；理由：单一出口，判定与存储同源，和 #253 的执行器单点一致 | `observer/prometheus.py` | |
| R2 | 哈希语义 | 对存储字节 vs 对原字节 | `raw_sha256`、`body_sha256`、fingerprint 都对脱敏后的存储字节计算；理由：与 #253 r1 一致，重放不用改 | 重放、幂等 | |
| R3 | 脱敏后变长超限 | 截断 vs 失败 | 脱敏后 200 体超过 `RESPONSE_LIMIT_BYTES`（30720）→ 与原来的超限同样处理：`failed/TOO_LARGE`、`body_complete=False`，保留截断后的前缀（截断发生在脱敏之后）；理由：`ReleaseQueryResult.raw` 的 `max_length` 与 0015 CHECK 都是 30720，否则 sampler 构造时直接抛错 | 发布采样不中断 | |
| R4 | 非 UTF-8 响应体 | 原样 vs 省略 | 换成空字节，`status`/`detail`/`http_status` 照记；理由：无法检查的字节不入库，与 web 展示规则（`app.py:735-742`）一致，判定本来就是 NOT_JSON/failed（UTF-16 例外见 U2） | 存储表示 | |
| R5 | `query`/`expr`/`signal_name`/`source` 是否在存储前脱敏 | 脱敏 vs 不动 | 不动；理由：这些是配置不是数据源返回，发布一致性检查逐字比较 `query`（`release_evaluation.py:500-505`），改写会让每个样本都不一致；展示层已脱敏 `query` | 一致性检查 | |
| R6 | web 展示代码 | 改 vs 不改 | 不改；新行展示前后无差，自然不带 `display_redacted`，旧行照旧；理由：避免与 #298 冲突 | `web/app.py` | |
| R7 | F13 | 改 vs 不改 | 不改；理由：不读 raw，读时 scrub 已覆盖（§1.5） | 无 | |
| R8 | 测试放哪 | 现有文件 vs 新文件 | 新文件；理由：避开 #299/#303 正在改的 F11 集成测试文件 | 合并冲突 | |
| R9 | 分支与提交 | — | 分支 `feature/F7-observer-redaction`，worktree 独立；提交 `fix: redact credentials in observer readings before storage [#F7]`；按 AGENTS 属于「数据流与凭据」用户门，由用户合并 | 流程 | |

## 4. 建议的 PR 范围

一个 PR，对应 #254 一条缺陷：Observer 的 Prometheus 客户端在解析前把响应体过 `redact_json_bytes`（U1-a、U2-a、R1-R4），恢复 bundle 和发布 `raw` 因此天然是脱敏后的字节。不加迁移，不改 web、F13、grants，不碰发布接入、告警关联和校准策略。任务记录新建一份（两页内），合同节写 U1-U5 裁决与 R1-R9 理由。

需改文件：
- `opspilot/observer/prometheus.py`：`instant()`、`instant_vector()` 收到体后统一脱敏（含错误/部分体），处理变长超限与非 UTF-8；从 `opspilot.redaction` 导入。
- （视实现）`opspilot/observer/sampler.py`：仅当 bundle 需要标注省略时；按 R4 可以不改。
- `docs/development.md`：Observer 节（l.173、l.189 附近）加一句「响应体在解析和入库前脱敏」——与 #299 的 l.50 改动不重叠。
- 新任务记录 `docs/tasks/2026-10-22-f7-observer-redaction.md`；ROADMAP 状态行（如有对应行）。
- 新测试文件（见下）。

## 5. 测试清单

验收与合同测试由未参与实现的全新上下文 Agent 按本表与公开接口编写（AGENTS「独立审查」）。

单元（无 PG）：
1. 客户端：本机 HTTP 服务走真实 socket，分别返回带凭据的 200 体（标签值 `url="https://u:p@h/x?token=…"`、`session` 标签、`api_key=…`）、401 错误体回显 `Authorization: Bearer …`、分块中断的部分体、超时部分体、非 UTF-8 体；断言 `InstantResult.body`/`VectorResult.body`/`series` 里没有合成秘密，`status`/`value`/`http_status`/`detail`/`body_complete` 与同形无秘密响应一致。
2. 无命中时字节逐字节不变（现有 `test_m1_observer.py` 全部通过就是回归证明；不得修改其断言）。
3. 变长：脱敏后超过 30720 → `failed/TOO_LARGE`，不抛错。
4. 恢复判定不变：同一组响应带/不带凭据标签，`take_sample` 得到的读数状态、值、点数、`evaluation` 相同；`replay_sample` 对存储 bundle 重放一致。
5. 发布判定：凭据标签不影响 `evaluate_release_sample`；`image_id="docker-pullable://busybox@sha256:…"` 仍映射到正确 digest；仅差凭据标签的两条序列 → `QUERY_FAILED`（若 U2 选 a，作为显式记录的行为）；`sample_fingerprint` 对同一证据重交稳定。

真实 PostgreSQL 集成（本地 PG，新文件）：
6. 恢复：Observer 经假 Prometheus 采样 → `submit_sample`；直接 SELECT `opspilot_observation_signal_readings.raw`，解开 bundle 各 `body_b64`，合成秘密零命中；`raw_sha256 = sha256(raw)`；`python -m opspilot.observer.replay --session` 退出码 0；web `GET /incidents/{id}/evidence/{sample}:{signal}` 的 `hashes_verified=true`、不带 `display_redacted`。
7. 旧行回归：直接插入含秘密的旧行，web 仍脱敏显示并带 `display_redacted: true`，存储字节不变。
8. 发布：`submit_release_sample` → SELECT `opspilot_release_query_results.raw` 零命中；`replay_release_history` 无 `RAW_SHA256_MISMATCH`/`SAMPLE_DECISION_MISMATCH`；同一 `sample_id` 重交返回 `replayed`。
9. 以 Observer 真实角色（非 owner）写入，证明不需要新权限（参照 `tests/integration/test_f11_replay_partial_evidence_postgres.py` 的非 owner 路径）。
10. F13：新会话的复盘输入（`read_input` → 生成层载荷）合成秘密零命中（回归）。

回归复跑（定向，避开 kind 观察窗）：`tests/test_m1_observer.py`、`tests/test_redaction_payload.py`、`tests/test_f6_recovery_outcome.py`、`tests/integration/test_m1_02_replay_postgres.py`、`test_m1_02_recovery_outcome_postgres.py`、`test_f11_replay_partial_evidence_postgres.py`、`test_f11_release_store_postgres.py`、`tests/acceptance/test_f7_step3_source_secrets*.py`。完成前跑一次 `make check`，放在 U2 观察窗之外。

真实 Run：按 U5 裁决；推荐不要求 LangSmith trace（Observer 无 trace）。

## 6. 未确认项

- release sampler 未提交的证据会不会跨进程重启保留（影响跨版本同 `sample_id` 重交，§2-6）。
- U2 harness 在 fold 阶段会不会校验代码快照与 capture 一致（只核对到记录，没找到比较逻辑）。
- 是否有现有测试固定了非 UTF-8 或 UTF-16 响应体的存储字节（R4 可能需要调整的测试）。
- 恢复存储层是否在别处比较 `query` 文本（只核对了发布侧；恢复侧 `_required_signals_of` 未见比较）。
