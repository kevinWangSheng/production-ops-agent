# 场景评测 PR-B2 公开接口（B1/B2 裁决、B3/B4）

- 依据：PR-B 预审第 4 节与用户 2026-10-14 裁决；B1 的 ArtifactRef/codec/summary 原样复用。本文先于实现保存，供独立测试作者使用。
- 范围：注入式录制、结构核验、冻结证据上的模型回放。合成测试标 synthetic，不代表 live 完整录制或生产验收。不调用 judge、数据库或基础设施。

## 模块、类型与入口

新增 `opspilot.evaluation.recording`、`opspilot.evaluation.replay`，公共符号从 `opspilot.evaluation` 导出。DTO 为 frozen dataclass；所有 Mapping 仅接受严格 JSON，时间为 UTC datetime；hash 为 64 位小写 SHA256。

```python
ToolBinding(tool: str, version: str, target: str, source: str,
            authorized_window: Mapping[str, Any], default_window: Mapping[str, Any] | None,
            permissions_hash: str, registry_hash: str, profile_hash: str,
            projection_hash: str)
RecordingContext(recording_id: str, source_run_id: str, source_manifest_hash: str,
                 scenario_id: str, scenario_version: str, synthetic: bool,
                 versions: Mapping[str, str], stimulus: Mapping[str, Any],
                 initial_state: Mapping[str, Any], input_watermark: str,
                 control_watermark: str, started_at: datetime, ended_at: datetime,
                 bindings: tuple[ToolBinding, ...], input_refs: tuple[ArtifactRef, ...] = (),
                 source_versions: Mapping[str, str] | None = None,
                 clock_policy: Literal['event-clock/frozen-end-real-monotonic'] =
                    'event-clock/frozen-end-real-monotonic')
RecordingEvent(seq: int, kind: Literal['model','tool','delivery','http','refusal'], parent: int | None,
               request_ref: ArtifactRef, response_ref: ArtifactRef | None,
               error: str | None, clock: datetime, identity: str,
               hashes: Mapping[str, str])
Recording(path: Path, ref: ArtifactRef, context: RecordingContext,
          events: tuple[RecordingEvent, ...], outcome: IncidentOutcome | None,
          complete: bool, incomplete_reasons: tuple[str, ...])
RecordingRef = ArtifactRef
record(driver: Callable[[CaptureHooks], IncidentOutcome], *, destination: Path,
       context: RecordingContext) -> ArtifactRef
load_recording(path: Path, *, expected_hash: str) -> Recording
replay(recording: Recording, *, mode: Literal['replay','structural'],
       attempt: AttemptRecord, manifest: Manifest, investigator: ModelClient | None,
       driver: Callable[[ReplaySession], IncidentOutcome] | None = None,
       destination: Path | None = None) -> ReplayCapture
```

`destination` 为新包目录；入口独占创建，拒绝覆盖，默认私有权限。非 synthetic 包必须位于本仓库 ignored `tmp/eval/<recording_id>/`；回放新录制也使用同规则。load 的 path 指向 recording.json。返回 ref.path 为 recording.json；B1 summary 的 resolver 以该包为根，直接传 ref（不把原始包复制到 summary）。完整性失败仍落 recording.json（complete=false），随后抛固定码，便于检查中断；硬进程终止时目录中保留 INCOMPLETE 标记，绝不视为完整。失败目录不自动删除。

`ReplayCapture(attempt, outcome, recording_ref, source_run_id, replay_run_id,
 business_time, structural_verified, error, id_map)`：attempt 是执行后副本，miss 时 status=aborted、replay_miss=True；结构成功不声称质量通过。recording_ref 是新录制引用（结构模式 null），source/replay Run 显式不同，旧 view/evidence ID/hash 不变。`id_map` 为新 operation_id -> 旧 operation_id。模型模式必须有新 destination、driver 和 investigator；结构模式忽略 investigator/driver/destination，不读取凭据，也不调用它们。

## 注入协议

复用 `investigation.loop.ModelClient.complete(ModelCall)->ModelReply`，每次 complete 是一次物理请求，包含重试和压缩；沿产品 `serialized_request()` 保存实际 body，不把 timeout 混进 body hash。reply 是边界完整规范化 ModelReply，另录原始 HTTP 字节。

