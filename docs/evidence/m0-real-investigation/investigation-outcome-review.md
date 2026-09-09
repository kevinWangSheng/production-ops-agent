# 真实调查结果独立核查

2026-09-09；审查者未参与 Holmes 实现或环境操作。仅只读核查本地原始业务输入/工具观察/最终结果、账本和独立工程证据；不读取 .env/approval/provider reasoning，不调用模型/trace，不重发目标查询或操作故障。样本是开发校准，不是盲测/保留集。

**最终调查判断：本轮未达到“正常与故障调查均完成”的预定目标，不能据此打开首片实施入口。** normal-03真实返回且主要数字可回查，健康措辞/短引用有限制；normal-01、normal-02、fault-01、fault-02均没有最终业务结论。故障真实存在且可诊断的独立事实，不等于Holmes已成功定位。全部失败保留。

Holmes最终16次HTTP、59工具、214739tokens；峰值cache-miss估算0.924021 CNY、16 CNY请求预留仍未核账。加本轮Flash4HTTP，整轮已达20HTTP，停止全部模型。最后一次新Run业务接续也因length/空正文未完成，不能把它当主动调查或诊断能力结论。不同窗口/限额/投影的5个主动开发尝试与1个独立报告Run不能当同条件准确率或可靠性统计。

## normal-01：调查未完成

原始工件位于环境 worktree `tmp/m0-environment/holmes-runs/normal-01/`。`result-business.json` 为 failed / InternalServerError、final_business_content=null、3次模型请求、11次工具查询。`holmes-request-ledger.json` 的3次HTTP均为200且有usage；因此异常类名不是供应商返回HTTP500的证据。没有最终结论可评审，正常调查完成判据未通过；原失败须保留在分母。

执行前固定窗口为 `[1788975149.661,1788975449.661]`，目标 m0-otel-20260909。输入明确无默认故障假设，列实际来源及日志覆盖缺口，没有提供正常答案或注入参数。正常输入和共享prompt可读；原上游含Kubernetes/TodoWrite默认叙述而本实验使用Compose/四工具，属于基线适配差异，不可据提示文字认证Kubernetes能力。

### 实际可见证据

- 11个观察均有evidence_id、工具/参数及observed_at。10个HTTP200，1个HTTP502 SOURCE_UNAVAILABLE；services只是配置名单。数据查询参数均使用固定窗口，成功数据包含integration/source/query/collected_at。
- e4/e11 payment交易 `increase(...[5m])=8.585129737662966`；e8 payment/前端代理错误increase均0，checkout正常span指标increase=112.80362977013127。独立环境 `normal-calls.json`/`normal-payments.json` 数值与之相同。increase是时间窗估算量，不等于精确整数请求次数。
- e2累计计数仍有payment错误5、前端代理错误6；e8窗口增量0说明这些累计值不能直接当本窗口错误。不能凭e2断言当前事故。
- e6真实查询返回7条trace、376个span。审查者逐条遍历：span start均在固定窗口内；process的opspilot.integration.id均为目标；所检查error标志、otel.status_code及非0rpc.grpc.status_code未见错误。这是返回样本，不代表所有请求或完整流量。
- 延迟线索存在：e9 checkout RPC histogram p95为10000ms；返回trace中checkout最长span12777.833ms，frontend最长15271.683ms，payment最长412.624ms。未冻结延迟基线/SLO且有宿主资源限制，不能强制“normal”样本全健康，也不能仅凭这些时长确定根因。
- e7 checkout日志0条，与已披露OTLP日志缺口一致；不能据无日志排除错误。e10 HTTP延迟指标空集同样属于未取得数据。
- e5宽泛RPC/HTTP指标rate表达式原返回502。后续已读取独立工程重放 `promql-error-reproduction.json`：源端422/execution，错误为vector cannot contain metrics with the same labelset；修复后的代理返回400 SOURCE_QUERY_REJECTED并保留source_status=422。故原失败是查询表达式执行错误被代理误分类，不是来源不可达证据；原502观察保留。

