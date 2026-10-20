# PR-B3 judge 与校准公开接口（r10 / B6、B7）

状态：实施接口；工作区 `poa-eval-b3`，分支 `feature/eval-b3-judge`，基线 `70bfb18`。依据 [PR-B 预审](2026-10-14-eval-pr-b-contract.md) 第4节 judge/P7 与第5节第3项、[r8/D16](2026-10-12-eval-suite-contract.md)。不改变 PR-A/B1、产品状态或 passes。所有接口同步，测试只注入假 transport。

## 配置、adapter 与调用

公开入口在 `opspilot.evaluation.judge`：

```python
load_judge_config(path: Path, *, environ: Mapping[str, str], provider="zai") -> JudgeConfig
judge_config_hash(config: JudgeConfig) -> str
judge_once(request: JudgeInput, *, config: JudgeConfig,
           calibration: VerifiedCalibration | None, transport: JudgeTransport) -> JudgeObservation
```

`JudgeConfig` 为 frozen dataclass：provider（zai/openai/deepseek）、endpoint（完整 chat/completions URL）、request_model、response_model、api_key（repr隐藏）、prompt、adapter_version（`judge-adapters-2`）、schema_version、timeout_seconds=900、max_request_bytes=8388608、max_response_bytes=2097152（流式下指组装后 completion 文档字节；线上 SSE 字节另有固定 32 MiB、单事件 1 MiB（只计 data 字节）上限，keep-alive 注释计入线上字节，超出同为 oversize；[judge 流式](2026-10-20-judge-streaming.md) S2）、max_output_tokens=65536（2026-10-20 用户裁决：P7 debug 推理耗尽输出上限）。参数不能扩大上限；hash 为上述全部非密钥字段的 canonical JSON SHA256，密钥只走认证头。config 的 repr 不显示密钥。

只接受 `ZAI_API_KEY/ZAI_BASE_URL/OPSPILOT_JUDGE_MODEL/OPENAI_API_KEY/OPENAI_BASE_URL/OPENAI_JUDGE_MODEL/OPENAI_JUDGE_RESPONSE_MODEL/DEEPSEEK_API_KEY/DEEPSEEK_BASE_URL`。文件必须绝对路径、当前用户拥有、普通文件、无符号链接、精确0600、≤16384字节；先用 O_NOFOLLOW 打开并 fstat，再读。空行/注释忽略，字面 KEY=VALUE，可成对引号，无执行/插值；重复/未知键拒绝。environ 不读取进程环境，只检查白名单键的冲突，不用其补齐文件凭据；无关环境键忽略。错误仅 `JudgeError.code`：CONFIG_ABSOLUTE_PATH_REQUIRED、CONFIG_PRIVATE_REGULAR_FILE_REQUIRED、CONFIG_UNREADABLE、CONFIG_SYNTAX、CONFIG_ENV_CONFLICT、CONFIG_INVALID、JUDGE_IDENTITY_MISMATCH。

Z.ai 固定 `https://api.z.ai/api/paas/v4/chat/completions`、请求/响应 `glm-5.3`。DeepSeek 固定 `https://api.deepseek.com/chat/completions`、请求/响应 `deepseek-flash`，只供校准对照。OpenAI 固定 `https://api.openai.com/v1/chat/completions`；必须显式给 OPENAI_JUDGE_MODEL 与 OPENAI_JUDGE_RESPONSE_MODEL，两个值均固定为 `gpt-6.1-sol`，缺失或其他名字拒绝；此值由lead依据Codex CLI采用，官方API可用性待真实校准第一次调用核实，本任务不联网。三家各有 ZaiAdapter/OpenAIAdapter/DeepSeekAdapter，build(config,input) 返回 bytes；不复用调查客户端。固定两条新消息（system规则、user白名单JSON），stream=true；OpenAI/DeepSeek 附 `stream_options.include_usage=true`；Accept `text/event-stream`（[judge 流式](2026-10-20-judge-streaming.md) S1/S4），Z.ai/DeepSeek 用 max_tokens，OpenAI 用 max_completion_tokens；无历史、工具或旧评分。按 SSE 组装（S12–S14：只拼接 delta.content，reasoning_content 计入线上字节后丢弃；delta.role 若出现须为 assistant 且全程至少一次；tool_calls/function_call/refusal 非 null 组装进文档判 invalid；每块 choices ≤1 且 index=0，非 null finish_reason 恰一次、其后只允许无 content 的 usage 块，choices 为空只允许在 finish 后；usage 取最后一个非 null 对象；每块须带 model 且全程相同、id 全程相同，否则组装文档 model=null 走下述身份码；流层面结构违规一律 JUDGE_OUTPUT_INVALID）后，响应需 model 匹配固定身份（OpenAI允许`gpt-6.1-sol`或有效日历日期`gpt-6.1-sol-YYYY-MM-DD`，其余为invalid；GLM/DeepSeek仍精确匹配）、单 choices、index=0、assistant、finish_reason=stop、tool_calls/function_call/refusal仅容许key缺省或显式null（包括[]在内的任何非null值均invalid）；只消费 content，私有响应协议不保留。usage 只记录非负整数 prompt/completion/total_tokens，缺值 unknown（None）；usage 与 finish_reason 只要 transport 已组装出文档即记录，含之后判 invalid（S6）。