`CaptureHooks.model(client)->ModelClient` 返回代理；`CaptureHooks.tools(executor)->ToolFacade` 返回带 execute(ToolRequest)->ToolOutcome 的透传代理，其余属性转发。工具结果含完整 model_view 与 evidence.raw，不额外查询后端。绑定按 tool_name/target_ref 精确查 context.bindings，别名须由 driver 先冻结成独立绑定。

`CaptureHooks.delivery(view, message)->None`：view 是完整视图，message 是实际工具消息（含 tool_call_id/content）。驱动将此函数注入 `InvestigationLoop.capture_delivery`，在 visible_view 投影后调用；包括缓存命中与拒绝。delivery 保存完整视图和实际消息，不能将 stub 解释为全文交付。

`CaptureHooks.http(body: bytes, response: bytes | None, status: int | None)->None` 注入 `DeepSeekClient.capture_http`，只接 body/response/status，不接请求头。挂点在 HTTP body 序列化后，成功读取或失败时调用。只有启用录制才增加回调；默认路径不改变序列化、hash、控制或报告逻辑。真实非 synthetic 录制每次 model 事件必须恰好有一个 http 子事件；假客户端的 synthetic 录制可省略。

`CaptureHooks.clock(at: datetime)->None` 更新后续事件业务时间，必须不倒退；非 synthetic 驱动结束时须调用 `CaptureHooks.finish(at)`，以最后一次业务钟读数封口并覆盖 context.ended_at（输入 ended_at 可先填 started_at）。封口后拒绝新增事件，缺封口为 RECORDING_INCOMPLETE。synthetic 可使用预先冻结的 ended_at。只支持无中途控制变更的冻结快照；`CaptureHooks.control(event)->None` 无条件抛 SCHEMA_INVALID 并标 incomplete。控制 watermark 是初态身份，不暗示已经实现控制事件模拟。

`ReplaySession` 提供 `.model`、`.tools`、`.context`、`.clock`、`.capture_delivery`；driver 必须从 context.stimulus/initial_state 建立隔离调查状态，不导入源模型历史。clock.now() 固定为源 ended_at，clock.monotonic() 使用真实 monotonic。tools 仅从源包取结果，不含 live executor。调用模型或工具前后检查真实 7200s Run 上限；模型调用 timeout=min(原 timeout,360,Run剩余)，由客户端负责中断物理请求；返回超时按失败处理。ModelClient 必须遵守 timeout_seconds，不为不可取消的任意 Python 客户端承诺强行杀线程。新请求另录，旧 reasoning_content 等私有协议字段不得由 driver 复制进新 Run；代理仅允许新客户端在本 Run 产生的 reasoning_content 回送。ModelReply.raw 只允许产品规范化 id/usage，未知私有协议不能借 raw 注入。

## 文件与 hash

recording.json 为 canonical JSON：`{schema:'opspilot-replay-1',context:<DTO>,events:[<DTO>],outcome:<DTO|null>,complete:bool,incomplete_reasons:[str]}`；所有字段必填，未知字段/版本拒绝。事件 seq 从 0 连续，identity=source_run_id，clock 在始末之间单调；parent 指向已开始的关联事件：http→model、delivery→tool/refusal（可为 null 表示缓存）、refusal→原 tool；refusal 的严格格式见末尾 P2 修复合同。事件按调用开始排序，失败也保留；hashes 精确按 kind 定义：

- model：request_ref 是 `{call:<ModelCall全部字段>,body:<UTF8原HTTP body>}`；response_ref 是完整 ModelReply；hashes={request_body_sha256,messages_sha256}，前者对 serialized_request 字节，后者对 canonical messages。
- http：request_ref 原 body，response_ref 原响应（或 null）；hashes={request_body_sha256,status}，status 为十进制 HTTP 状态字符串或 `unknown`。须与父 model 字节一致。
- tool：request_ref 是 `{request:<ToolRequest>,binding:<ToolBinding>,key:<sha256>,occurrence:<从0开始整数>}`；response_ref 完整 ToolOutcome（evidence 含 raw，raw 编码为小写 hex）；hashes={view_sha256,raw_sha256}，无 evidence.raw 时 raw hash 为 SHA256(b'')。
- delivery：request_ref 为完整 view，response_ref 为实际 message；hashes={view_sha256,delivery_sha256}，实际内容单独校验，不要求等于完整视图（允许 stub）。有工具消息时必须与后续模型请求工具消息相符；尾部未再送模的 delivery 仅表示已生成，不能算实际送达。