### 次数和费用

normal-01账本3次请求各保留1 CNY：prompt tokens合计14216，completion tokens6973，total21189；记录峰值cache-miss估算0.105405 CNY。实际账单未核对，3 CNY占用不能因低估算释放或增加请求额度。0 trace上传分支。异常后没有最终结果，已有工具观察仍保留。

## 正常环境与权限证据边界

环境 `normal-ready/capture.json`、`normal-identity.json`、部署注册和image-lock提供查询/运行身份来源；HTTP200本身不等于有数据或健康。checkout trace与payment指标具备当前integration及service/version标签，服务→实例→部署revision的完整冻结合同仍需主任务处理。

已读取 network-denials.json、network-direct-ip.json、redirect-denial.json：专属容器探针能访问proxy，直接backend/host/internet不可达，uid65534且无socket/home；实际302的目标请求计数0。该探针与宿主运行的Holmes进程不是同一执行边界，不能借此认证Holmes本身OS网络隔离。审查者没有重新操作网络探针。

## 后续待核查

等待有据修复后的新Run与实际故障调查结果；逐项对照真实工具可见证据、独立故障记录、window/identity/引用、失败和限制，并重算整轮HTTP/usage/费用占用。任何成功仅代表该具体开发样本；不自动打开SPEC实施门槛或改变feature passes。

## 有据复验准备：确定性 trace 投影

执行者为避免大工具正文占满下一轮请求，新增显式抽样投影。审查者直接提取当前 trace_projection 函数，以normal-01原始e6复算：7587 UTF-8 bytes；7条trace/376span，展示20/省略356；service计数总和376，原始observation hash和所有选中trace/span ID均匹配。说明明确错误标签优先、再最长耗时的抽样偏差，不能当总体错误率。受检源码摘要 `1b1d054cc987b8850dfd5f5b82b31d15fc41ef396530932ff4ef55a870940b6a`。

这是投影机制的本地复验，尚不证明normal-01的具体失败原因就是体积。新run才有安全请求包络/错误码诊断；不倒推缺失历史字段。完整原始观察单独保存，后续审查区分“模型收到的投影视图”和“评估者独立拥有的完整观察”。

## normal-02：局部投影后仍未完成

原始 `normal-02/result-business.json` 仍为failed/InternalServerError、final=null，2次模型HTTP、8工具；账本只记录2次实际HTTP且均200。安全包络记录3个待发包：13828、45118、83148 bytes；第三包超过固定65536，当前执行器会在发送前拒绝。结合账本可确认本轮第三次HTTP未发送；boundary_error_codes为空，异常包装没有保留具体固定码，但包络与守卫条件已给出可审计的本轮拒绝原因。不能用新诊断倒推normal-01。

8条工具均HTTP200，窗口和目标与normal-01相同；payment错误rate=0，payment交易rate=0.0286170991/s；checkout日志仍无数据。模型获得两份带hash/ID/省略说明的trace投影视图，以及指标名发现结果；可见证据累计和同Run必要协议上下文仍使后续请求超限。由于未产生最终结论，此案继续计为未完成，不能以工具有结果当调查完成。

本次prompt tokens12405、completion5537、total17942；峰值cache-miss估算0.087048 CNY，保留2 CNY。两正常尝试合计5次模型HTTP、19工具，估算0.192453 CNY；5 CNY请求预留仍占用，失败不退款。

环境随后重放normal-01原e9查询仍为10000ms；新窗口`[1788975699.9291909,1788975999.9291909]`同query为71.25ms（normal-latency-followup.json）。不同窗口允许状态变化，旧尾延迟事实保留；后一个窗口不证明旧窗口健康，也不定位变化原因。

## 有据调配后的边界复核

