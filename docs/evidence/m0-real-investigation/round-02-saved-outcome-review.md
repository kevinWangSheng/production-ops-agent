# M0-02 saved report 独立结果核验

结论：**未产出报告，报告收束判据未通过**。这次独立新 Run `m002-saved-report-01` 只有一次模型 HTTP 尝试、零工具查询；不能计为主动故障调查成功，也不是旧 `fault-02` 的恢复。旧主动调查结果不变。

## 评审身份与范围

本评审由未参与实现或调优的独立 Agent 执行，依据当前 AGENTS、SPEC、C3 第12节与 `round-02-contract.md`。仅检查指定业务工件和旧12份 `*-tool-model-view.json`，未控制环境、调用模型、改代码、读取 `.env`、任何 `private-protocol` 文件或 provider reasoning。共享宿主上的角色分离不是 OS 隔离或盲测证据。本次没有独立工程故障事实检查；也没有用任何工程注入答案补写模型结论。

## 实际结果与交付证据

- `result-business.json`：`status=failed`，`final_business_content=null`，`error_type=InternalServerError`，`boundary_errors=[RuntimeError]`，`boundary_error_codes=[]`，`model_http_requests=1`，`tool_queries=0`，`tool_wall_seconds=0.0`。
- `delivered-business.json`：一条请求业务快照，`request_ordinal=1`、`http_status=200`，但 `state=attempt_outcome_unknown`。HTTP200不构成完整报告成功证据；供应商消费、响应处理具体失败原因仅凭这些业务字段无法确定。
- `configuration.json`：`deepseek-v4-flash`、thinking enabled/high、32768输出预留、360秒请求/1800秒Run限制、max_steps=1、tool_schema空、trace disabled、compaction disabled。此处只核对实际配置记录，不认证限制机制实现或计费。
- 对交付快照中的12个业务view分别做JSON结构等值比较，全部与对应旧业务view一致；对12份源文件逐字节重算SHA256，全部匹配业务输入中的 `source_files` 清单。它们可证明确有这12份视图进入可信运行器标记的实际尝试业务快照，但该记录的 outcome 仍为 unknown，不能扩大成供应商全部消费确认。

## 报告质量判据

由于正文为null，事实引用、数值准确性、因果置信度、反证、未知/缺测/截断、建议权限均为**无报告可评分**，不是这些判据通过。没有 `finish_reason` 或其他公开业务证据证明本次是length失败，故不将此前length结论套到本次。当前可观察分类是“调查/报告未完成”，不是“完成评估但因果不确定”。

旧12份证据本身不能代替模型最终报告。本评审不据此补写根因、订单影响、健康或恢复结论。后续若有非空报告，仍须逐条检查完整 `fault-02-eN` 引用、实际窗口、span与订单区分、缺测/采样限制，以及无SLO/HealthProfile不能认证healthy或恢复的边界。

## 实际检查方法及工件固定

采用Python `json.loads` 解析明确允许的文件，`hashlib.sha256(path.read_bytes())` 重算源字节哈希，比较每个 `business_tool_views` 对象与其原文件对象；没有递归打开其他运行文件。核验时间：2026-09-10T02:21:22.683688+00:00。

工件根目录：`/Users/shenghuikevin/dev/AI/production-ops-agent-m0-environment/`。以下路径相对此目录。

- `tmp/m0-environment/holmes-runs/m002-saved-report-01/configuration.json`，1049 bytes，SHA256 `03c735527bdd70f2f22410dbabe5fdd0db27f62fe7cb5f45a7e808e311a66534`。
- `tmp/m0-environment/holmes-runs/m002-saved-report-01/input-business.json`，157453 bytes，SHA256 `5d5d90c76c63acdcd3ce2b49adef933b3a6aa4d9cf1cd6851b75db5cba340f59`。
- `tmp/m0-environment/holmes-runs/m002-saved-report-01/delivered-business.json`，151690 bytes，SHA256 `8cdbf65e3ae5be7b52531781cc055aa16eeeea0d64da9e16505b44f7b745a612`。
- `tmp/m0-environment/holmes-runs/m002-saved-report-01/result-business.json`，288 bytes，SHA256 `5aa4fb8c4c64d952fcedaaaf0ba6935db5cd8384cba83ae4f3456e7c7443ee74`。

12份已核对的源view（哈希匹配且与实际尝试业务快照JSON等值）：