所有 ArtifactRef 沿 B1 的全部 5 字段，visibility=private；原始字节落 `blobs/<sha256>`，同内容去重、独占发布、逐件 fsync。JSON metadata canonical 算法沿 B1：sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False，无换行。原请求保持产品原有空格与键序。model 请求正文、messages、raw、view hash 分别核对，禁止互换。无完整包总量预算；单请求8 MiB、模型原响应/规范化响应各2 MiB、工具 raw 2 MiB、view 8 MiB、metadata 8 MiB；超限不写截断数据，落 incomplete。reader 按引用大小限读，拒绝路径越界、任一符号链接与非普通文件，逐件核 bytes/SHA。私有原文不进异常、summary、judge、trace；公开测试只用 synthetic 数据。

工具键为 SHA256(canonical JSON({fixture_hash, binding, tool_name,target_ref,params,window}))；fixture_hash 在录制期间用 source_manifest_hash（包内冻结命名空间），回放再强绑定源 recording.ref.sha256，不跨包共享索引，避免 recording hash 自引用。occurrence 单独记录，按同键源出现顺序消费。binding 包含 tool/version、target/source、授权/默认窗与四个兼容性 hash；参数保留类型、数组顺序、查询原字串、显式字段和缺省字段之别。只忽略 ToolRequest.step_id/tool_index 导出的新 operation ID，并记录映射；不删除 params 内同名键。模型模式校验 manifest tools_schema/registry/profile/projection/permissions 与 context.versions 对应值一致（必须已冻结为 hash/version，unknown/none 不兼容）；prompt/model/code 允许在当前 manifest 独立冻结变化。工具多余尾项允许；缺键/耗尽/绑定不兼容停止该 Run，绝不近似或补 live。

## 判定与错误

`RecordingError` 复用 B1 StorageError（别名），`.code` 与 str 只含固定码，不带原始异常链。结构模式重新 load 源包，核字节、事件配对、模型 tool_calls 与 tool delivery、引用、窗口与冻结绑定；零客户端调用。模型模式同样先完整核验源包，随后运行新 driver；REPLAY_MISS 捕获为 ReplayCapture 的失败 attempt，其余 driver/timeout 失败为 error=DRIVER_ERROR/TIMEOUT，不把失败当质量通过。

| 码 | 条件 |
|---|---|
| REPLAY_MISS | 模型模式工具键缺失、耗尽、当前绑定/冻结版本不兼容 |
| RECORDING_INCOMPLETE | INCOMPLETE 标记、complete=false、未配对/缺失物理调用/交付、driver 中断、超大小上限；不得当完整包使用 |
| HASH_MISMATCH | recording expected_hash、blob字节/SHA、请求正文/messages/raw/view/delivery独立hash不符 |
| IDENTITY_MISMATCH | attempt/manifest/模式/场景/Run绑定错误，seq/parent/clock/工具调用与交付身份不符 |
| SCHEMA_INVALID | 严格JSON/DTO错误、未知字段/版本、非法路径/hash、重复binding、无法表示的控制事件、配置缺项 |
| OUTPUT_EXISTS | 目标目录已存在（包括符号链接），不覆盖 |
| EVIDENCE_UNAVAILABLE | 输入/输出不可读写、缺引用、符号链接/非普通文件 |
| DRIVER_ERROR / TIMEOUT | 当前 driver 失败/超真实monotonic时限；失败录制保留incomplete |

