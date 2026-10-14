# PR-B3 judge 与校准公开接口（r8 / B6、B7）

状态：实施接口；工作区 `poa-eval-b3`，分支 `feature/eval-b3-judge`，基线 `70bfb18`。依据 [PR-B 预审](2026-10-14-eval-pr-b-contract.md) 第4节 judge/P7 与第5节第3项、[r8/D16](2026-10-12-eval-suite-contract.md)。不改变 PR-A/B1、产品状态或 passes。所有接口同步，测试只注入假 transport。

## 配置、adapter 与调用

公开入口在 `opspilot.evaluation.judge`：

```python
load_judge_config(path: Path, *, environ: Mapping[str, str], provider="zai") -> JudgeConfig
judge_config_hash(config: JudgeConfig) -> str
judge_once(request: JudgeInput, *, config: JudgeConfig,
           calibration: VerifiedCalibration | None, transport: JudgeTransport) -> JudgeObservation
```

`JudgeConfig` 为 frozen dataclass：provider（zai/openai/deepseek）、endpoint（完整 chat/completions URL）、request_model、response_model、api_key（repr隐藏）、prompt、adapter_version、schema_version、timeout_seconds=360、max_request_bytes=8388608、max_response_bytes=2097152、max_output_tokens=16384。参数不能扩大上限；hash 为上述全部非密钥字段的 canonical JSON SHA256，密钥只走认证头。config 的 repr 不显示密钥。

只接受 `ZAI_API_KEY/ZAI_BASE_URL/OPSPILOT_JUDGE_MODEL/OPENAI_API_KEY/OPENAI_BASE_URL/OPENAI_JUDGE_MODEL/OPENAI_JUDGE_RESPONSE_MODEL/DEEPSEEK_API_KEY/DEEPSEEK_BASE_URL`。文件必须绝对路径、当前用户拥有、普通文件、无符号链接、精确0600、≤16384字节；先用 O_NOFOLLOW 打开并 fstat，再读。空行/注释忽略，字面 KEY=VALUE，可成对引号，无执行/插值；重复/未知键拒绝。environ 不读取进程环境，只检查白名单键的冲突，不用其补齐文件凭据；无关环境键忽略。错误仅 `JudgeError.code`：CONFIG_ABSOLUTE_PATH_REQUIRED、CONFIG_PRIVATE_REGULAR_FILE_REQUIRED、CONFIG_UNREADABLE、CONFIG_SYNTAX、CONFIG_ENV_CONFLICT、CONFIG_INVALID、JUDGE_IDENTITY_MISMATCH。

Z.ai 固定 `https://api.z.ai/api/paas/v4/chat/completions`、请求/响应 `glm-5.3`。DeepSeek 固定 `https://api.deepseek.com/chat/completions`、请求/响应 `deepseek-flash`，只供校准对照。OpenAI 固定 `https://api.openai.com/v1/chat/completions`；必须显式给 OPENAI_JUDGE_MODEL 与 OPENAI_JUDGE_RESPONSE_MODEL，两个值均固定为 `gpt-6.1-sol`，缺失或其他名字拒绝；此值由lead依据Codex CLI采用，官方API可用性待真实校准第一次调用核实，本任务不联网。三家各有 ZaiAdapter/OpenAIAdapter/DeepSeekAdapter，build(config,input) 返回 bytes；不复用调查客户端。固定两条新消息（system规则、user白名单JSON），stream=false，Z.ai/DeepSeek 用 max_tokens，OpenAI 用 max_completion_tokens；无历史、工具或旧评分。响应需 model 匹配固定身份（OpenAI允许`gpt-6.1-sol`或有效日历日期`gpt-6.1-sol-YYYY-MM-DD`，其余为invalid；GLM/DeepSeek仍精确匹配）、单 choices、index=0、assistant、finish_reason=stop、无 tool_calls；只消费 content，私有响应协议不保留。usage 只记录非负整数 prompt/completion/total_tokens，缺值 unknown（None）。