`JudgeTransport.send(*, endpoint:str, headers:Mapping[str,str], body:bytes, timeout_seconds:float, max_response_bytes:int) -> JudgeHTTPResponse(status_code:int, chunks:Iterable[bytes])`。必须用给定端点/认证头，零重试、禁重定向；超时覆盖连接及完整响应读取，抛 TimeoutError；其他异常只映射固定码。附 HTTPJudgeTransport 基于仓库已有 httpx2（无新增依赖），解析 SSE（S7–S11：WHATWG 分帧，注释/keep-alive 忽略，event 非 message 为 invalid；200 非 `text/event-stream` 为 invalid；非 2xx 照旧受 2 MiB 读正文后 HTTP_ERROR；流中 `error` 事件为 HTTP_ERROR，坏 JSON/非对象为 invalid），返回组装后的非流式 completion JSON；收到 `[DONE]` 即停止读取，未收到 `[DONE]` 即断开为 TRANSPORT_ERROR；超时仍为单一 900 s 总墙钟，不加空闲超时，deadline 单调钟。假 transport 返回组装后文档即可，可返回多块、抛异常；judge 也独立计字节与检查耗时。生产评测每报告恰一次调用；未校准仍可取得观察，但不能通过。

## P6 输入与输出

`JudgeInput(spec:ScenarioSpec, answer:Answer, report:ReportCapture, delivered:Mapping[str,DeliveredRecord], authorized:Mapping[str,Window], outcome:IncidentOutcome)`，outcome 必填。DTO严格类型及未知字段检查。实际 body 只来自 PR-A `judge_request(..., outcome=...)` 的9键：question、subject_id、authorized_windows、report_raw、cited_evidence、expected_output、scenario_kind、injected（仅service/aliases/operation/aliases）、signature_matches。不发送 flag/variant、场景ID/路径、未引用/未交付/其他Run证据、recording或ledger。发送前对证据/报告数据键递归执行r7的敏感片段子串拒绝（含私有协议字段），不因大小写、分隔符或前后缀放行；report_raw 内JSON也检查。私有字段注入返回 invalid，transport 调用为0。修改未引用证据不会改变请求body。

content 必须是严格 JSON 对象（无重复键/未知键/非有限数），恰 `schema, diagnoses, verdict`，schema=`opspilot-judge-1`，verdict 严格 bool；每条 diagnoses 恰 PR-A JudgedDiagnosis 的9字段：location、quote、factual_causal(bool)、service/operation(str|null)、cited_evidence_ids(str数组)、linked_claim(int|null，拒bool)、supported(bool)、reason(str)。不能由模型提供 calibrated/hash/status。

代码复用 PR-A定位/引文/claim引用检查、签名匹配、因果诊断 AND 与事实证据归属检查：正常不得有事实型因果诊断；故障必须有至少一条，每条服务/完整操作匹配、supported=true且引用匹配证据；未合格的事实证据判失败。返回 verdict 必须与 PR-A重算因果AND一致，否则 invalid。`JudgeObservation`：result（PR-A JudgeResult）、valid、recomputed_verdict、passed、error_code、response_model、usage、elapsed_seconds、provider、phase（evaluation/calibration）、balance_delta（本模块不查余额，None=unknown）、finish_reason（S6，str|None，旧工件缺字段按 None 解码）；不留原响应/异常/私有推理。passed 仅有效P6、合格gate、已绑定校准且重算true。最终产品评测仍由 evaluate 再与 deterministic gate/trace做AND。error/timeout/invalid/oversize/未校准均 passed=false；HTTP/GLM或DeepSeek身份错误=error，OpenAI响应身份错误=invalid，非stop/JSON/schema/定位/引用/verdict错误=invalid。错误码固定且异常链隐藏；零修复、零重试。

## 双盲标签包、冻结与D16

入口在 `opspilot.evaluation.calibration_flow`：

```python
freeze_label_pack(path: Path, pack: LabelPack) -> str
load_label_pack(path: Path, *, expected_pack_hash: str) -> LabelPack
run_calibration(pack: LabelPack, *, configs: Mapping[str, JudgeConfig],
                transports: Mapping[str, JudgeTransport], destination: Path) -> SelectionRecord
load_verified_calibration(path: Path, *, pack: LabelPack,
                          config: JudgeConfig) -> VerifiedCalibration
```

LabelPack(items:tuple[CalibrationItem,...], annotations:tuple[AnnotationSet,...], adjudications:tuple[Adjudication,...])。
CalibrationItem(item_id,scenario_id,split=`debug|validation`,source_family,input_hash,request:JudgeInput,coverage:tuple[str,...])；input_hash为实际P6白名单输入canonical hash。恰六场景，每场景2debug+4validation，ID唯一、输入hash唯一，source_family采用r8规定的规范形式并不跨集合。验证覆盖 hidden_normal_cause/summary_claim_mismatch/extra_wrong_cause/borrowed_citation/hypothesis/no_positive_diagnosis/insufficient_evidence。来源族由作者诚实记录，程序不能证明作者独立性。
AnnotationSet(annotator_identity,model_identity,labels:tuple[BlindLabel,...])；BlindLabel(item_id,input_hash,diagnoses:tuple[JudgedDiagnosis,...],verdict:bool)。恰两份按casefold比较仍不同的规范标注者身份（模型身份可相同）、都覆盖36项，不得用三家候选judge盲标（模型名按r9片段规则排除，含装饰写法）；记录身份不证明全新上下文。每标签定位/引文/引用与重算AND均复核。两份逐条标签不同（包括漏诊）时必须有 Adjudication(item_id,annotation_hashes:tuple[str,str],decision_record:str,label:BlindLabel)；绑定两份完整annotations文件hash，decision_record是非空人工裁决记录；一致项禁止额外裁决。最终至少12个validation负例。