主合同在normal-02实证拒绝后、后续调用前记录128KiB输入及Holmes累计16HTTP/16CNY（含已用5），将原未分配4纳入同allocation；全轮Flash4+Holmes16仍不超过20HTTP，20CNY总额与deadline不变。每请求1CNY预留；2input tokens/byte工程上界假设下，(131072×2×3+8192×9)/1e6=0.86016 CNY，不是供应商账单或硬计费证明。

审查者独立抽取当前guarded_send、替换为无网络transport验证：131072 bytes允许、131073拒绝；累计第16允许、第17拒绝；输出8192不变。受检源码SHA-256 `bc5004d4599c921d372fa74277188d11cc4466d1c94a57506aa0190b5bafdd97`。旧失败与旧限额记录保留；后续normal-03采用新窗口，不能与前两案宣称完全同条件比较。

## normal-03：链路返回，结论有限支持

使用新窗`[1788975849.645199,1788976149.645199]`和128KiB限额；4次HTTP均200、14工具、finish_reason=stop。最大已发送请求99753bytes，在新上限内。不能与前两案声称同条件重试或计算泛化成功率。

逐项对照模型实际收到的`*-tool-model-view.json`、完整observations及最终正文：

- checkout PlaceOrder server p99=74.25ms（e4）、checkout outbound p99=23.2ms/code0（e9）；最长checkout span77.826ms、frontend约86.076ms、payment4.151ms均与视图一致。
- e5/e6实际上是相同5条trace、去重236spans，0个被列出状态标签判错的span；payment10 spans。它们不是两批独立样本，不能相加为472。投影标明偏差，原正文也承认trace样本不能证明总体错误率。
- payment STATUS_CODE_ERROR rate=0、checkout有正常span流量且没有非0错误series；唯一非0错误rate为recommendation0.004159/s，与正文另列观察一致。未进一步查recommendation，不能推广为整个Demo无异常。
- payment累计交易30=USD29+CAD1，服务版本2.0.2及integration_id匹配；正文正确声明无法证明30均在5分钟窗内。checkout约0.15 spans/s相乘约45是span增量估算，不是45个唯一订单/calls。
- checkout/payment日志0、payment RPC直方图缺失均被正确列为覆盖缺口，没有凭空制造数据。

**质量限制：** “no evidence of degradation”在该窗口已采集证据范围内有支持；开头/结尾无保留“healthy”及“healthy latency envelope”强于现有未冻结SLO/覆盖证据。“unbiased”span metrics覆盖也未由本次原始记录证明。应保留实际原文，将这一过强概括记为评审不足；不能把它转为系统健康/独立恢复认证。报告引用仅`(e2)`等短号，人工可在本Run映射到`normal-03-e2`，但与工具完整evidence_id不相等，当前未提供确定性解析器，因此不能声明已达到可点击/机器可解析引用合同。

本案prompt41494、completion10720、total52214，峰值cache-miss估算0.220962CNY；4CNY预留仍占用。三次正常尝试合计9HTTP、33工具、91345tokens，估算0.413415CNY、9CNY请求预留；一案返回、两案失败，保留全部而不将有不同输入窗口/限额的探索样本当可靠性统计。

## fault-01：模型执行前独立故障事实

固定窗`[1788976626.533447,1788976926.533447]`，300秒。审查者读取工程合同、fault-01-observation.json及其中7个原始文件，逐一重算SHA-256均匹配manifest。未执行故障或查询。

- 7个不同trace均有payment Charge错误，并沿同trace对应checkout Charge grpc状态2、PlaceOrder状态13；已满足至少两次实际checkout失败的预定条件。checkout查询和payment查询返回同7条trace，不能视为14条独立样本。
- payment成功交易increase=0；checkout→PaymentService/Charge状态2增量约7.501625、状态0增量0；payment错误span增量约7.501438、checkout错误span增量约15.002876。正常旧窗的相应错误增量为0，变化具有方向性证据，非精确请求整数计数。
- frontend-proxy总日志131，返回最新20，其中1条POST /api/checkout HTTP500，traceId=`a75dd411a2cb31aa11cceb558ce9a651`与上述错误trace匹配。日志样本只证明这条可关联失败，至少两次失败依据来自不同trace而非假造多条日志。
- 调查可见input只描述HTTP500症状、精确integration/窗口/字段映射；不含payment故障结论、工程注入参数或答案。可见来源足以支持“定位payment/Charge错误及其影响checkout”的预定有限目标，不要求模型猜隐藏flag名称。