`JudgeTransport.send(*, endpoint:str, headers:Mapping[str,str], body:bytes, timeout_seconds:float, max_response_bytes:int) -> JudgeHTTPResponse(status_code:int, chunks:Iterable[bytes])`。必须用给定端点/认证头，零重试、禁重定向；超时覆盖连接及完整响应读取，抛 TimeoutError；其他异常只映射固定码。附 HTTPJudgeTransport 基于仓库已有 httpx2（无新增依赖），分块读且响应2MiB保护，deadline 单调钟。假 transport 可返回多块、抛异常；judge 也独立计字节与检查耗时。生产评测每报告恰一次调用；未校准仍可取得观察，但不能通过。

## P6 输入与输出

`JudgeInput(spec:ScenarioSpec, answer:Answer, report:ReportCapture, delivered:Mapping[str,DeliveredRecord], authorized:Mapping[str,Window], outcome:IncidentOutcome)`，outcome 必填。DTO严格类型及未知字段检查。实际 body 只来自 PR-A `judge_request(..., outcome=...)` 的9键：question、subject_id、authorized_windows、report_raw、cited_evidence、expected_output、scenario_kind、injected（仅service/aliases/operation/aliases）、signature_matches。不发送 flag/variant、场景ID/路径、未引用/未交付/其他Run证据、recording或ledger。发送前递归拒绝私有协议键（reasoning_content、reasoning、thinking、redacted_thinking、encrypted_content）及凭据槽键（api_key及大小写/连字符变体、authorization、password/passwd、token/access_token/refresh_token、secret、credential/credentials、cookie、bearer；命名槽前后缀同样拒绝）；report_raw 内JSON也检查。私有字段注入返回 invalid，transport 调用为0。修改未引用证据不会改变请求body。

content 必须是严格 JSON 对象（无重复键/未知键/非有限数），恰 `schema, diagnoses, verdict`，schema=`opspilot-judge-1`，verdict 严格 bool；每条 diagnoses 恰 PR-A JudgedDiagnosis 的9字段：location、quote、factual_causal(bool)、service/operation(str|null)、cited_evidence_ids(str数组)、linked_claim(int|null，拒bool)、supported(bool)、reason(str)。不能由模型提供 calibrated/hash/status。

代码复用 PR-A定位/引文/claim引用检查、签名匹配、因果诊断 AND 与事实证据归属检查：正常不得有事实型因果诊断；故障必须有至少一条，每条服务/完整操作匹配、supported=true且引用匹配证据；未合格的事实证据判失败。返回 verdict 必须与 PR-A重算因果AND一致，否则 invalid。`JudgeObservation`：result（PR-A JudgeResult）、valid、recomputed_verdict、passed、error_code、response_model、usage、elapsed_seconds、provider、phase（evaluation/calibration）、balance_delta（本模块不查余额，None=unknown）；不留原响应/异常/私有推理。passed 仅有效P6、合格gate、已绑定校准且重算true。最终产品评测仍由 evaluate 再与 deterministic gate/trace做AND。error/timeout/invalid/oversize/未校准均 passed=false；HTTP/GLM或DeepSeek身份错误=error，OpenAI响应身份错误=invalid，非stop/JSON/schema/定位/引用/verdict错误=invalid。错误码固定且异常链隐藏；零修复、零重试。

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
CalibrationItem(item_id,scenario_id,split=`debug|validation`,source_family,input_hash,request:JudgeInput,coverage:tuple[str,...])；input_hash为实际P6白名单输入canonical hash。恰六场景，每场景2debug+4validation，ID唯一、输入hash唯一，source_family不跨集合。验证覆盖 hidden_normal_cause/summary_claim_mismatch/extra_wrong_cause/borrowed_citation/hypothesis/no_positive_diagnosis/insufficient_evidence。来源族由作者诚实记录，程序不能证明作者独立性。
AnnotationSet(annotator_identity,model_identity,labels:tuple[BlindLabel,...])；BlindLabel(item_id,input_hash,diagnoses:tuple[JudgedDiagnosis,...],verdict:bool)。恰两份不同标注者身份（模型身份可相同）、都覆盖36项，不得用三家候选judge盲标；记录身份不证明全新上下文。每标签定位/引文/引用与重算AND均复核。两份逐条标签不同（包括漏诊）时必须有 Adjudication(item_id,annotation_hashes:tuple[str,str],decision_record:str,label:BlindLabel)；绑定两份完整annotations文件hash，decision_record是非空人工裁决记录；一致项禁止额外裁决。最终至少12个validation负例。