freeze 在新目录独占写 items.json、annotations-1.json、annotations-2.json、adjudications.json、pack.json；均0600。pack.json包含schema=`opspilot-label-pack-1`与前四文件sha256，不包含自己hash；返回该索引canonical hash。loader重算四文件与输入hash、标签、分布与裁决；拒绝符号链接、未知schema/字段、hash不符。OUTPUT_EXISTS时不覆盖；写失败留下不完整件并fail-closed。工件只能放ignored私有目录，公开仓库只记录允许的hash/结果，不能提交实验答案。

run_calibration必须收到三家配置/transport，先核包并在destination/label-pack落盘完整标签冻结件，再调用；每家同一36项，debug每项一次、validation每项两次，全程串行。每次保存 JudgeObservation 与item/input/repetition身份；关键因果诊断按最终标签的(location,quote)核漏检。错误/无效/超时/oversize、漏诊不能作为负标签上的正确False：该供应商校准直接失败，另记录错误原因，计数用judge原始result.verdict经B1 calibrate重算，再扣除错误/漏诊/P6复核失败的虚假一致数；误通过只计有效原始输出的True，不能用复核后的False替换。24验证、48比较、≥46一致、≥12负例、0误通过。结果保存为新目录 calibration.json（schema=`opspilot-judge-calibration-1`），含pack/config hashes、全部observations和重算counts、selection；loader从逐次`provider-NNN.json`与`label-pack/`逐项重建，不采信汇总observations/passed。

SelectionRecord(status=`selected|pending_user_decision|no_baseline`,selected_provider:str|null,eligible:tuple[str,...],pack_hash,results:Mapping[str,CalibrationResult])：GLM合格且agreement≥Sol即自动selected zai；否则有合格GLM/Sol则pending且selected=null；两者不合格no_baseline。DeepSeek永不eligible。待裁决记录只写候选与证据，不自动使用Sol；B3不提供伪造人工批准的快捷入口。VerifiedCalibration只能经load_verified_calibration从已选记录重建，绑定pack/config hash与合格结果；pending、对照、错误工件返回 CALIBRATION_UNRESOLVED。B1 load_calibration 保持原语义，不把旧纯bool计数件当B3认证。

## 对草案的可逆接口调整与理由

- JudgeInput使用PR-A完整原始DTO且outcome必填，理由：重复利用P6代码复核，杜绝宽松回退。
- JudgeObservation附JudgeResult/valid/复核值，理由：校准时区分调用失败和正确负例，兼容evaluate。
- OpenAI请求/响应身份固定为lead指定的gpt-6.1-sol，理由：任意型号不能满足D16；真实API可用性由第一次校准调用核实。
- 使用已有httpx2有界HTTP而非SDK，理由：asyncio总deadline覆盖连接与完整读取，零重试且不新增依赖。
- B3认证loader另名，不替换B1 load_calibration，理由：保持已测计数工件语义并补齐盲标认证边界。
- 增加七类coverage与逐诊断漏检，理由：落实P7覆盖要求，避免只比较最终bool。
- pending记录无人工选择写入口，理由：合同要求停在选择记录处，裁决由lead后续接入。

- judge 改 SSE 流式（adapter `judge-adapters-2`），理由：zai 非流式长推理 5–12 min 无字节，本机代理下长静默连接被断；流式使推理期间持续有字节，零重试与 900 s/65536/8 MiB/2 MiB 不变，见[judge 流式](2026-10-20-judge-streaming.md)。
- 余额差显式unknown并保存provider/phase，理由：本边界不自动增加余额查询，真实对账由驱动提供证据。
- run_calibration发送前自动落盘标签包副本，理由：即使调用者仅给内存DTO，也保证先冻结标签再读judge输出。

## r2：独立验收分诊后的明确约定（2026-10-14）

以下补充是公开接口的一部分。A类由实现方修复，B类由独立测试作者修正，C类由实现方澄清接口并实现；验收测试由作者依据此接口维护，实现方不改acceptance。依据仍为r8，不改变P7门槛、权限或D16选择。

1. **hash算法（歧义10、20）**：`judge_config_hash`是纯指纹函数，覆盖第16行所有非api_key字段；结构/类型不合法报SCHEMA_INVALID，但不在hash函数里校验供应商profile。timeout统一为float，使360与360.0相同；其他字段不得省略。加载、adapter和校准运行另做profile校验，hash不同不表示允许调用。理由：消除未声明的hash执行前提，变更配置仍必须重新校准。
   `input_hash`严格等于`canonical_hash(json.loads(json.dumps(judge_request(spec,answer,report,delivered,authorized,outcome=outcome), allow_nan=False)))`，其中`canonical_hash`来自`opspilot.tools.registry`。等价字节是JSON规范化后的输出经`json.dumps(sort_keys=True,separators=(",",":"),ensure_ascii=False,allow_nan=False).encode("utf-8")`再SHA256。不是HTTP body、messages、完整JudgeInput或report单独的hash；未知字段/非有限数/私有键先拒绝。理由：明确与PR-A产品canonical算法对齐，避免测试作者另猜。