- `tmp/m0-environment/holmes-runs/fault-02/fault-02-e1-tool-model-view.json`：`5f89d75a7fcfd5fcea6b1fd2947bee91fc253da4c30d3494333c40b258f3c1ab`。
- `tmp/m0-environment/holmes-runs/fault-02/fault-02-e10-tool-model-view.json`：`3fb26998d84d0f77beb14aead57f98e098b9de7b3c4f331eaa6e12b629afd3e7`。
- `tmp/m0-environment/holmes-runs/fault-02/fault-02-e11-tool-model-view.json`：`2e4a0964ea1b043d9c3afddb3479ae04df980cf340c719ea0d414b2d22c1026d`。
- `tmp/m0-environment/holmes-runs/fault-02/fault-02-e12-tool-model-view.json`：`6295559c52869f4acaf84751159d0d6550c980333595542d9de154f389832600`。
- `tmp/m0-environment/holmes-runs/fault-02/fault-02-e2-tool-model-view.json`：`bc2400e0547e79de0041f08badc7a8d474a1f1701b8385435b5f6b7dc1226970`。
- `tmp/m0-environment/holmes-runs/fault-02/fault-02-e3-tool-model-view.json`：`70a7d69f98ab2a31d1d279db2b573fd597153572705eaa251cc246583ae8b987`。
- `tmp/m0-environment/holmes-runs/fault-02/fault-02-e4-tool-model-view.json`：`e68512142feb25822b76abaf1bce095ee08a1f7d706b2f951c3bacfd2b9c1443`。
- `tmp/m0-environment/holmes-runs/fault-02/fault-02-e5-tool-model-view.json`：`7cc542fcd2782a91c1bb1b5dc5d499a8259e57ad57f02644e811675c2a9b6f50`。
- `tmp/m0-environment/holmes-runs/fault-02/fault-02-e6-tool-model-view.json`：`53c4d0bcbc672d0998f9ec1356ff69ee3a544f3b90b924c9e931eeb5ad18d438`。
- `tmp/m0-environment/holmes-runs/fault-02/fault-02-e7-tool-model-view.json`：`5d0d1927b7288b0bde8567cc0ae803b7d3e473dd905296c7425c5f09c8f9ff23`。
- `tmp/m0-environment/holmes-runs/fault-02/fault-02-e8-tool-model-view.json`：`8b051bf003d582e97ba7352eb0f105c77392b222409d21b6c5e3005636fb0e2c`。
- `tmp/m0-environment/holmes-runs/fault-02/fault-02-e9-tool-model-view.json`：`e0b7b2022477cf602658714b8ac2a8df57c00a3442f1b4ef34d2cfdb0fbe0723`。

## 未知与后续边界

错误更深原因、实际费用、provider完整消费状态、任何重试或新实验是否成功均未在本评审中认证。允许执行者保全失败并做有界诊断；不因本报告自动新增重试或支出授权。若修复后生成了新的业务结果，须保留此次失败身份与原工件，按新结果重新独立评审。SPEC门槛与产品passes不得据本次打开。


## 第二次独立新Run：m002-saved-report-02

**结果分开判断：安全业务正文已生成，但运行身份被拒绝；正文质量也存在需修正的实质发现，不能标为合同通过。** 本节保留前次失败，不反推首请求实际返回model。

### 原始响应与交付

`response-2-business.json` 记录HTTP200、`response_model=deepseek-flash`、`identity_accepted=false`、`quality_assessment=not_accepted`、`finish_reason=stop`。正文11650字符、按空白分词1544词；不是空正文或length失败。usage为prompt35721、completion15104、total50825（相加一致），cache hit2048/miss33673（相加等于prompt）；completion不是可见报告专属token计数。

`result-business.json` 仍为failed、final_business_content=null，边界错误明确 `response model identity mismatch`，1模型HTTP、0工具。`delivered-business.json` 单条ordinal2（不能因此算本Run两个HTTP）、`state=response_rejected`、HTTP200。配置请求仍为deepseek-v4-flash/high；是否deepseek-flash为合法等价返回别名，不在本评审中认证，也不豁免身份门槛。

第二次交付业务输入与第一次逐字段比较，仅run_id不同；12份view逐份结构相等，12源文件hash再核均匹配。以下正文检查均使用实际尝试的业务快照与对应保存view，未读取private目录或原始私有响应。

### 有支持的正文内容

- e5/e11的7 traces、205 spans、185 omitted、各服务计数与正文facts 2列出的28/28、14/14、7/7、14/64、7/14一致；两查询trace ID集合及服务计数相同。它们不是两套独立样本。
- e6确有20/131条新近日志，其中1条18:01:58.653893Z的checkout500，与正文所列trace/span ID相符；其他19条为200/308。e7/e8/e12是本查询0 hits，不能证明不存在应用日志。
- e9的PlaceOrder status0各桶0、status13 +Inf约7.501625；e10的Charge status0各桶0、status2 +Inf约7.501625、le5约1.098371、le10约3.750813、le25约7.501625均匹配。GetCart/GetQuote/Convert/GetProduct的+Inf值与ShipOrder/EmptyCart零增量也匹配。
- e3 payment 2.0.2 transaction counter increase为0；e2各主要span数及约22%、4%、6%、50%算术比例与正文相符，但这些比例只适用于所查span序列。e1有17个服务，且未列flagd/otelcol-contrib。
- 正文保留payment内部原因未知、无flag配置证据、采样有偏、缺SLO、不认证恢复、只作人工查询建议；近因“Charge错误与PlaceOrder/HTTP500相关”有指标支持。将其进一步写成确定逐请求传播链则超出投影视图证明能力，见下。