freeze 在新目录独占写 items.json、annotations-1.json、annotations-2.json、adjudications.json、pack.json；均0600。pack.json包含schema=`opspilot-label-pack-1`与前四文件sha256，不包含自己hash；返回该索引canonical hash。loader重算四文件与输入hash、标签、分布与裁决；拒绝符号链接、未知schema/字段、hash不符。OUTPUT_EXISTS时不覆盖；写失败留下不完整件并fail-closed。工件只能放ignored私有目录，公开仓库只记录允许的hash/结果，不能提交实验答案。

run_calibration必须收到三家配置/transport，先核包并在destination/label-pack落盘完整标签冻结件，再调用；每家同一36项，debug每项一次、validation每项两次，全程串行。每次保存 JudgeObservation 与item/input/repetition身份；关键因果诊断按最终标签的(location,quote)核漏检。错误/无效/超时/oversize、漏诊不能作为负标签上的正确False：该供应商校准直接失败，另记录错误原因，计数由B1 calibrate重算后扣除错误/漏诊的虚假一致数；误通过只计有效输出的True。24验证、48比较、≥46一致、≥12负例、0误通过。结果保存为新目录 calibration.json（schema=`opspilot-judge-calibration-1`），含pack/config hashes、全部observations和重算counts、selection；loader从逐次`provider-NNN.json`与`label-pack/`逐项重建，不采信汇总observations/passed。

SelectionRecord(status=`selected|pending_user_decision|no_baseline`,selected_provider:str|null,eligible:tuple[str,...],pack_hash,results:Mapping[str,CalibrationResult])：GLM合格且agreement≥Sol即自动selected zai；否则有合格GLM/Sol则pending且selected=null；两者不合格no_baseline。DeepSeek永不eligible。待裁决记录只写候选与证据，不自动使用Sol；B3不提供伪造人工批准的快捷入口。VerifiedCalibration只能经load_verified_calibration从已选记录重建，绑定pack/config hash与合格结果；pending、对照、错误工件返回 CALIBRATION_UNRESOLVED。B1 load_calibration 保持原语义，不把旧纯bool计数件当B3认证。

## 对草案的可逆接口调整与理由

- JudgeInput使用PR-A完整原始DTO且outcome必填，理由：重复利用P6代码复核，杜绝宽松回退。
- JudgeObservation附JudgeResult/valid/复核值，理由：校准时区分调用失败和正确负例，兼容evaluate。
- OpenAI请求/响应身份固定为lead指定的gpt-6.1-sol，理由：任意型号不能满足D16；真实API可用性由第一次校准调用核实。
- 使用已有httpx2有界HTTP而非SDK，理由：asyncio总deadline覆盖连接与完整读取，零重试且不新增依赖。
- B3认证loader另名，不替换B1 load_calibration，理由：保持已测计数工件语义并补齐盲标认证边界。
- 增加七类coverage与逐诊断漏检，理由：落实P7覆盖要求，避免只比较最终bool。
- pending记录无人工选择写入口，理由：合同要求停在选择记录处，裁决由lead后续接入。

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
8. **标签与记录（歧义21、24–27）**：场景限定D1已有六例，source_family由独立作者记录；coverage恰允许第46行七名，验证集合合计全部覆盖，调试可为空、每项可多名，未知名拒绝。固定候选glm-5.3/deepseek-flash/gpt-6.1-sol（含Sol日期快照）在freeze/load即拒绝作annotator；run_calibration还检查传入configs身份，拒绝发生在任何transport前。已有destination在发送前报OUTPUT_EXISTS；不覆盖旧件。错误与漏诊均扣掉虚假一致数，有效误通过才增false_passes。loader允许JSON空白/键顺序变化，按语义重算，不能仅靠格式不同拒绝篡改。理由：提前堵住可在无configs时识别的候选标注者污染，补充严格读取规则。
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