2. **annotations字节（歧义22）**：每份annotations文件只取决于该AnnotationSet：`codec.canonical_bytes(annotation)`，无额外envelope、schema、槽号、pack身份或时间戳；SHA256对文件原始字节计算。移动到另一个包或1/2槽不改变字节。annotation_hashes严格按annotations-1、annotations-2的顺序；交换即拒绝。理由：使冻结前的人工裁决可绑定稳定的两份标注hash。
3. **认证路径（歧义23）**：`load_verified_calibration(path=destination,pack=pack,config=config)`正式接受run_calibration的destination目录，内部读取`destination/calibration.json`；兼容原实现的显式`destination/calibration.json`普通文件参数。拒绝符号链接、其他文件名、缺失/不可读文件为EVIDENCE_UNAVAILABLE；不搜索其他目录、不回退旧工件。已存在但未选定/不合格记录为CALIBRATION_UNRESOLVED；汇总observations/计数/selection与逐次记录重算派生值不符为DERIVED_RESULT_MISMATCH，逐次item/input/repetition身份不符为IDENTITY_MISMATCH，pack/config不符为HASH_MISMATCH。理由：消除第42行未说明文件/目录的歧义，支持目录同时保持原文件调用兼容。
4. **调查侧隔离（歧义1）**：C:158第1–2项已由`tests/unit/test_eval_b3_impl.py::test_answer_flag_and_path_do_not_change_actual_investigation_requests`覆盖，参数answer/flag/path各执行一次。使用真实InvestigationLoop及记录调用的假模型：先assert首个`serialized_request(model.calls[0])`相同，再在固定假回复/固定完整工具结果下assert所有物理请求bytes序列相同。这不是只测investigator_input；B3不新增PR-C驱动公开入口。理由：提供已有可观察证据与可搜索测试名，避免重复声明未覆盖。
5. **有界HTTP与认证类型（歧义2、3）**：总deadline/禁重定向/响应大小已有`test_http_transport_mock_redirect_size_and_total_deadline`，仅httpx2.MockTransport与本地计时，不开socket；测试不等于真实360s供应商观察。VerifiedCalibration公开类型可被Python构造/replace，但只有loader重建并逐次复核的proof绑定才能使judge通过；`test_all_three_same_samples_p7_selected_and_verified_judge`已覆盖替换pack_hash不能伪造。理由：明确“只能经loader”的信任语义，不声称Python构造器不可调用。
6. **异常与文件（歧义4–9）**：B3所有转换异常离开except块后再抛，`__cause__`与`__context__`均None；正文只带固定码，不能带路径/原始异常/密钥。配置绝对路径错误=CONFIG_ABSOLUTE_PATH_REQUIRED；最终symlink/目录/缺失/解码失败=CONFIG_UNREADABLE；非0600/非owner/文件过大=CONFIG_PRIVATE_REGULAR_FILE_REQUIRED；未知/重复键/坏语法=CONFIG_SYNTAX；值冲突=CONFIG_ENV_CONFLICT；缺选定供应商凭据或Sol双模型身份=CONFIG_INVALID（Sol两个值必须为gpt-6.1-sol）；固定模型改名=JUDGE_IDENTITY_MISMATCH，固定BASE_URL改动=CONFIG_INVALID。同值environ允许，不能补齐缺失文件键；三家键可同文件，未选provider不要求其凭据；BASE_URL缺省为第20行固定值，Z.ai模型缺省glm-5.3。只拒绝最终组件symlink（父目录可能为系统路径别名），judge凭据仍先打开/fstat再读。
   成对引号内的`$`、`${...}`、反引号都是字面文本，绝不执行/插值；未加引号的`$`/反引号以及任何NUL拒绝，行尾注释不被剥离。理由：保留安全的字面配置，消除“无插值”到底是禁字符还是禁行为的歧义。
   标签/校准错误统一JudgeError：SCHEMA_INVALID（结构/分布）、HASH_MISMATCH（hash）、CALIBRATION_SET_OVERLAP、CALIBRATION_COVERAGE_INCOMPLETE、CALIBRATION_TOO_FEW_NEGATIVES、CALIBRATION_LABEL_INVALID、CALIBRATION_UNRESOLVED（标注/裁决/选择）、DERIVED_RESULT_MISMATCH、OUTPUT_EXISTS、ARTIFACT_UNWRITABLE、EVIDENCE_UNAVAILABLE。理由：给出可测固定码及拒绝点，不扩大通过条件。