以上支持该故障案的有限可诊断前提，尚不是模型已定位成功；最终应只用模型实际拿到的工具投影视图检查其主张，再与工程独立事实对照。

## fault-01实际调查：证据已取得，最终回答未完成

模型3次实际HTTP均200、14工具均HTTP200；第四个候选请求131799bytes超过131072，上限在发送前生效。最终failed/InternalServerError、正文null、boundary_error_codes为空，不能宣称故障定位已完成。故障可诊断与执行器未交付结论是两件事实。

实际工具查询start/end被模型截为1788976626/1788976926，均比输入固定小数窗早0.533447秒；不是精确同窗。独立核对返回仍为上述同7条错误trace，故本次实际发现没有因此变成另一组故障，但不能将这一巧合当作窗口权限保证。首片应由Controller绑定精确窗口或冻结可接受的明确容差，不让模型抄写数字承担 enforcement。

模型已获得包含错误服务/方法/状态、log与指标的业务视图；e3/e4使用sum(rate(histogram_bucket))且省略le，会累加累计桶，不能直接作为真实RPC请求速率。没有最终答案，无法检查它是否错误解读；保留查询质量问题作为开发校准输入，独立故障判据使用工程 `_count` 增量与实际trace。

本案prompt33459、completion5690、total39149，峰值cache-miss估算0.151587CNY，3CNY预留。Holmes四次尝试累计12模型HTTP、47工具，估算0.565002CNY，12CNY已请求预留；16HTTP allocation尚余4，原失败保留且不得擅自增加次数。

## 最后fault-02前的日志投影复验

主任务授权只在原剩余4HTTP内做最后复验，不再增加128KiB/8192上限或全轮20次。执行者将重复resource/scope提升到身份表，保留原始观察及hash。审查者用fault-01原e8/e9/e13独立复算：12957/8484/877bytes，20/20/0条，0省略；逐条完整body、resource/scope、事件timestamp、trace/span/document ID均与原数据一致，hash正确。受检源码 `aaed9ac3ae501a2271c60bd52c5beb5f2b88611c633d8d0a808dbaa08508ead8`。此次验证只支持投影保持所核查可见事实，不保证下一轮模型必然按时/按大小完成。

## fault-02最终结果与停止边界

独立读取最后result、包络、12份model-visible视图/原观察及累计账本：3次实际HTTP均200，12工具均200；第四候选158029bytes超过131072，发送前拒绝，最终failed/InternalServerError、正文null。此案沿用精确小数故障窗，没有fault-01的截秒差异。

已取得的可见证据仍包括目标/version匹配的payment交易增量0、跨服务错误span指标、同故障trace、日志500及明确的无日志结果。最后两个工具结果也保存在视图工件中，但第四次模型请求未发送，不能把这些“工具已生成视图”一律称为模型已消费；前面正常/故障失败案亦应按实际成功发送请求的消息进度区分。模型是否能据最终一组证据形成准确结论未被本实验完成验证。

本案prompt39913、completion9523、total49436，估算0.205446CNY，3CNY请求预留。Holmes最终累计prompt141487/completion38443/total179930，15实际HTTP、59工具、0.770448CNY估算、15CNY请求预留。次数/字节边界实际拒绝超限；未知账单不被估算替代。执行者已报告停止模型调用，保留剩余1次不用；审查者没有追加模型请求。

