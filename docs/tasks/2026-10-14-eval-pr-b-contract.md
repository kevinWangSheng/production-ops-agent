# 场景评测 PR-B 合同预审（原文）

- 预审：Codex gpt-6-astra（high，只读，全新上下文），2026-10-14，基线 `origin/main` `0f12a8e`；D16 按 `origin/chore/eval-judge-model@4c4daac`。
- 裁决状态：用户 2026-10-14 裁决 B1 = B（当前模型真实调用、工具按冻结请求键精确匹配，缺键即 `REPLAY_MISS`）、B2 = B（冻结证据快照与录制结束时刻业务钟，回放 Run 与源 Run 显式映射），均按推荐，只影响 PR-B2；B3–B8 为可逆默认，lead 采纳预审推荐。拆步按第 5 节：PR-B1 持久化边界与表（#222）→ PR-B2 录制与回放 → PR-B3 judge 接入与校准，依次合 main。
- PR-B1 范围：第 4 节「summary、aggregate 与 #222」与 B5、B8；不含录制、回放、judge 网络调用。

---

**PR-B 预审结论：建议先裁决 B1、B2，再按三个有界步骤实施。旧六包不能直接充当 D2 完整录制源；#222 两项应在统一 loader 重建时校验，并给 `aggregate()` 加同源防御检查。**

核查基线：`HEAD = origin/main = 0f12a8e`；D16 按本地远端引用 `origin/chore/eval-judge-model@4c4daac` 预审。未修改文件、提交、stash、checkout 或运行真实评测；已有用户改动保持原状。下文是供调用方 `-o` 捕获的结果正文。

### 1. 已核查事实

引用缩写：**T**＝`docs/tasks/2026-10-12-eval-suite.md`；**D**＝同目录 `2026-10-12-eval-suite-contract.md`；**I**＝`2026-10-13-eval-pr-a-interface.md`；**U**＝本地 `tmp/prb-prereview/upstream/`，固定提交 `46e3a728d762b774d2ec458357decb8698541ce5`。

**A｜模型与工具不是同一个录制接缝。**

- 初始消息在 `opspilot/investigation/context.py:600–634` 组装；普通/最终请求在 `loop.py:640–695`，压缩请求在 `:925–943`。物理调用统一经过 `:1108–1129`，包含重试；只录已提交步骤会漏掉失败调用。
- `loop.py:184–203` 的 `serialized_request()` 是实际 HTTP body，包含完整 messages、tools、模型与生成参数；timeout 不在 body。`client.py:162–168` 调用它后发请求，凭据只在 `:201–208` 的认证头。现有请求 hash 在 `loop.py:1071–1085`；messages hash 另用 canonical JSON（`context.py:1042–1046`），两者不能混用。
- 工具执行在 `loop.py:826–859`：`executor.execute()` 返回完整 view，之后才经 `visible_view()` 变成实际工具消息；超大视图可能仅送 provenance stub（`context.py:280–290`）。因此须同时录**工具请求/结果、完整视图及实际送模内容**，不能把存储中的完整 view 自动认作已交付全文。
- transport 的 raw、实际查询窗、来源时间、lookback 等字段见 `tools/executor.py:322–416`；view 包含 evidence/operation ID、query、版本、窗口与时钟（`:1463–1515`），raw hash 与 view hash 分别计算（`:1552–1564`）。
- 产品业务时钟来自 PostgreSQL `clock_timestamp()`（`web/store.py:477–492`）。授权框在提交时冻结（`tools/otel_demo.py:767–807`）；实际查询窗由参数或授权框内默认窗确定（`:1173–1208`）。window points 的首计算点为 `window.start + lookback`（`:1057–1069`），不能回放时换成“现在”或重新补点。
- `reasoning_content` 会进入后续同一调查请求（`loop.py:728–736`），但只可返回同供应商、同 Run，不能送 judge/LangSmith（`PRODUCT-CONSTRAINTS.md:48`）。LangSmith 会删私有字段并省略部分消息（`tracing.py:313–318,407–420`），**不能反向作为精确请求录制包**。

**B｜六个旧包只能有限复用。**

逐一读取 `docs/evidence/m1-01-alignment-c-effect/{normal-1,normal-2,fault-1,fault-2,pc-fault,cart-fault}/ledger.json`：大小约 **0.69–2.21 MB**，共 **76 条 evidence**，重新计算 view hash 全部一致；30 个模型步骤的私有协议字段均为 withheld 占位。例：`normal-1/ledger.json:215,250,254` 同时保留输入 hash、占位文字和请求 hash。