7. **请求和观察（歧义11–19）**：恰两条system/user消息，system.content严格等于config.prompt，user.content是第26行9键JSON字符串。第26行私有/凭据槽名单是发送前拒绝的最低集合，大小写/连字符/下划线/点号命名槽也拒绝；报告解析失败、outcome=None、请求超限都不发送。gate失败允许调用方跳过；调用judge_once最多发送一次且gate仍为通过必要条件。HTTP失败、GLM/DeepSeek响应身份不符为error，OpenAI响应身份不符为invalid；其余非法envelope、content、定位/引用、非stop为invalid。usage仅Mapping中的非负严格int，缺失/坏类型为None；judge_once.phase=evaluation，run_calibration.phase=calibration，余额差None。超过响应上限的块被计入消费量后立即停止、不读取下一块。理由：澄清形状和状态边界，不承诺模型输出可靠性。
8. **标签与记录（歧义21、24–27）**：场景限定D1已有六例，source_family由独立作者记录；coverage恰允许第46行七名，验证集合合计全部覆盖，调试可为空、每项可多名，未知名拒绝。固定候选glm-5.3/deepseek-flash/gpt-6.1-sol（含Sol日期快照）在freeze/load即拒绝作annotator；run_calibration还检查传入configs身份，拒绝发生在任何transport前。已有destination在发送前报OUTPUT_EXISTS；不覆盖旧件。错误、漏诊和P6复核失败均扣掉虚假一致数，false_passes按有效原始verdict计数，复核不能抵消误通过。loader允许JSON空白/键顺序变化，按语义重算，不能仅靠格式不同拒绝篡改。理由：提前堵住可在无configs时识别的候选标注者污染，补充严格读取规则。
9. **selected测试的正确写法（B类）**：第40/51行`load_label_pack`返回LabelPack，比较对象应是注入的pack fixture值。`test_selected_record_and_artifact`当前签名缺pack，名称解析为模块的fixture函数。测试作者应改为`def test_selected_record_and_artifact(selected, frozen, configs, pack):`；实现方不改返回类型以迎合函数对象。


## r3：独立审查五项修复（2026-10-14，用户/lead裁决）

1. 槽名先拆camelCase/PascalCase及缩写边界（clientAPIKey→client_api_key），再把非字母数字分隔符归一为下划线、大小写折叠；凭据和私有协议字段共用同一拒绝规则，前后缀和无分隔符的敏感复合词也拒绝。保守识别可能拒绝带token等敏感槽名的业务字段；不通过删除字段修复请求，而是invalid且零发送。理由：封堵clientSecret/clientApiKey与带前后缀私有字段的出口。
2. 配置读取失败显式记录失败标志，离开except后抛固定错误；B3两模块已逐个审计raise点，转换错误不在except内raise，cause/context都为空。配置解码错误不能保留原始密钥字节。理由：from None不能删除隐式异常链。
3. `JudgeObservation.error_code`只允许：CONFIG_INVALID、JUDGE_IDENTITY_MISMATCH、SCHEMA_INVALID、JUDGE_PRIVATE_FIELD、JUDGE_OUTPUT_INVALID、JUDGE_VERDICT_MISMATCH、JUDGE_REQUEST_OVERSIZE、JUDGE_RESPONSE_OVERSIZE、JUDGE_TIMEOUT、JUDGE_HTTP_ERROR、JUDGE_TRANSPORT_ERROR、JUDGE_RESPONSE_IDENTITY_INVALID；其他transport JudgeError.code统一error/JUDGE_TRANSPORT_ERROR，不保存原值。原始校准记录也验证此白名单。理由：transport是可注入边界，异常类型本身不赋予文本可信度。
4. 三家请求及配置响应身份固定：zai=glm-5.3、openai=gpt-6.1-sol、deepseek=deepseek-flash，端点不变。OpenAI实际响应仅容许请求同名或gpt-6.1-sol-YYYY-MM-DD（日期须有效，保存实际response_model），其余invalid/JUDGE_RESPONSE_IDENTITY_INVALID；GLM/DeepSeek仍要求同名。日期形状校验不代表该快照已由官方发布；gpt-6.1-sol是lead指定、Codex CLI使用的名字，首次真实校准调用须核实官方API是否支持；不可用就失败，不自动换模型。理由：显式配置任意名字不能替代D16的Sol对照。
5. `calibration.json`仅是缓存。loader先核`destination/label-pack`与调用方pack hash，再按items顺序读取每家`provider-001.json`至`provider-060.json`（12调试+48验证）；每项item_id/input_hash/repetition必须匹配。observations、P6、漏诊、计数及D16 selection均从这些逐次件重算，和缓存逐项比对；派生值不一致报DERIVED_RESULT_MISMATCH，文件与已绑定hash不符报HASH_MISMATCH，逐次身份不符报IDENTITY_MISMATCH，缺件报EVIDENCE_UNAVAILABLE。返回认证绑定所在目录；judge_once使用时重新读取源件及缓存，源件变化即未校准，不延用旧认证。不只检验内存_proof。理由：缓存不能把失败逐次记录改写为合格校准。


## r4：校准错误域明确化（2026-10-14，用户裁决）

- 标签语义校验（包括引文、定位、引用与verdict复核）失败一律`CALIBRATION_LABEL_INVALID`；复用P6时捕获其异常，在except外转成标签错误，cause/context均None。标签校验不透传`JUDGE_OUTPUT_INVALID/JUDGE_VERDICT_MISMATCH`等judge输出码；结构、身份及hash错误仍按原定义拒绝。理由：同一复核算法在标注域和judge输出域有不同错误合同。
- `HASH_MISMATCH`只表示内容与已绑定hash不符（包括冻结文件字节、输入及配置指纹）；不是汇总派生结果错误的别名。来源item_id/input_hash/repetition身份不符仍为`IDENTITY_MISMATCH`。
- 从逐次原始记录及冻结标签重建的observations、计数或selection，与`calibration.json`缓存不一致一律`DERIVED_RESULT_MISMATCH`。把另一轮成功observations/selection换入失败汇总、保留180份来源文件不动，仍拒绝认证，报该码；不改P6/P7/D16计算或源件权威。理由：修正r3错误码表述，保持第80行明文错误域。