最小调整理由：草案 replay 增加 driver/destination，复用调查驱动以允许当前模型改变调用；不在 B2 重写 loop。工具 namespace 用源 manifest hash 并由 recording hash 隔离，避免自引用。无可表示控制事件时一律拒绝。原始 HTTP 可选挂点与实际 delivery 挂点补齐双接缝遗漏；synthetic 允许无 HTTP，真实录制不允许。B2 不声明完成 live driver、真实录制验收或独立审查，这些由 lead 后续接线和审查。

实现对账补充：回放新包 initial_state.replay_source 固定 {run_id,recording_hash,id_map}，id_map 为新 operation_id 到旧 operation_id，核验冻结 ToolOutcome 的原 operation/evidence 身份；不改旧视图或 evidence 内容。ReplaySession 另提供 capture_http，驱动在创建当前 ModelClient 时将 HTTP 回调桥接到该函数。AttemptRecord.status 沿 PR-A 使用 aborted，配合 replay_miss 计失败，不新增 status 枚举。

实施时收紧：工具 facade 当前只能接受已冻结绑定的请求；未冻结工具/目标（包括无法表示的注册拒绝）拒绝 fixture，不猜测绑定。缓存 delivery 必须来自包内已录工具视图或 initial_state.evidence_views。模型请求消息仅允许产品消息字段，reasoning_content 须匹配本回放已产生的完整 assistant 配对，单 Run model 名称固定；未知私有协议字段拒绝。增加 finish(at) 是为了让 live driver 在调用结束后冻结真实业务钟，不预猜录制结束时间。逐件读 blob，索引只保留引用/hash，不累积所有请求/工具原文。

格式补齐：context 的 input_refs/source_versions/clock_policy 显式保存原始输入引用、来源版本和固定时钟策略；JSON 中包括默认字段。`CaptureHooks.input(raw: bytes)->ArtifactRef` 保存8 MiB以内原始JSON输入（严格JSON、无凭据/私有协议），driver 应录原始业务输入，禁止含答案/flag的评测封套。synthetic 未给输入时自动冻结 stimulus 的 canonical JSON；非 synthetic 缺原始输入即 incomplete。source_versions 默认取录制 versions，回放另存源版本；新包复制源输入blob并单独记录当前实际模型请求。补齐显式字段的理由是避免原始输入来源与时钟策略只藏在通用 Mapping 中。已知人控终态（paused/cancelled及pause/cancel/resume/follow_up/correct交互）与控制事件同样拒绝；压缩/重试逐物理调用记录，RESPONSE_TOO_LARGE即incomplete。

独立验收分诊澄清（2026-10-14；不改变只读、精确查找、私有协议或完整性合同）：

