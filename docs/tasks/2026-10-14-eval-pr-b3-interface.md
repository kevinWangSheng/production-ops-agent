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

Z.ai 固定 `https://api.z.ai/api/paas/v4/chat/completions`、请求/响应 `glm-5.3`。DeepSeek 固定 `https://api.deepseek.com/chat/completions`、请求/响应 `deepseek-flash`，只供校准对照。OpenAI 固定 `https://api.openai.com/v1/chat/completions`；必须显式给 OPENAI_JUDGE_MODEL 与 OPENAI_JUDGE_RESPONSE_MODEL，标识 D16 的 Sol，缺失拒绝；本任务禁止网络，尚未核实 Sol 的官方 API 名，记录配置不等于验证其可用性。三家各有 ZaiAdapter/OpenAIAdapter/DeepSeekAdapter，build(config,input) 返回 bytes；不复用调查客户端。固定两条新消息（system规则、user白名单JSON），stream=false，Z.ai/DeepSeek 用 max_tokens，OpenAI 用 max_completion_tokens；无历史、工具或旧评分。响应需 model 精确匹配、单 choices、index=0、assistant、finish_reason=stop、无 tool_calls；只消费 content，私有响应协议不保留。usage 只记录非负整数 prompt/completion/total_tokens，缺值 unknown（None）。

`JudgeTransport.send(*, endpoint:str, headers:Mapping[str,str], body:bytes, timeout_seconds:float, max_response_bytes:int) -> JudgeHTTPResponse(status_code:int, chunks:Iterable[bytes])`。必须用给定端点/认证头，零重试、禁重定向；超时覆盖连接及完整响应读取，抛 TimeoutError；其他异常只映射固定码。附 HTTPJudgeTransport 基于仓库已有 httpx2（无新增依赖），分块读且响应2MiB保护，deadline 单调钟。假 transport 可返回多块、抛异常；judge 也独立计字节与检查耗时。生产评测每报告恰一次调用；未校准仍可取得观察，但不能通过。

## P6 输入与输出

`JudgeInput(spec:ScenarioSpec, answer:Answer, report:ReportCapture, delivered:Mapping[str,DeliveredRecord], authorized:Mapping[str,Window], outcome:IncidentOutcome)`，outcome 必填。DTO严格类型及未知字段检查。实际 body 只来自 PR-A `judge_request(..., outcome=...)` 的9键：question、subject_id、authorized_windows、report_raw、cited_evidence、expected_output、scenario_kind、injected（仅service/aliases/operation/aliases）、signature_matches。不发送 flag/variant、场景ID/路径、未引用/未交付/其他Run证据、recording或ledger。发送前递归拒绝私有协议键（reasoning_content、reasoning、thinking、redacted_thinking、encrypted_content）及凭据槽键（api_key及大小写/连字符变体、authorization、password/passwd、token/access_token/refresh_token、secret、credential/credentials、cookie、bearer；命名槽前后缀同样拒绝）；report_raw 内JSON也检查。私有字段注入返回 invalid，transport 调用为0。修改未引用证据不会改变请求body。

content 必须是严格 JSON 对象（无重复键/未知键/非有限数），恰 `schema, diagnoses, verdict`，schema=`opspilot-judge-1`，verdict 严格 bool；每条 diagnoses 恰 PR-A JudgedDiagnosis 的9字段：location、quote、factual_causal(bool)、service/operation(str|null)、cited_evidence_ids(str数组)、linked_claim(int|null，拒bool)、supported(bool)、reason(str)。不能由模型提供 calibrated/hash/status。

代码复用 PR-A定位/引文/claim引用检查、签名匹配、因果诊断 AND 与事实证据归属检查：正常不得有事实型因果诊断；故障必须有至少一条，每条服务/完整操作匹配、supported=true且引用匹配证据；未合格的事实证据判失败。返回 verdict 必须与 PR-A重算因果AND一致，否则 invalid。`JudgeObservation`：result（PR-A JudgeResult）、valid、recomputed_verdict、passed、error_code、response_model、usage、elapsed_seconds、provider、phase（evaluation/calibration）、balance_delta（本模块不查余额，None=unknown）；不留原响应/异常/私有推理。passed 仅有效P6、合格gate、已绑定校准且重算true。最终产品评测仍由 evaluate 再与 deterministic gate/trace做AND。error/timeout/invalid/oversize/未校准均 passed=false；HTTP/身份错误=error，非stop/JSON/schema/定位/引用/verdict错误=invalid。错误码固定且异常链隐藏；零修复、零重试。

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

run_calibration必须收到三家配置/transport，先核包并在destination/label-pack落盘完整标签冻结件，再调用；每家同一36项，debug每项一次、validation每项两次，全程串行。每次保存 JudgeObservation 与item/input/repetition身份；关键因果诊断按最终标签的(location,quote)核漏检。错误/无效/超时/oversize、漏诊不能作为负标签上的正确False：该供应商校准直接失败，另记录错误原因，计数由B1 calibrate重算后扣除错误/漏诊的虚假一致数；误通过只计有效输出的True。24验证、48比较、≥46一致、≥12负例、0误通过。结果保存为新目录 calibration.json（schema=`opspilot-judge-calibration-1`），含pack/config hashes、全部observations和重算counts、selection；loader逐项重建，不采信passed。

SelectionRecord(status=`selected|pending_user_decision|no_baseline`,selected_provider:str|null,eligible:tuple[str,...],pack_hash,results:Mapping[str,CalibrationResult])：GLM合格且agreement≥Sol即自动selected zai；否则有合格GLM/Sol则pending且selected=null；两者不合格no_baseline。DeepSeek永不eligible。待裁决记录只写候选与证据，不自动使用Sol；B3不提供伪造人工批准的快捷入口。VerifiedCalibration只能经load_verified_calibration从已选记录重建，绑定pack/config hash与合格结果；pending、对照、错误工件返回 CALIBRATION_UNRESOLVED。B1 load_calibration 保持原语义，不把旧纯bool计数件当B3认证。

## 对草案的可逆接口调整与理由

- JudgeInput使用PR-A完整原始DTO且outcome必填，理由：重复利用P6代码复核，杜绝宽松回退。
- JudgeObservation附JudgeResult/valid/复核值，理由：校准时区分调用失败和正确负例，兼容evaluate。
- OpenAI请求/响应身份显式必填，理由：离线约束下无法核查Sol API名称，缺失fail-closed。
- 使用已有httpx2有界HTTP而非SDK，理由：asyncio总deadline覆盖连接与完整读取，零重试且不新增依赖。
- B3认证loader另名，不替换B1 load_calibration，理由：保持已测计数工件语义并补齐盲标认证边界。
- 增加七类coverage与逐诊断漏检，理由：落实P7覆盖要求，避免只比较最终bool。
- pending记录无人工选择写入口，理由：合同要求停在选择记录处，裁决由lead后续接入。

- 余额差显式unknown并保存provider/phase，理由：本边界不自动增加余额查询，真实对账由驱动提供证据。
- run_calibration发送前自动落盘标签包副本，理由：即使调用者仅给内存DTO，也保证先冻结标签再读judge输出。