## r5：PR #232 工具调用字段裁决（2026-10-14，lead裁决）

assistant响应的`tool_calls`、`function_call`等工具调用字段按存在性与null校验：key不存在或值为null视为无工具调用；key存在且值非null一律invalid/JUDGE_OUTPUT_INVALID，包括空数组[]、非空数组/对象及其他假值，不采用真值判断。原因：部分OpenAI兼容端点在无工具调用时显式返回null，拒绝null可能使所有正常响应及校准失败。理由：PR #232 机器人 P2；null 读法待真实调用核实。首次真实调用须核实该端点行为，本次仅用假transport验证。


## r6：PR #232 工件目录持久化（2026-10-14，lead裁决）

每次创建输出目录或冻结标签目录后，必须fsync父目录；每次独占创建标签文件、pack.json、provider-NNN.json或calibration.json，必须先flush/fsync文件并关闭，再fsync所在目录，成功后才允许继续下一次调用或返回。目录open/fsync/close失败一律在except外报ARTIFACT_UNWRITABLE，cause/context为空；立即中止流程，不发起后续付费调用，不重试、不回滚/覆盖已有文件。失败可能留下不完整工件，不能因此视为已完成校准。理由：PR #232 机器人 P2；逐次权威工件和冻结标签必须先完成目录项持久化，避免崩溃丢失后继续付费调用。


## r7：PR #232 凭据槽 fail-closed（2026-10-14，lead裁决）

- 证据/报告的键名先NFKC规范化并casefold为小写，去掉全部非字母数字字符；只要包含任一敏感片段就拒绝为invalid、transport零调用（槽检查报JUDGE_PRIVATE_FIELD；报告schema先拒绝时可报SCHEMA_INVALID，同样零调用）：auth、token、secret、password、passwd、apikey、credential、cookie、session、bearer、privatekey、signature、reasoning、thinking、encrypted。无词边界、大小写、分隔符或前后缀例外；客户端不得静默删字段。理由：PR #232 机器人 P1。
- 证据/报告精确键豁免清单为空，author仍拒绝。核查`docs/evidence/m1-01-alignment-c-effect/{normal-1,normal-2,fault-1,fault-2,pc-fault,cart-fault}/ledger.json`的全部76份`evidence[*].view`：103个不同键，敏感片段命中0，author及其大小写变体未出现；不扫描/发送ledger的dsn、模型私有steps等容器。现有PR-A/B3夹具证据17键、报告12键也无命中。真实键无需豁免，后续证据出现普通词误伤时先失败，不自动扩豁免。理由：PR #232 机器人 P1；仅按实际证据决定例外。
- P6固定协议元数据不是证据属性：外层严格九键白名单，`authorized_windows`限定`引用ID -> Window(start,end)`（严格UTC类型）；`signature_matches`限定`证据ID -> span ID字符串数组`。这些map键为身份引用，值按封闭schema验证，不能塞入凭据属性对象；报告raw解析后仍检查所有数据键，cited_evidence及view逐层检查。保留已批准的P6授权/签名字段和ev-signature等引用ID，不将其加入全局数据键豁免；相同名字出现在证据/报告数据里照样拒绝。理由：PR #232 机器人 P1；避免把固定协议和身份映射误作外来凭据槽，同时不改变PR-A请求语义。
- 历史r3的边界正则及排除auth/authentication的复合词规则已由本节替换；识别保守，未来普通词命中可导致invalid，不把安全失败改成通过。核对只证明现有键兼容，不证明任意值不含敏感数据或未来OTel字段都兼容。


## r8：PR #232 机器人第 4 轮（2026-10-14，lead裁决）

- 原文核对：P6/P7见`2026-10-12-eval-suite-contract.md:137–138`；PR-A接口`:130`区分原始verdict与证据资格、`:147`明确agreement=judge判定与标签相同、false_passes=标签False且judge判True；预审`2026-10-14-eval-pr-b-contract.md:147/164`要求按实际verdict重建，错误响应不得当负例上的正确False。“经P6复核”不授权用复核否决后的False覆盖原始judge判定，原文与本次修复无冲突。理由：PR #232 机器人第 4 轮。
- 每个有效验证观察按原始`result.verdict`计一致与误通过；`recomputed_verdict`仅作P6资格/复核核验，不能替换计数值。原始True、复核False增加P6_RECHECK_FAILED，provider不达标，且该调用不给一致信用：负标签时false_passes增加1、agreement不增加；正标签时原始True本来的一致信用被扣除，也作不一致。错误/无verdict/漏诊继续不获一致信用；无有效verdict在B1 bool计数API中的False仅占位，随后扣除其可能得到的负例一致数，不冒充judge判False。读回同样从逐次记录重算此口径。理由：PR #232 机器人第 4 轮。
- 来源家族采用拒绝策略，不trim/规范化后继续使用：只允许ASCII小写规范ID，正则fullmatch为`[a-z0-9][a-z0-9._:-]*(?:/[a-z0-9][a-z0-9._:-]*)*`。禁止首尾/内部空白、控制字符、大小写/Unicode变体、空路径段及`.`/`..`路径段，非规范形式SCHEMA_INVALID，在目录写入与任何transport前拒绝。它是区分大小写的规范身份，不是待解析文件路径；合法ID按原字节比较跨集合重叠，重叠仍为CALIBRATION_SET_OVERLAP。理由：PR #232 机器人第 4 轮；消除空白或非规范拼写绕过来源隔离。