- context.versions/source_versions 保存版本标识或 hash 字符串，manifest 比较 DimensionValue.value（kind 为 hash/version）；binding 四个 *_hash 是独立的内容 SHA256，不要求等于对应版本标签。理由：版本名与内容摘要是两个冻结维度，原接口没有规定二者相等（歧义 5）。
- 两个录制边界各自保留原始请求：模型 arguments 与 facade 的 typed params 不要求相等，driver 可在 facade 前完成既有默认值/授权参数组装；结构核验关联工具名、同名调用顺序与 delivery.tool_call_id，模型 arguments 和工具 params 均不得改写。理由：接口没有把某个调查 loop 的参数派生方式强加给所有注入 driver；工具查找仍完整比较 facade 的 typed params/window 与冻结 binding。
- model_view 是任意严格 JSON 对象，不要求嵌入 operation_id/evidence_id；若含 evidence_id 则必须与 EvidenceRecord 一致，完整 view/hash 仍须等于 evidence.view/hash。缺 operation_id 的新 delivery 按完整 view 的字节 SHA 与待交付工具事件的来源顺序关联，不做工具请求近似匹配。理由：这些身份已在 ToolOutcome/EvidenceRecord 的显式字段中，不能虚构或额外要求视图字段。
- 源包始终以传入 Recording.ref.sha256 重新读取、逐件验证，工具索引只属于该已核验包；manifest.replay_fixture 可用版本标签，若明确声明 kind=hash 则必须等于该 SHA。理由：原接口未要求 manifest 的版本标签伪装成文件 hash，来源强绑定由已经验证的 Recording.ref 实现。
- 结构模式不创建产品 Run，允许 attempt.product_run_id 与 ReplayCapture.replay_run_id 为 null；若提供 Run ID 仍不得冒充源 Run。模型模式仍必须使用与源不同的显式产品 Run。理由：零调用的静态核验不需要虚构运行身份，原接口未要求结构模式创建 Run（补充歧义 11）。
- DeepSeekClient.capture_http 是默认 None 的可赋值属性，也接受同名构造参数；后赋值替换当前 hook，赋 None 关闭。理由：原「注入到 capture_http」两种读法均成立，采纳歧义 10 的实例赋值方式。
- synthetic 的 stimulus 自动输入仅在 driver 返回且未调用 input()/未提供 input_refs 时补入；显式原输入和回放复制的源输入不会另附一份默认输入，finish 后仍允许落此派生元数据，不允许新增事件。理由：落实「未给输入时」的条件，避免回放输入引用重复。
- delivery 先核自身 view/delivery hash，再核父事件身份与消息配对；自身 hash 不符为 HASH_MISMATCH，字节/hash 均正确但与后续实际模型工具消息不符为 IDENTITY_MISMATCH。理由：区分直接内容损坏与跨事件身份错误，采纳歧义 12 的消息不符读法。
- 尾部未再送模的 delivery 仍须对应录制模型回复中存在的 tool_call_id，并来自已录工具视图或冻结缓存；它只表示已生成，不计实际送达。理由：原第 75 行的「模型 tool_calls 与 tool delivery 配对」不会因未发送而失效；不能凭空添加未知 call ID。


独立审查 P2 修复合同（lead 2026-10-14 已裁决；适用于 record 完整性检查、load/结构核验以及模型回放的源包校验）：