**下一步建议（未授权新付费实验）：** 先用这些实际业务观察做离线有界上下文/工具返回设计验证，精确保留证据身份、配对、可见性与私有协议续传边界，明确“即将超限时完成有限结论或handoff”的外部行为。不能反复增大上限把未收敛上下文问题当已解决；也不能删去私有协议或已提交证据来迁就预算。完成必要机制证据与冻结合同后再讨论对应新实验/首片入口，不把当前结果抹成成功。

## 执行前调整：独立fault-handoff-01

主合同与账本在执行前记录改变“余1不用”的工程安排，使用用户原20HTTP/20CNY中最后1HTTP/1CNY作新Run业务证据报告；不是增加预算/次数。原两次主动故障调查0/2最终结论不变，原停止决定保留为历史。

审查者快速独立核对输入12份fault-02业务工具视图：逐项JSON值等于原model-view文件，所有source_files SHA正确，完整evidence_id保留。输入71493bytes，SHA `13a778424fdfd9011d5ee20ed8bbb087220244867e5bcf1f219298d0be866f32`；只增加目标/窗口/实际HTTP500症状及报告指令，不含工程注入答案或旧provider消息/reasoning。包括先前未成功发送给旧Run的末批持久业务结果，新Run可以消费，但不是恢复旧私有协议。

当前执行器 `--max-steps=1` 使用空工具集构建prompt/schema/ToolExecutor，并硬拒绝localhost查询。独立AST+假transport检查：第1模型请求允许、第2拒绝，新工具GET拒绝；累计账本15→16，0实际网络。源码SHA `d24ae3d6649088172d812b1c9193c9696407a446f1d7bffc978de5db3b869bc2`；128KiB/8192/原deadline/trace禁用保持。

本补实验只能核查持久业务证据能否支持一次报告生成；无主动取证、无同条件对照、无跨进程provider私有状态恢复证明。完成后全轮最多20HTTP并严格停止模型；等待本新Run最终结果后追加独立主张核验。

## fault-handoff-01最终：length终止，没有诊断正文

独立核对result、请求包络及累计账本：新Run仅1个90513bytes请求，低于131072上限；HTTP200、显式deepseek-v4-flash、thinking/high、8192输出参数、0工具查询。finish_reason=length，final_business_content为空字符串，状态incomplete。该新Run没有最终诊断可逐项评分，不能宣称“证据汇总已成功定位”，也不能把一次输出受限结果推为模型没有诊断能力。未读取、保存或评判provider reasoning。

本次prompt26618、completion8191、total34809，估算0.153573CNY，1CNY请求预留。最终Holmes16HTTP、prompt168105/completion46634/total214739，估算0.924021CNY、16CNY请求预留；加Flash4HTTP达到原全轮20上限。此前“余1不用”决定及本次执行前变更均保留，执行者已报告停止模型；本审查没有任何新模型或trace调用。

原两次主动fault仍0/2最终结论；这个报告新Run也未完成。首片实施入口仍缺对应恢复/控制/权限/合同冻结及可完成的真实故障调查链路证据；结果不能被工具数据或工程答案替代。

## 还原后的独立工程观察

已只读核对recovery-01-observation.json所引用7个raw SHA，以及额外recovery-checkout-logs.json的raw SHA、故障时间线hash。恢复窗`[1788977596.026837,1788977896.026837]`为300秒；当前flag文件逐字等于保存的原始快照，restore事件18:12:46Z早于观察窗。

审查者独立遍历恢复原trace：8条trace的已检查状态标签未见错误；8条POST /api/checkout日志全部HTTP200，其traceId集合与这8条trace完全相等。窗口全部已存在error series增量0、Charge成功增量约8.750036/状态2增量0、付款交易增量约8.749563。支持“本次工程还原后，在该5分钟、有实际交易的观测窗未再观察到原故障”的有界结果；不是模型认证恢复，也不是F6的Kubernetes HealthProfile、完整信号/长期稳定或72h证明。环境停止与数据保全工件由环境任务记录收尾，不能由本报告的模型结果代替。