### 独立发现

1. **P2，服务归属错误。** §4.7声称recommendation有21 spans、0 error spans并引用 `fault-02-e11`。该view没有recommendation服务；21/0属于quote。e2只有recommendation ERROR约1.250/UNSET约45.009，不能用quote的trace证据排除recommendation。应改为此样本未提供recommendation拓扑，相关性未确定。
2. **P2，累计桶被当成调用数。** §4.3将e4的1985.501称为logs Export calls，据此推测空日志是pipeline artefact。e4查询对所有le累计桶求和，既非1985次调用，也不能定位frontend/checkout/payment日志来源。正文§4.6自身已正确指出e4不可作count view，但没有贯彻。只能说存在collector日志Export桶增量；缺失服务日志原因仍未知。
3. **P2，逐请求完整因果链过度确定。** facts2称“Full call chain per failed request”，facts1称parent HTTP spans；e5/e11投影没有parent引用，sampled_spans未显示任何payment span，只在聚合表有payment计数；仅4个trace ID有保留span。服务聚合和同trace只能支持相关性及受限近因假设，无法证明7个请求全部沿同一完整父子链或支付返回码传播方式。facts5的flow aborted及H1应明确是结合业务序列的推断。
4. **P2，无依据恢复原始失败数。** §4.4以自行选择的共同系数倒推“≈6 failure events”，H1又写6–7.5 failures。已交付increase结果为外推值，未提供原始scrape/counter、reset信息或统一比例合同，且le5的1.098371本身不能套统一系数。标为hypothesis仍不构成可审计影响估计。保留指标增量与7 returned traces，原始请求数unknown。
5. **P2，健康/覆盖与趋势措辞越界。** facts7“no latency growth”没有基线支持；facts9“inventory intact”没有预期清单，且§4.8承认清单不完整；facts3“Zero successful checkouts”、facts6“no completed payment transactions”、§4.1“100% PlaceOrder failure”需限定为当前可见指标序列及其窗口，不能直接认证真实业务全量。§6.5“a real zero”也不能排除已存在但未正确更新的计数器。没有升级为healthy/恢复认证，但上述局部确定性仍超过已验证覆盖。
6. **P2，事实的完整引用与身份不足。**scope/status部分使用e3/e6/e8/e7/e12/e9/e4短引用，违反每个事实完整evidence_id合同；开头完整范围列举不能替代具体事实引用。e6空cluster_name的缺口有披露，但其他来源integration_id及run target不能独立证明该log的cluster身份，应留unknown。§4.2“Node-style service”没有交付的语言/runtime证据；§4.9“every view confined”忽略e1 inventory没有查询窗。
7. **P3，描述与算术精度。** §4.5称trace ID sets“byte-identical”，实际仅集合相同、数组顺序不同。facts8的“Ratios are pipeline-invariant”没有pipeline不变性证据，应删除，仅报本次算术值。

本评审没有更改模型原文。以上有支持的内容不能抵消实质错误，也不代表模型身份符合批准profile。建议先保留失败与正文，修正文档/报告合同问题并按适用授权处理身份兼容，不据此自动发送第三请求或开始主动实验。saved仍不是主动故障调查成功。

### 第二次工件hash

核验时间：2026-09-10T02:28:52.701854+00:00。以下根目录与前节相同；12源view hash未改变并已复验。

- `tmp/m0-environment/holmes-runs/m002-saved-report-02/configuration.json`：SHA256 `03c735527bdd70f2f22410dbabe5fdd0db27f62fe7cb5f45a7e808e311a66534`。
- `tmp/m0-environment/holmes-runs/m002-saved-report-02/input-business.json`：SHA256 `02091a53263843b6f28941a33cfb6e184dbf40f0df85e24fc79eeb12c809d633`。
- `tmp/m0-environment/holmes-runs/m002-saved-report-02/delivered-business.json`：SHA256 `a258e67b7d0e1c98c8763cd097829c636cdf79454c3634c861f830b763a490f4`。
- `tmp/m0-environment/holmes-runs/m002-saved-report-02/result-business.json`：SHA256 `cbcaf470bc42fce2591ff053ed3b51329776d636ea8633276f193773ef9487bb`。
- `tmp/m0-environment/holmes-runs/m002-saved-report-02/response-2-business.json`：SHA256 `753eb21a6352f4b582e16cbc94b5666de2067a587c47b6cbf9d81ab014eeadd3`。