- 工具跨事件关联：operation.requested_tool/requested_target 必须与 ToolRequest.tool_name/target_ref 按 typed canonical JSON 相等；view 如带 operation_id，必须等于 operation.operation_id。只要 operation.dispatched=true，无论 evidence 是否为空，都必须检查窗口关联：显式 request.window 严格解析为 Window 且等于 operation.window；request.window=null 时，operation.window 等于 binding.default_window，未设默认窗则等于 binding.authorized_window；已记录的 operation.window 仍须在授权框内。dispatched=true 且 evidence=None 时，view 不得有 rows、非 null content、evidence_id 或其他源数据，不能宣称 adopted/citable；只接受与 operation/outcome 一致的网关元数据，RESULT_TOO_LARGE 可含产品固定的 message/query/大小或 token 限制提示（不是源数据），否则报 IDENTITY_MISMATCH。豁免只适用于由 _unqueried_refusal() 白名单核验的未执行查询拒绝：dispatched=false、source_contact=none、合法 denied/error、无 EvidenceRecord、无源数据，此时允许 operation.window=None，不解析/比较请求窗或默认窗。未查询 view 仅允许 operation_id/trust/status/reason/source_contact/tool/target_id/window/requested_at、content=null、citable_as_fact/adopted=false，未知数据字段及元数据槽中隐藏数据均拒绝。其他关联矛盾也报 IDENTITY_MISMATCH，回放在使用源包前拒绝。理由：复验2 P2：是否执行以 dispatched 判定，不以 evidence 是否为空判定。 固定提示须按模板重建逐字相等：用限制字段调用产品 executor 的同一字符串函数重建 message；字节模式须完整给出 message/max_result_bytes，token 模式须完整给出 message/view_tokens/max_view_tokens/query/window，不允许缺字段或混用两种模式。query/window 继续按已有规则核对，计数保持严格整数校验；多余空白、伪造文本、限制字段与提示不一致均报 IDENTITY_MISMATCH。理由：复验3 P2。
- 无证据结果及未执行拒绝中，operation.query 如非 null，必须逐字等于冻结 facade request.params 的产品 canonical JSON；view.query 如出现，还须与该已绑定 query 的对象逐字段相等，不能仅比较两份响应字段。产品 _accept_params 只验证、复制参数，不补默认值、不改变值，已生成 query 不能额外携带请求之外的数据，也不能改写空白。理由：复验4 P2：允许字段须可由冻结请求/注册/模板重建。本条仅落地 query 绑定；运行审计字段及完整注册快照的收口仍待明确，不能据此宣称所有允许字段已可重建。
- `DUPLICATE_TOOL_CALL` 是 loop 在 facade 之前产生的拒绝，CaptureHooks.delivery 自动在对应 delivery 前添加 kind='refusal'，不需要额外产品挂点或调用 executor。该事件 parent 指向最初产生被引用证据的 tool 事件；request_ref 为严格对象 `{operation_id,tool_call_id,duplicate_of_evidence_id}`（均非空字符串），response_ref 为完整拒绝 view，hashes 精确为 `{view_sha256}`。operation_id 为本次被拒调用自己的 ID，不能占用源工具或其他拒绝的 ID；delivery.parent 指向该 refusal。其 error=null，clock/identity/seq/私有 refs 沿既有规则。
- refusal 必须对应当前模型真实声明的 call ID；工具/目标/解析后参数的重复身份按现有 loop._call_fingerprint 与被引用工具的原模型调用相等，证据 ID 必须由包内既有 tool view 产生，完整拒绝字段必须等于现有 loop._duplicate_call_view 的投影。它不是旧 view 的全文缓存，也不能生成新 adopted evidence。理由：合法拒绝有新的 operation_id 和新的投影，不能误当作不相同的缓存 view，也不能放行任意伪造网关结果。
- 模型回放分别索引物理 tool 与 refusal：refusal 按其 parent 工具的精确冻结请求键及拒绝出现次序消费，在 ReplaySession.capture_delivery 校验；不经 facade，facade 不会合成一个未发生的 ToolOutcome。当前 loop 产生的拒绝除新 operation_id 外须与冻结拒绝逐字段相等；新/旧拒绝 operation_id 加入 id_map，证据 ID 不变，原证据及 hash 不变，当前实际 view/消息另录。不命中、耗尽或拒绝字段不兼容 → sticky REPLAY_MISS，停止该 Run；旧尾项可不消费。理由：重现既有 loop 的拒绝，保留身份映射与真实交付，不绕开冻结包查询或补 live。
- 新增固定原因码 `HTTP_BODY_INCOMPLETE`，用于 http.error 与 recording.incomplete_reasons，不是供应商文本、也不是新的 outward RecordingError：record 仍抛 RECORDING_INCOMPLETE。启用 capture_http 时，已知 HTTP 错误正文在读取中抛 IncompleteRead/HTTPException/OSError（含读取超时），或 Content-Length 与读取长度不符/非法、错误头已收到但预算耗尽而未能读完时，以 `(body, None, 已知status)` 回调一次；不把部分正文冒充完整空 bytes。真正的完整空响应仍为 `(body,b'',status)`；未取得响应的连接失败仍为 `(body,None,None)`。reader 即使遇到被改写的 complete=true 也不能接受该截断事件。关闭录制时仍沿原读取、返回 `(b'',status)` 与 ModelError/重试路径；不改认证头或默认请求 body。理由：请求失败可以有完整录制，已知截断不能标为完整。


复验 P2-1/P2-2（lead 2026-10-14 裁决）：

- 每个 refusal 和物理 tool 一样占用其模型 call ID，录制时更新 _claimed_calls；同一模型回复里的 [A,重复A,B] 必须把后续物理 B 关联到 B，不能复用已拒绝调用的指纹。ReplaySession 的预检不提前占用 ID，实际拒绝交付落盘时才占用，避免重复预检被误拒。
- refusal view 不加入缓存分支可用的 tool_views；独立的父事件关联索引可以持有 refusal view。每次 DUPLICATE_TOOL_CALL 交付必须具有自己唯一 operation_id 的 refusal 事件，严格保持 delivery→refusal→tool 链路；不接受 parent=null 或直接指向物理 tool 的拒绝交付，即使视图/消息/全部 hash 与历史拒绝自洽，也报 IDENTITY_MISMATCH。理由：拒绝是本次调用的独立动作，不是历史拒绝全文缓存。