## r9：标识类与候选模型按类收口（2026-10-14，lead裁决）

理由：PR #232 机器人第 5 轮，按类收口。

**统一规范与入口。** 所有下列结构化标识由同一个校验器覆盖：非空、1–256字符、ASCII字符集`[A-Za-z0-9._:/@-]`，fullmatch；不允许任何首尾/内部空白、控制字符、Unicode变体或超长，不trim、不Unicode规范化后继续接收、不改写保存值。非规范一律SCHEMA_INVALID，cause/context为空。在freeze/运行前、每条逐次记录与汇总发布前，以及load标签包、load认证、既有认证使用时均检查；运行中新返回的标识不规范时立即SCHEMA_INVALID，不发布该条工件且不继续下一次调用；源记录及汇总缓存都检查，先检查格式再比较身份/派生值。已有字节/hash完整性检查仍先于对绑定文件的内容解析，坏文件保持HASH_MISMATCH。

**逐项字段清单（`[]`为所有元素，map key同样检查）。**

1. 标签主记录：`CalibrationItem.item_id/scenario_id/source_family/input_hash/split/coverage[]`；`AnnotationSet.annotator_identity/model_identity`；`BlindLabel.item_id/input_hash`（含两份标注及裁决label）；`Adjudication.item_id/annotation_hashes[]`。source_family还须满足r8的ASCII小写、非空规范路径段规则，长度新增256上限，仍拒绝而非trim。
2. 版本/场景/主体：`request.spec.scenario_id/version/kind`、`spec.incident.scenario_id/feature_id/kind/subject_id`、`spec.stimulus.subject_id/target_id`；`request.answer.scenario_id/version`、`answer.injected.flag/service/service_aliases[]/operation/operation_aliases[]`、`answer.signature.rule_id/any_of[].service/operation`。
3. 证据与授权：`request.delivered`的key和每项`evidence_id/target_id/status/time_scope_refs[]/view_sha256`，`request.authorized`的窗口引用key；view内部已声明投影引用槽`evidence_id/operation_id/run_id/subject_id/target_id/trace_id/span_id/parent_span_id/time_scope_refs[]`和指纹`view_sha256/raw_sha256/source_response_sha256`递归检查。此外固定投影身份槽`tool/tool_version/source/registry_revision/tool_registry_revision/projection_revision/projection/trust/status`、`content[]/span_groups[]/query`中的`service/operation`、span的`service_version/status_state`、`parent_references[].refType/traceID/spanID`、`backend_trace_ids[]`和`backend_counts_by_service`的服务key同样递归校验。投影可选引用ID及service_version为null代表缺失，不接受空字符串；其他已声明固定身份槽存在时不得null。操作名沿下述span名称扩展，不将这些固定结构误作自由OTel文本豁免。
4. 报告与结局：`request.report.finish_reason`；raw报告中的`schema_version/assessment_status/conclusion`、每条claim的`kind/evidence_ids[]/target_refs[]/time_scope_ref`；`request.outcome.scenario_id/final_state/decision/evidence_ids[]/actions[]/permissions[]/handoff_reasons[]`。报告引用按原JSON检查，不能借产品文本strip绕过。时间框、日期是时间值，不是ID。
5. 逐次记录：`CalibrationObservation.item_id/input_hash`；`observation.provider/phase/response_model/error_code`及`usage`计数名key；`result.status/config_hash/calibration_hash`、`result.diagnoses[].service/operation/cited_evidence_ids[]`。同样适用于冻结标签diagnoses中的这些字段；location为P6专用定位表达式（允许claims[0]等），仍按P6/r4校验而不套通用ID字符集。
6. 校准配置/汇总/认证：`configs`、`transports`、`observations`及`SelectionRecord.results`的provider key；`JudgeConfig.provider/request_model/response_model/adapter_version/schema_version`；汇总固定schema及pack_hash，`SelectionRecord.status/selected_provider/eligible[]/pack_hash`，每个CalibrationResult的`judge_config_hash/calibration_pack_hash/reasons[]`；VerifiedCalibration的`provider/pack_hash/config_hash/result`及其冻结pack复核沿用同规则。
7. 固定文件协议：pack索引schema、files文件名集合仍严格固定；所有出现的input/config/calibration/pack/view/raw/annotation SHA256指纹都必须64位小写hex，非规范SCHEMA_INVALID、规范但与绑定内容不符仍HASH_MISMATCH。provider只允许精确小写zai/openai/deepseek；其余既有enum除格式外仍须符合原schema。