因此旧包可用于**可见证据校准选样、旧格式 reader、结构反例**；不能据其重建完整物理请求字节、失败请求与全部时钟，也不能冒称 D2 完整录制。新 D2 源必须重新 live 捕获。ADR-0006 `:15–20` 已规定新原始 ledger 不入库，历史包不改写；P11–P13 见 D`:151–159`。

**C/D｜judge 与校准已有纯函数，尚缺可信持久化边界。**

- 被测输入白名单只有 question/subject_id/target_id（`evaluation/model.py:395–401`）；judge 输入由 `runs.py:303–357` 组装，剔除 flag/variant、未引用证据及其他 Run 的记录。真实接入须始终传 Outcome，不能使用 `outcome=None` 的宽松回退。
- `evaluate()` 拒绝 `started=False`（`runs.py:199–200`）；`aggregate()` 却直接占槽并读取 `overall`，校准也直接信任 `passed`（`rounds.py:224–247,257–259,292–299`）。只读内存复现结果：**started=False 仍 attempted=1、passed=1；零样本的伪造校准使 baseline_complete=True**。
- `calibrate()` 已有计数算法（`rounds.py:153–197`），但输入只是 bool 标签与 verdict，不能独立证明 P7 的场景分布、盲标、来源隔离和裁决。
- M0 配置只接受固定键且固定 DeepSeek profile（`scripts/m0/config.py:10–23,63–115`）；不能把 `.env.judge` 交给它。D16 分支模板 `evals/judge.env.example:1–9` 指定独立文件及 Z.ai 按量端点。
- Z.ai 官方文档确认 `POST https://api.z.ai/api/paas/v4/chat/completions`、Bearer、`glm-5.3` 与非流式协议；这支持 OpenAI 兼容接入，尚未证明本地凭据可用。[官方接口](https://docs.z.ai/api-reference/llm/chat-completion)

**上游实际读取结果。**

U `tests/llm/utils/classifiers.py:97–120,215–252` 用 OpenAI/Azure 客户端和独立 classifier 配置，expected＋output 判二值，输出上限 16384；不能直接满足本项目逐诊断证据复核。其打印 key 后缀及 Braintrust rationale 的做法（`:199–247`）不照搬。

U `test_ask_holmes.py:102–112` mock 的是提示词日期；`:665–668` 使用 mock DAL；`:308–313` 的 replay 是写出 skill 后重新调查，`:574–579,682–685` 仍真实调用模型。U `.claude/skills/create-eval/references/running-evals.md:21–26,49–57` 给出并行、重复次数和独立 classifier 配置；`conftest.py:135–145` 的 RUN_LIVE 只控制基础设施 setup/cleanup。

**未核查：**完整树 API、`mock_dal.py` 补取及 `gh issue view 222` 均因网络失败；因此不作“上游没有通用录制机制”的结论。#222 原文未读到，两项按用户给出的描述及上述实际复现处理。

### 2. 决定表

**需用户裁决**

| 编号 | 问题 | 选项与代价 | 推荐 | 影响面 | 裁决 |
|---|---|---|---|---|---|
| B1 | 模型回放怎样判未命中 | A 每轮完整请求必须等于旧请求，非确定性输出很容易导致后续全 miss；B 当前 loop 真调用模型，工具按冻结请求键查录制；C 近似匹配/临时 live 补查，会改变证据集合 | **B**。完整请求 hash 用于本次发送对账；工具仅精确匹配，不要求新模型生成旧回答。结构重放才逐请求核旧字节 | D2/D13 的回放含义与失败率；规则见第4节 | |
| B2 | 回放业务时间与身份 | A 直接接当前 PG 时钟，会过期且可能重算视图；B 独立模拟上下文保留旧证据时间，实际调用另计耗时 | **B**。结构模式按录制事件推进虚拟业务钟；模型模式使用冻结证据快照、业务钟固定在录制结束时刻，真实 monotonic 执行360s/7200s保护。新 attempt/回放 Run 与源 Run 显式映射；旧证据 ID/hash 不变 | 回放只证明冻结证据上的调查质量，不证明当前 PG 租约、恢复或 freshness；控制事件无法表示时拒绝该 fixture | |

**可逆默认：实施者记录理由后执行**

| 编号 | 问题 | 选项与代价 | 推荐 | 影响面 | 裁决 |
|---|---|---|---|---|---|
| B3 | 录制接缝 | 仅工具层漏实际输入；仅模型层无法审计视图来源 | 模型客户端边界＋工具执行器 facade 双录制，关联 EvidenceSink 和实际交付；理由：覆盖已有物理请求、工具与投影接缝 | 不重写产品 loop 语义 | |
| B4 | 原始包存放 | 入公开仓库违反 ADR；只依赖 trace 无法精确回放 | ignored `tmp/eval/<recording_id>/` 私有包；仓库只留 R1 摘要/manifest/aggregate 与合成结构 fixture；理由：沿 ADR-0006，不新增远端存储 | 原始包丢失后不能重新完整校验 | |
| B5 | #222 保存摘要在哪里校验 | 各消费方自行检查易漂移；只验 hash 无法发现自洽伪造 | 一个 loader 严格解析原始输入，经 `evaluate/calibrate/aggregate` 重建，比较保存派生字段；聚合入口复用必要不变量检查；理由：复用 PR-A 算法 | summary、校准、表生成均只消费已验证结果 | |
| B6 | judge 客户端和配置 | 直接复用 DeepSeekClient 会带入专属协议；新通用框架过重 | evaluator 内独立 Chat Completions 客户端，借鉴上游独立配置；`.env.judge` 专用白名单 loader、零重试；理由：已有 SDK，改动有界 | D16 三供应商身份和 P9 上限 | |
| B7 | 校准工件 | 只存最终布尔值不可复核；完整盲标记录多一些文件 | 版本化样本、双标注、裁决、verdict 分文件，冻结输入包 hash；理由：直接落实 P7，避免 hash 自引用 | 不改变既有门槛 | |
| B8 | 表与冻结写入 | 从缓存 aggregate 渲染快但可能失真 | 独占、原子写入；表从 loader 重算 aggregate，旧件只读、补证新 revision；理由：沿 P10/P12/P13 | 固定槽位与失败不丢失 | |

### 3. 与 r7/D16 的冲突及九类覆盖

**已裁决合同之间：无新增实质冲突。** D`:140,171` 嵌入的旧预审仍写 judge 与 DeepSeek 共用并发；r7 裁决说明 D`:81` 和 T`:45` 已明确改为 judge 单独串行，应遵循后者，不重新裁决。

有两个实施限制：完整请求若直接上传 LangSmith/judge，会违反 `PRODUCT-CONSTRAINTS.md:48`；把旧占位包标为 D2 完整录制，会与 T`:23` 冲突。

| 九类 | 已定或待决 |
|---|---|
| 时间 | 授权框、UTC与旧 hash 保真已定 T`:33`、D`:165`；回放钟及身份映射待 B2 |
| 计数与口径 | 已定 P10、D`:157–159`：setup 前登记、固定槽位、缺结果 unknown、结构模式质量率 null；miss 匹配待 B1 |
| 状态转移 | 已定 P1/P2：judge 不改产品状态，补证新 revision；B5/B8落实读写 |
| 主体与权限 | 已定 P3、D16、产品约束`:48`；回放隔离和原证据映射待 B2 |
| 数据合同 | 已定 P4/P11–13、R1–R4；B3–B8补文件与接口，无 DB 迁移 |
| 旧数据兼容 | 已定 P13、v4`:27`；旧件保真、缺项 legacy/unknown，不能自动升级合格 |
| 并发与水位 | 已定 T`:25,36,45`；跨进程保证 DeepSeek并发1、judge串行；每 attempt 独占槽位，保存 input/control watermark |
| 外部调用 | 已定 D10/P9/D16；无套件预算；模型回放禁业务源访问，结构模式零网络/凭据 |
| 验收与 passes | 已定 T`:10,30`、I`:201`：仍投影 Scenario→Outcome，不翻 passes、不声明发布或生产资格 |

### 4. PR-B 公开接口草案

**以下为拟议接口；B1/B2 待裁决，其余沿 PR-A DTO 与公开接口 I。**

```python
load_recording(path: Path, *, expected_hash: str) -> Recording
record(driver: Callable[[CaptureHooks], IncidentOutcome],
       *, destination: Path, context: RecordingContext) -> RecordingRef
replay(recording: Recording, *, mode: Literal["replay", "structural"],
       attempt: AttemptRecord, manifest: Manifest,
       investigator: ModelClient | None) -> ReplayCapture

load_judge_config(path: Path, *, environ: Mapping[str, str]) -> JudgeConfig
judge_once(request: JudgeInput, *, config: JudgeConfig,
           calibration: VerifiedCalibration | None,
           transport: JudgeTransport) -> JudgeObservation

load_calibration(path: Path, *, expected_pack_hash: str,
                 expected_judge_hash: str) -> VerifiedCalibration
load_summary(path: Path, *, manifest: Manifest,
             resolve: Callable[[ArtifactRef], bytes]) -> RunEvaluation
load_round(path: Path, *, resolve: Callable[[ArtifactRef], bytes]) -> Aggregate
write_summary(path: Path, capture: EvaluationCapture) -> ArtifactRef
write_baseline(round_path: Path, *, resolve, destination: Path) -> ArtifactRef
```

`driver` 由 PR-C 提供，PR-B 只提供录制 hooks。无需先实现第二个 live driver。

**录制文件格式与 hash。** `recording.json` 使用 `opspilot-replay-1`，固定：

- `recording_id/source_run_id/source_manifest_hash`；当前及来源版本；原始输入引用、实际模型刺激、授权窗、权限、registry/profile、初态、输入/控制 watermark、业务时钟策略。
- 有序 `events[]`：`seq/kind/parent/request_ref/response_ref/error/clock/identity`。涵盖每次物理模型请求（包括失败、重试、压缩）、工具调用及拒绝、结果、实际 delivery。
- 每个 `ArtifactRef={path,sha256,bytes,media_type,visibility}`；路径只容许包内相对普通文件，拒绝越界/符号链接。内容寻址二进制文件保存原始字节，JSON 元数据用产品 canonical 算法。请求 body SHA、messages SHA、raw SHA、view SHA 分别标名。
- 私有协议正文只在私有原始包内；从不进入 summary、judge、公开 fixture 或 trace。新模型回放不发送源 Run 的私有协议字段，只使用新调用产生的同 Run 字段。凭据头不录制。
- 单请求沿8 MiB、单模型响应2 MiB，工具 raw 沿2 MiB及现有视图限制；流式落盘、去重，不新增套件预算。100次模型请求仅请求/响应上界就约1 GiB，不能把旧包2 MB当容量上限。任何截断必须标 incomplete，不能保持“完整包”标记。

**回放比较口径。**

结构模式：零客户端调用、零凭据读取；按事件顺序核完整请求字节与所有 hash、消息/调用配对、交付、窗口及权限。合成 fixture 必须标 synthetic，不能代替 live 录制证明。

模型模式：真调用当前模型并录当前实际请求；冻结刺激、授权、工具 schema/registry/profile/projection 必须兼容。工具查找键为：

```text
fixture_hash + tool/version + target/source + typed canonical params
+ authorized/default window + permission/registry/profile/projection hashes
+ occurrence
```

只忽略回放新生成的 step/call/operation ID，另存映射；保留值类型、数组顺序、查询字符串、显式参数与缺省之别。同键多次响应按源 occurrence 消费；不跨 fixture、不近似匹配、不补 live 查询。缺键、耗尽或不兼容即 `REPLAY_MISS`，停止该 Run，计失败分母。模型早结束不因录制存在未使用尾项而自动 miss。新 prompt/model 在当前 manifest 冻结，不能把它们与源版本的差异误当旧请求 hash 损坏。

工具返回原 view 字节/hash，保留窗口、计算点、lookback、来源时钟；实际交付另存本次消息。只送 stub 时不得把未交付全文作为 P6 可见证据。

**summary、aggregate 与 #222。**

沿 D`:157–159` 的 P12 字段，schema 分别为 `opspilot-eval-summary-1/aggregate-1`；manifest 沿 PR-A。额外固定 `recording_ref/source_run_id/replay_run_id/delivery_refs`。trace 使用 P13 的多条 `entries[]`，区分当前 attempt、investigation、judge 与来源 trace；来源 trace 不能冒充当前 required trace。

统一 loader 必须：

1. 拒绝重复 JSON 键、未知版本/字段、错误类型、非有限数、坏 hash及身份不符；不接受 `"false"`/`0` 代替 bool。
2. 从保存的 Scenario/Answer、Outcome、报告、实际交付、授权、trace、judge 原始结果调用 `evaluate()`；保存的 gate/overall/failure 等必须逐项一致，否则 `DERIVED_RESULT_MISMATCH`。
3. `started=False` 的 RunEvaluation → `ATTEMPT_NOT_STARTED`，不能占槽。另有 setup 前写出的 attempt journal；started但缺summary合成 unknown失败，完全未开始才计 not_started。
4. 校准必须从冻结标签＋两次实际 verdict 重算，绝不信缓存 `passed`。`aggregate()` 同时拒绝未开始 DTO和明显不自洽的校准计数，防绕过 loader。
5. 缺外置证据 → `EVIDENCE_UNAVAILABLE`；可以展示历史摘要，但不能返回“已验证”结果或新 baseline_complete。读旧件不改原 verdict/hash。

**judge 实现合同。**

只发送固定系统评分指令＋PR-A `judge_request(..., outcome=...)` 白名单 JSON，不附整个 recording、ledger、既有分数、私有协议字段或对话历史。GLM 使用上述按量端点；三家 adapter 各自固定 API模型名、响应身份、参数与输出协议，不能拿 DeepSeek专属 `serialized_request()` 通吃。

360秒覆盖连接和完整响应；发送前查8 MiB，读取时查2 MiB，输出上限16384，关闭客户端自动重试。HTTP/身份错误→error，超时→timeout，超限→oversize，非JSON/重复键/缺字段/非stop/定位引用错误→invalid；均失败，不静默修复。按供应商、阶段保留 usage/耗时/余额差，缺值为unknown。

`judge_config_hash` 覆盖供应商、端点、请求/响应身份、prompt、参数、schema与adapter版本，排除密钥；与校准包hash一起绑定manifest和结果。`calibrated` 由有效校准记录推导，不采信模型输出。

隔离验收至少包括：修改 answer/flag/场景路径不改变调查客户端首请求；固定假模型回复和工具结果后，全调查请求序列不变；给 judge DTO混入私有字段不能到达 transport；修改未引用证据不改变 judge body。不能只测试 `investigator_input()` 返回值。

**P7 标注与冻结。**

`calibration/{pack}/` 保存 `items.json`、两份 `annotations-<annotator>.json`、`adjudications.json`；实际内容放私有包，仓库留允许公开的清单/hash/结果。item 固定场景、debug/validation、来源族、P6输入hash；输出为 `item_id/input_hash/annotator_identity/diagnoses[]/verdict`，diagnoses沿 `JudgedDiagnosis`。

两名全新上下文 Agent只看P6材料和规则，不看彼此、judge输出或旧评分；分歧包括关键诊断遗漏，须有用户裁决记录绑定两份标注hash。先冻结裁决与标签，再跑judge。loader验证六场景各2调试＋4验证、同源不跨集合、至少12负例，每验证项两次；错误响应不得伪装成负标签上的“正确False”。有效判定经P6复核后调用现有 `calibrate()`，再按D16比较三家；需用户选择时停在选择记录，不自动换模型。

通用固定错误码另含：`SCHEMA_INVALID/HASH_MISMATCH/IDENTITY_MISMATCH/RECORDING_INCOMPLETE/CONFIG_INVALID/JUDGE_IDENTITY_MISMATCH/CALIBRATION_UNRESOLVED/OUTPUT_EXISTS`。写入不得覆盖旧件；错误正文不含凭据或供应商原始异常。

### 5. 建议拆步与 PR-C 依赖

建议拆成依次合 main 的三步，避免 stacked PR：

1. **PR-B1：持久化边界与表。** loader/writer、#222、attempt journal、旧格式兼容、aggregate与表。
2. **PR-B2：录制与两种回放。** 完成B1/B2裁决后的双接缝、hash/时间/交付合同与合成fixture。
3. **PR-B3：judge接入与校准流程。** 独立配置、三家adapter、盲标冻结、D16选择记录；真实校准通过前不记基线。

**实现接口不依赖 PR-C；完整真实录制验收依赖新的 live 数据。** 可在B2提供hooks后由PR-C接线录制，再完成B2真实对账验收；若坚持PR-B先完整验收，则需显式纳入最小live录制执行项，不能用旧六包或合成fixture替代。PR-D负责CI入口与第一轮基线入库，边界不变。

### 6. 风险与未核查项

- **未核查：**#222全文、固定提交上游完整树及mock DAL实现；不能宣称全部指定上游材料已核查。
- **未核查：**三家真实凭据、Sol确切API模型标识与端点能力、余额、限流、真实调用、LangSmith回读、kind/PG部署状态及P7校准。
- 本次实际执行的是只读结构检查、76条view hash复算和#222最小内存复现；未跑全仓测试。
- 精确工具匹配可能产生较高miss率；必须与质量失败并列披露。原始包过期后只剩摘要，不再具备完整重算能力。以上限制不改变既定阈值或passes。