**协议允许的缺失值与内容字段。** `SelectionRecord.selected_provider`、`JudgeObservation.response_model/error_code`、诊断service/operation和报告time_scope_ref可按原schema为null（缺失不是空ID）；校准逐次JudgeResult尚未认证时唯一允许`calibrated=False,calibration_hash=""`，并由_counts继续检查状态绑定，其他绑定hash不能为空。`quote/reason`、问题/摘要/说明、expected_output、acceptance_step、human_interaction、人工decision_record及提示词为内容；`injected.variant`为配置值（已有100%），不是身份ID。普通OTel属性值、span_groups.status中的标签表达式及计数（例如rpc.grpc.status_code=0）是内容，不被猜成身份，仍受r7敏感键拒绝；顶层view.status仍是规范枚举标识。已核查PR-A六份冻结包中的76个view，标识规范校验全部放行；文件路径不是ID。服务名/别名使用通用规范；操作名/别名按span名称的受限ASCII语法`[A-Za-z0-9._:/@(){}\[\]-]+(?: [A-Za-z0-9._:/@(){}\[\]-]+)*`，总长1–256；允许单个ASCII空格分隔词及路由括号，拒绝首尾/连续空白、控制字符及Unicode变体。理由：PR-A六包实际含`HTTP POST`、`orders publish`、`router frontend egress`、`GET /api/products/{productId}`、`executing api route (pages) /api/products/[productId]`及`{closure}`；这些是既有规范span名称，不是身份trim或自由文本豁免。ASCII大小写保留，P6仍用原比较规则。endpoint仍按固定URL校验，api_key只走认证且不作为身份或hash输入。

**去重与模型排除。** annotator_identity先通过规范检查，再casefold比较；大小写不同仍视为同一标注者，报CALIBRATION_UNRESOLVED，不改写标注字节或hash。model_identity先通过规范检查，再小写去全部分隔符，含D16任一固定片段`glm53/gpt61sol/deepseekflash`即CALIBRATION_UNRESOLVED，允许误判；例如openai/gpt-6.1-sol、Z.ai-glm-5.3、DeepSeek/Flash@revision及任意前后缀都拒绝。`Z.ai glm-5.3`因空格先报SCHEMA_INVALID。两个独立标注者仍可共用同一个非候选模型；程序校验不能替代真实独立上下文证据。


## r10：工件写入上限与汇总瘦身（2026-10-18，按用户 2026-10-17 常设指示采纳推荐，#237）

理由：PR #232 机器人第 6 轮 P1。读取上限 64 MiB（67108864 字节），写入原先不设限：汇总内嵌 180 条 observation，单条平均超过约 360 KiB 就会跑完付费调用却读不回；36 项请求都接近 8 MiB 时 items.json 同样读不回。

1. **写入与读取同一上限。** 所有校准工件（标签冻结件、pack.json、provider-NNN.json、calibration.json）写入前检查序列化字节数，超过 67108864 即报 ARTIFACT_UNWRITABLE（cause/context 为空），不创建该文件，立即中止，不再发起付费调用。标签冻结件在第一次付费调用之前写入，所以 items.json 超限在零付费调用时就会失败。单条 provider-NNN.json 受 2 MiB 响应上限约束，不会触及该上限。
2. **calibration.json 只存缓存摘要。** 顶层字段固定为 `schema`（仍为 `opspilot-judge-calibration-1`，该格式尚无真实工件）、`pack_hash`、`configs`、`records`、`selection`，不再含 `observations`。`records` 的 key 为三家 provider，值为按 001–060 顺序排列的 60 个 SHA256（小写 64 位 hex），每个对应 `provider-NNN.json` 的原始字节。第 51 行「含全部 observations」与 r3 第 5 项「observations 与缓存逐项比对」按本条替换；observation 只以逐次文件为准。
3. **loader 判定顺序。** 保留现有 pack/config/身份校验，然后：先从逐次文件与冻结标签重算 selection，与缓存 selection 不符报 DERIVED_RESULT_MISMATCH（保持 r4 「换入另一轮汇总」报该码）；selection 一致而 `records` 的 provider 集合、数量或任一 hash 与逐次文件原始字节不符，报 HASH_MISMATCH；缺件仍是 EVIDENCE_UNAVAILABLE，hash 非规范、顶层字段多余/缺失、provider key 不是 zai/openai/deepseek（r9）仍是 SCHEMA_INVALID。

## r11：judge_once 对当前请求执行 r9 规范标识校验（2026-10-18，按用户常设指示采纳推荐，#250）

理由：PR #232 机器人第 7 轮 P2。原来 r9 的规范标识校验覆盖了标签包、逐次记录、汇总和认证工件，但没有覆盖 `judge_once` 收到的当前请求。

1. **范围。** 在 `judge_once` 中，`validated(JudgeInput, request)` 之后、构建请求体之前，对当前请求调用与 r9 相同的校验器。检查范围是 r9 字段清单第 2–4 类（版本/场景/主体、证据与授权、报告与结局），规则（含字符集、长度、操作名语法、null 语义）与 r9 完全一致，不另立规则。
2. **已认证与未认证一视同仁。** 带 `VerifiedCalibration` 的调用和不带认证的调用都要做这项检查，这样「既有认证使用时均检查」就覆盖到每一次评测请求。理由：未认证调用同样会把字节发往外部供应商，标识规范是数据出口约束，与是否认证无关。
3. **失败语义。** 标识不规范时，结果为 status `invalid`、code `SCHEMA_INVALID`，transport 调用次数为零，不发送任何字节；cause/context 为空。超大请求等既有检查的顺序不变：先做 DTO 校验，再做标识校验，再构建请求体，最后检查请求大小。
4. **不变项。** 规范的请求（包括 PR-A 六个冻结包里的 76 个 view）照常放行；已有的 judge 和校准测试断言不修改。
