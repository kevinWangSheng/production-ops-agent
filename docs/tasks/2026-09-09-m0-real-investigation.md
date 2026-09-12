# M0：从协议链路进入真实调查

创建：2026-09-09；当前收尾：2026-09-10 M0-02付费执行已结束、资源已停止保全，报告质量尚未通过，M1入口仍not cleared。

当前结果见[round-02-results.md](../evidence/m0-real-investigation/round-02-results.md)、[独立入口审查](../evidence/m0-real-investigation/round-02-final-delivery-gate-review.md)及[当前v4冻结首片包](../testing/first-investigation-v4-2026-09-10.md)。20模型HTTP/0trace，真实PG组合与有限核心调查结果有据，事实/可见范围P2仍未闭环；PR最新CI/review收尾下文继续更新。

## 历史：2026-09-09上一轮与PR #15过程

以下旧日期、旧分支、旧预算和进行中措辞保留为历史，不授权重跑；本轮接续从后面的2026-09-10小节开始。

## 工作区与合同

主工作项复用production-ops-agent-m0-01 / chore/m0-real-investigation，干净旧分支快进已合并main738b5c7。独立环境production-ops-agent-m0-environment / chore/m0-real-environment从同基线建立；环境Agent唯一负责OTel实例，HolmesAgent仅负责基线配置/运行，避免重复搭建。

[本轮绝对截止、授权、预算和完成条件](../evidence/m0-real-investigation/contract.md)。新增20 CNY/20模型/5trace，截止2026-09-10T17:14:30Z；旧Pro+首次Flash4 CNY未核账保留。原数据库在原worktree，系统PG不动。

现场GitHub：PR14 mergedAt=2026-09-09T17:05:55Z、merge738b5c7d637ded49919d6368fbc3980b1b9c5856；main CI34380825700成功，本地主仓库main/origin/main一致且干净。原专属PG停机后经所属脚本重新启动；历史归属和22条等旧实验状态按实际全表行hash保留。未清理任一旧worktree。

## Flash真实链路

[原始安全结果](../evidence/m0-real-investigation/flash-results.json)保留两次新实验：

1. 当前请求诊断：2请求、3.453秒，最终69字符为Markdown json围栏，内部target/evidence_id严格匹配，整体json.loads失败。业务failed/LIVE_FINAL_JSON_INVALID；0上传。只能定位这次，不倒推旧轮丢失正文。
2. 最小修复后：仅最终请求加入response_format=json_object，其他请求/fixture/成功校验不变。2请求、10.792秒，最终57字符严格JSON；PG业务completed及诊断、outbox可回读；1上传/2回读后TRACE_VERIFIED。

官方JSON模式依据：https://api-docs.deepseek.com/guides/json_mode/ 。[诊断1](../evidence/m0-real-investigation/final-diagnostic-1.json)、[复验2](../evidence/m0-real-investigation/final-diagnostic-2.json)。诊断包装器只记录结构和已确认等于已知fixture的最终业务文本，不保存reasoning或未知正文；其[原始脚本](../evidence/m0-real-investigation/diagnostic-harness.py.txt)和私有运行目录保留，源码digest另列。Python/依赖沿用锁定CPython3.12.13、OpenAI3.10.0/HTTPX2 2.12.0/LangSmith0.12.2；真实请求直接HTTPX2，未宣称OpenAI SDK已真实运行。

回归测试捕获第二次请求JSON mode，同时返回真实围栏文本仍严格拒绝且不上传。有效修前红例见[输出](../evidence/m0-real-investigation/json-mode-red-valid.txt)；前两次测试搭建错误保存在私有目录，不算有效回归证据。修后75项定向通过、make check265 passed/17默认PG skip；[完整输出](../evidence/m0-real-investigation/check.txt)。独立源码/PG/原始证据审查待收尾。

本轮Flash合计4模型、1上传，输入1973/输出205 tokens；峰值cache-miss估算0.007764 CNY，当前空闲时段估算0.003882 CNY。实际账单未核；4 CNY新子额度继续占用，加旧4 CNY共8 CNY未核账。Holmes初始分配12 CNY/12模型/1trace，累计16 CNY，彼时剩余4 CNY/4模型；该段为Flash收尾时历史预算，当前分配见下文。

## 后续与完成条件

环境正常遥测/只读权限→正常与故障上游实际调查→独立核对→冻结首个纵向流程验收/环境与恢复前提；条件不足列具体缺项，不改功能passes。

收尾需整合环境/基线证据及预算，形成入口决定和下一实施任务，更新ROADMAP/M0当前状态，完成独立审查、PR最新CI及已触发review；不自动合并。停止本轮服务，所有数据库/证据保留。

## 首片机制与任务候选

[控制探针合同](../evidence/m0-real-investigation/control-probe-contract.md)、[原始PG结果](../evidence/m0-real-investigation/control-probe.json)：正常提交1次；cancel/epoch/lease及取消后新generation共4次迟到采纳被拒绝；真实PG pid变化、4份快照与旧live业务/诊断哈希保留。0模型/trace。仅最终业务快照和SQL条件证据，owner/Run字段、逐步骤重建、取消后禁止新调用尚缺；不报完整恢复成功。

[首个完整纵向任务候选](../plans/first-vertical-investigation-2026-09-09.md)已明确提交→查询→展示→跟进/取消→持久保存及外部验收。独立审查指出动态可见证据、最终人控代次和Compose身份与旧v2静态/Kubernetes合同存在接缝，入口冻结前须解决；不向已有v2数据填假UID、不提前给调查者最终证据。当前保持SPEC gate未开。

PR #15初次CI checks失败（m0-postgres成功）：公开诊断原始脚本以.py存档，被Ruff作为维护源码检查而报格式问题。按既有原始源码证据习惯改为.py.txt保留逐字内容/hash，不修改运行脚本或放宽检查。修后make check265 passed/17默认PGskip，失败与修复输出均保留；后续PR最新CI另核对。

## 正常调查失败与有据复验（进行中）

normal-01实际3模型/11工具，无最终结论（SDK InternalServerError，服务端3次均HTTP200，不能称provider500）。已确认上游在无shell结果目录时把大trace清空为ERROR；完整保存不等于模型可见。normal-02增加有界trace摘录/完整证据引用后，实际2模型/8工具；第3待发包83148bytes>65536，发送前被拒，具体包络证据已取得，旧失败不倒推同因。

按峰值价、128KiB输入按每byte最多2token估算，加8192输出，每请求0.86016CNY<1CNY预留。当前子额度明确追加原剩余4CNY/4模型：Holmes总16CNY/16实际HTTP（含已有5），Flash4CNY/4HTTP，整轮总20CNY/20HTTP不变，trace仍最多5。原旧4CNY占用不计入本轮新20上限、继续保留；不复用旧批准。normal-03使用新稳定窗口复验，不能将不同窗口伪称同条件比较；故障尚未执行。

normal-03已返回：4实际模型/14工具，stop，新稳定窗口checkout p99=74.25ms、5条去重trace/236 spans且可见error0；日志/payment RPC histogram缺口正确披露。独立核查支持“此窗采集证据未显示checkout/payment错误或明显异常”，不支持无条件healthy；约45为span增量估算而非唯一订单，e5/e6同5条trace不能算独立样本，短引用e2仍缺产品可解析ID适配。正常实际尝试3次，前2失败不从分母删除。本轮目前模型13/20、trace1/5；真实故障已在专属环境注入，等待完整窗口与调查。

fault-01工程前提经独立核验成立：7个raw SHA匹配，7个不同trace各见payment Charge错误向checkout传播，支付成功增量0、checkout Charge grpc2增加7.50、frontend-proxy日志checkout500及traceID一致。调查者只收到症状/接入/窗口，不收到注入答案。实际模型3HTTP/14工具后第4包131799>131072（多727bytes）在发送前被拒，无最终结论；不能将独立故障事实当模型定位成功。

最后有据复验：基于实际frontend-proxy25.5KB/cart20.5KB日志中重复metadata，保留完整body/身份/时间/trace-span-ID的确定性视图与原raw/hash，128KiB/8192上限不变；fault-02最多4模型，全轮此前16实际模型、最终最多20，之后停止付费调用。实际工具将小数窗口截为整数早0.533447秒，偏差保留，未来由可信Controller绑定窗口而非让模型复写；本轮不假称精确窗口权限通过。

fault-02实际3HTTP/12工具后，第4候选158029>131072被拒；两次主动fault均无最终结论。Holmes累计15HTTP、59工具，input141487/output38443，峰值cache-miss估算0.770448CNY，实际未核。已停止多轮重跑。

收尾安排随后调整并已告知用户、执行前更新合同和账本：最后剩余1HTTP/1CNY用于新Run fault-handoff-01，仅继承fault02已持久业务views，验证是否可形成有据报告；无新工具/trace、不继承私有reasoning、不看工程答案。原fault0/2不改，新Run不作为同条件上游循环或真实私有协议恢复成功。原“余1不用”的审查/过程记录是该调整前历史。环境还原/观察不等待这一次只依赖已存证据的调用。

## 本轮付费执行最终状态

fault-handoff-01最后1HTTP200、90513bytes在上限内、0新工具；finish_reason=length、8191输出tokens、最终正文为空，接续报告未完成。它没有证明模型故障诊断成功或没有能力；已明确取得输入历史体积与输出预算两个实测阻塞。所有模型请求到20上限后停止，未追加。

[最终用量与费用](../evidence/m0-real-investigation/final-usage.json)：20模型/1trace、59真实工具查询；input170078/output46839、总216917tokens。按本轮空闲时段与已知cache（Flash按miss保守）估算0.3834861CNY；全miss空闲估算0.4658925、峰值上界估算0.931785。实际账户余额减少0.39CNY，是聚合显示而非逐Run账单；模型/trace可归属实际费用仍unknown。新20CNY+旧4CNY共24CNY未核账预留，**不是实际花费24CNY**。各Run计时以首模型请求至末响应定义，详见工件，不冒充完整进程wall/告警SLA。

专属PG已于18:19:44Z停止，55431关闭、数据及全部历史保留，系统PG4391仍运行：[收尾](../evidence/m0-real-investigation/postgres-cleanup.json)。环境者另已还原故障、独立采还原后5min，26容器/卷/profile保留并停止，具体完成工件由环境任务维护。

## PR #15 审查发现处置

f79e84f两项CI34389374955成功；Code Review与Security Review均已返回，前者指出P2：freeze_images覆盖已提交image-lock，后者无新增可见发现。按实际发现改为将旧lock/config hash作为只读输入，完整收集/比对后才允许写runtime；缺档/漂移/平台或配置差异先拒绝。原96份归档及4份运行文件字节不变，9项真实CLI临时树回归红→绿及独立复验通过，服务/模型未重启。修复来自环境dbd2ac7/5df00df，独立覆盖见closeout-review.md；原实际实验的源码/失败/费用记录不改写。推送后核对覆盖本次变更的CI/Code/Security复审，未据旧head通过宣布新head完成。

同一保全缺陷检查中又通过真实入口临时树复现export_traces.py在新checkout覆盖旧Jaeger manifest；增加查询/创建输出目录前的旧manifest拒绝，最终用独占创建防覆盖。两条回归红1失败→绿2通过；原归档字节未改、0网络/模型，独立局部复验另记closeout-review。不是追加真实实验或导出，也不修改先前2792trace结果。

1efa66c复审Code Review再指出P2：四个生成配置hash未覆盖全部bind输入；Security Review已完成无新增可见发现。775e51e按原已锁SHA的OTel tar及既有生成文件hash核查全部9bind，目录集合/字节/软链接/未知路径拒绝在写入前完成；未建立事后新基线、未追溯认证原时点。26CLI回归与独立复验、实际当前9bind只读核对通过，原archive/runtime不变。连同同类trace-manifest保护一起推送最终复审；模型仍20/20，服务停止。

6d1b736的Code复审补充P2：observe_window只拒绝ignored raw目录，在干净checkout仍会覆盖已提交summary。按相同原则增加query/mkdir前的旧summary拒绝与最终独占创建；真实runpy临时树回归红1fail→绿，并一并核对本组所有docs/evidence输出：capture已有tracked目录独占、freeze只读、export已保护，observe这次补齐。仅本地保全修复、0模型/网络，不改原调查/还原事实。

## 2026-09-10 M0-02 本轮接续

新授权与绝对时间见 [round-02-authorization.json](../evidence/m0-real-investigation/round-02-authorization.json)。先故障保存证据新Run报告实验，再真实正常/故障主动Holmes；随后冻结首流程动态合同和最小PG步骤恢复/取消。旧4+20 CNY未核账占用保留，不算实花，也不占用新的20 CNY授权；新账本不得覆盖旧账本。实际付费前需补齐相互一致的子额度、参数、通过与停止条件。

现场核对：PR #15已于2026-09-10T01:22:37Z合并，merge e9d22e9183601ae322f006f4a42d9dd2bbcbae84；交付bc46370两项CI成功，最新Code Review 19:35:24Z无major issues、Security Review已返回。main CI34425221461成功，主工作区从738b5c7快进同步e9d22e9。两个旧实验worktree无未提交改动，原地创建chore/m0-02-convergence和chore/m0-02-environment并快进同起点；tmp数据库/业务证据/归档保留，没有重建、清理或搬运。Colima两个profile实际Stopped。

唯一环境负责人root；report_calibration只读核对真实源码/失败与官方参数，contract_recovery_plan只读准备首流程合同/恢复设计，preflight_independent全新上下文核查旧证据/账本。各自不启停环境、不付费；共享文件系统不算隔离。重大方案实施前及最终关键结果另做独立审查。

活动时间：01:49Z开始现场核对；约01:53Z起上述三个只读工作与父调度并行。下载/安装0，新增模型/trace0。后续阶段在此追加起止与原始证据。

02:01Z起报告wrapper与v3/最小PG模块并行实施；[实施前独立审查](../evidence/m0-real-investigation/round-02-plan-review.md)允许本地实施，真实付费前要求完成硬停止、账本未知占用、实际交付与取消竞态验证。02:09:59Z原专属PG按所属脚本启动，server data_directory再次匹配；原7表53行安全hash快照见[PG起点](../evidence/m0-real-investigation/round-02-pg-before.json)。允许机制实现者仅添加隔离随机实验，不重建/删除原库。OTel仍停止。准备好m002-saved-report-01的新Run业务输入12 views及hash，尚未发送。

02:19–02:27Z两次saved有界请求：首HTTP200后响应身份拒绝且返回值未保留，unknown3.44064；补拒绝前诊断保全后第二stop15104输出tokens、有正文，但reported model=deepseek-flash被旧strict equality拒绝。独立质量发现6P2，模型原文及失败保留。02:28Z单次官方/models只读metadata确认当前列表deepseek-flash与deepseek-v4-pro；独立兼容审查允许请求v4flash/返回双Flash名合同，权重不保证。第二usage峰值上界0.243099追加核算而非账单。后续直接在真实active验证通用报告约束/来源view修复，report phase不再重打。

## 本轮真实组合与故障报告进展

normal02实际形成JSON报告，15份最后交付raw/view/hash/完整refs可核对，但独立发现累计counter当5分钟增量及backend20/view19误概括；质量未过，不把格式成功算质量通过。之后按真实缺陷补metric时间语义、logs可见/后台计数及3步Run同样的权限/终止检查，48项定向及实际Holmes离线3/4步独立复验通过。旧7fd732完整源码已按SHA精确恢复、只读保全；每次后续真实Run前另存全部源码快照，不只hash。

真实PG首请求400强制tool_choice不兼容已保留；新Run5822fb34-c343-4085-aca5-6337ff2ad40d采用官方thinking适用的省略参数，同一Run两stage PID39801→44306、epoch1→2，完整响应/工具/同provider私有协议从PG重建后实际续传成功。独立只用DB端布尔/安全业务核验，2HTTP/0.003051CNY峰值上界（非账单）；原7表53行hash全不变。它是正常进程退出后的实际组合，精确中途崩溃/取消证据另由真实PG+可控替身提供，不混称真实故障恢复。

唯一新故障在专属环境注入并观察1789010952..1789011252，独立7raw哈希/多个真实trace关联/增量/日志核查成立。fault Run m002-fault-01实际4HTTP/19工具，http17输出JSON，独立确认近因payment Charge(code2)向checkout(code13)再到HTTP500的有限定位有据。仍保留3组P2：cart跨trace误关联和样本泛化、实际14/349写20/349、把投影省略误作原始遥测缺失，不能记完整报告质量通过。工程于03:42Z按原字节还原，继续5分钟正常观察供最后3HTTP复验；不由模型认证恢复。

本轮累计17模型HTTP（含1次400），剩余3用于normal03；trace0。当前子额度report4/2HTTP、normal6/11、fault5.5/4、pg4/3、trace0.5/5且禁用，总20CNY/20HTTP不变。两项unknown各3.44064及旧24CNY保留。最终SPEC入口需在normal03与整体独立审查后决定，目前仍not cleared。

## 本地检查与最终入口结论

最新整合make check：370 passed/31显式PG默认skip；新增PG机制/driver定向22 passed，bridge/v3含旧v2回归98 passed。第一次整合检查仅被Markdown代码块格式拒绝，格式修复后通过，原输出保留。离线view49tests及独立UTF8/身份/字节上限/旧版本重放通过；新依赖snapshot桥接18tests与独立恶意路径/缺依赖/hash探针通过。

[初次本地入口审查（历史）](../evidence/m0-real-investigation/round-02-entry-review.md)已以新上下文亲自核验源码、实际PG安全元数据、fault/normal03真实raw/delivery及冻结包；无新增实现阻断发现，但实际报告P2未经最新候选复验仍阻M1。OS隔离明确为运行边界限制，不额外要求首片编码前搭完整平台。20HTTP已尽，0trace；root已停止专属PG和26容器/VM，系统PG4391仍在。所有历史数据和依赖worktree保留。

环境代码/证据先本地提交bc573cd、22b4d8f，再按范围cherry-pick到主任务0f20ed2、da4d464。当前只做最终提交/推送/PR和最新远端CI及Code/Security审查；不合并。

PR前fresh-checkout检查另复现PG probe在import时依赖本机外部worktree导致收集失败：隔离subprocess阻断外部lab来源，原版1 failed；改为从本仓库package导入相同round02配置（live数据目录仍原地），9项offline通过。六份实际PG执行源码已在修改前逐字节SHA核验并保全，真实PG两stage仍对应历史版本，不声称新import版本已新增live复验；未改预算、数据或模型逻辑。

## PR #16 审查闭环（进行中）

[PR #16](https://github.com/kevinWangSheng/production-ops-agent/pull/16)初次head58e10b1，两项CI34439757538成功。Code Review3975793064发现new_run递增generation但v3控制事件不能表示，合法新Run Outcome被误拒；[修复记录](../evidence/m0-real-investigation/round-02-pr16-review-fixes.md)与[独立复验](../evidence/m0-real-investigation/round-02-pr16-control-review.md)覆盖直接new_run/cancel+new_run/correct+new_run和旧结果拒绝，104tests通过。为必要跨层验证仅临时启动原PG，随后再次停止；无模型/trace新增。此段为修复完成时快照：Security待返回，修复当时尚未推送。

05:40Z修复1462e2d已推送PR16；make check 374 passed/34默认PG跳过、秘密扫描通过。首轮Security请求超过30分钟仍无运行确认/结果，补发一次同head后仍未知；因此推进已修复版本，同时保留所有旧/新请求的审查未完成状态。最新Code/Security请求5613751787/5613752083，等待CI与审查结果；原P1已回复具体修复及独立104项回归，不将pending当作通过。

05:46Z最新Code Review返回3975937306/3975937313：completed空事实结论缺少唯一匹配committed报告交付要求，及Run子限额错误按experiment计数。统筹指派统一复现修复，保留experiment总授权/unknown/绝对期限约束；05:48Z临时重启原PG并验证身份，仅用于随机隔离回归。Security各请求仍无确认/结果，未据此结束审查。

第二组P1实现者修复见[报告绑定与Run限额](../evidence/m0-real-investigation/round-02-pr16-report-binding-run-limit-fixes.md)：先复现14项报告绑定失败与新Run REQUEST_LIMIT，再修复；152项组合回归通过。root完整make check为398 passed/35 PG默认skip（13.93s，原输出tmp/m002-pr16-second-full-check.txt），独立复验与真实旧证据桥接重放正在进行。该组不修改实际模型账本/质量FAIL，未新增真实模型或trace。

第二组[独立复验](../evidence/m0-real-investigation/round-02-pr16-second-review.md)152 passed/8.86s、5项源码hash一致；原fault01/normal03使用原22a96投影重放仍19/19与12/12结构通过，原质量FAIL保留。root于复验后再次停止专属PG，见[停止记录](../evidence/m0-real-investigation/round-02-pr16-second-pg-stop.json)。修复按两个逻辑提交与状态记录一起批量推送后，继续核对最新CI和已请求审查；Security首次请求至此仍无确认/结果。

06:11Z da02563 Code Review返回3976067577/3976067579/3976067585：Outcome缺完整summary/next_steps、事实target缺失可绕来源绑定、freshness缺可信可检查策略。统筹以新上下文启动[整组合同设计审查](../evidence/m0-real-investigation/round-02-pr16-full-report-design-review.md)，另复现反证/排除假设的状态/目标检查遗漏和最后cancel/correct与completed不一致。实现前统一审查最小兼容方案；保留旧schema/原报告，历史缺字段标unknown/fail，不填模型未返回的target，不改变质量FAIL。仍0新增真实模型/trace，PG/Colima保持停止；Security首次请求至此超过一小时无确认/结果，不能宣称PR就绪。

严格v4/report-v2完成[联合独立终审](../evidence/m0-real-investigation/round-02-pr16-v4-final-review.md)：62项定向测试、9组自写反例、3份schema与DTO一致，原fault/normal03四份本地回放全文/hash逐字保真；strict均明确不通过/unknown，显式legacy只保留历史结构结果。runtime另经[独立替身审查](../evidence/m0-real-environment/round-02-strict-runtime-review.md)验证目录/时钟/权限及530KB目录发起前拒绝。root make check425 passed/35 PG默认skip；原PG模块未改，不重启服务。完整回放按既有数据出口规则保留tmp/m002-v4-replays（0600），Git仅元数据摘要，接口未删全文。当前v4包据此冻结，旧v2/v3/report-v1保留，仍无新版真实模型或质量通过证据；新上下文最终入口审查及PR最新远端闭环继续。

全新上下文[最终入口审查](../evidence/m0-real-investigation/round-02-final-entry-v4-review.md)亲核31份manifest/raw/view hash、两次真实最终delivery及报告/legacy保真，并核当前四源码hash。有限核心定位成立，但实际事实错误仍可复现；v4只有离线/替身证据，M1仍not cleared。当前入口导航已指向该最新判断，前一entry-review文件保留历史。下一步完成本组提交、最新CI及适用Code/Security审查；不自动合并。

联合离线候选[源码清单](../evidence/m0-real-environment/round-02-pr16-v4-offline-source-manifest.json)保全12个必要Python源、5份schema及6项依赖/审查引用；root逐项验证23项当前文件及对应snapshot hash一致。原快照未覆盖，明确不是实际Run。完整回放仍ignored、Git只含安全摘要；旧预算/PG/产品passes未改。本组待批量推送后的最新CI与Code/Security结果仍按PR确认，不能用离线终审替代。

## PR16 发送许可后续修复

aa485f9的CI checks/m0-postgres均SUCCESS（run34449729273）；07:30Z Code Review3976615675指出未领取send_grant仍可提交响应。独立真实PG复现并发现prepare两事务窗口；[修复](../evidence/m0-real-investigation/round-02-pr16-send-grant-fix.md)把grant与dispatch/预留原子提交，采纳要求已有grant已领取，保留直接路径合同。[独立复验](../evidence/m0-real-investigation/round-02-send-grant-review.md)24项PG及6个自写实验通过，历史真实两请求只读查到claimed/response/execution相容；这不是新真实模型验证。adapter标识同步pg-private-pipe-v3-atomic-send-grant，原code/profile hash门槛本已存在，10项离线检查及actual execute到claim替身验证版本传递。旧无grant歧义记录不回写，不授权静默续跑。

root检查425 passed/41 PG默认skip（13.92s）；审查文档Python示例格式失败原输出与原文已保留，格式前后AST相同。随后专属PG已再次停止，见[停止记录](../evidence/m0-real-investigation/round-02-send-grant-pg-stop.json)；模型/trace新增0，原账本、v4报告源、旧schema/工件不变。此项待推送最新CI/Code/Security复审；此前六项发现已有修复/独立证据，不因安全审查长期无确认就视为通过。

## PR16 初始证据与当前结果后续修复

6eab0a4的CI checks/m0-postgres成功（run34451776556），08:02Z Code Review3976875093/5099/5104指出控制后current final未清、report-only初始证据桥接缺失、versions未含Holmes/tool合同。当前final修复已经[独立PG复验](../evidence/m0-real-investigation/round-02-final-pointer-review.md)：26项与两组独立历史保留/newRun检查通过；仅清当前指针，历史报告不变。root已[停止专属PG](../evidence/m0-real-investigation/round-02-final-pointer-pg-stop.json)，真实模型/trace新增0。

[初始证据有界设计](../evidence/m0-real-investigation/round-02-initial-evidence-design-review.md)已独立通过：可信manifest核原raw/view/原投影Context/Timing，只作新Run副本；单兼容context才绑定，缺材料/坏ID/混合context保全真实input/report并经strict返回unknown，不能view冒raw或伪造新鲜度。实际user消息视图进入审计，导入不算新query；版本绑定实际upstream_commit和tool_schema，缺失为明确unknown。两作者正并行完成runtime/bridge；旧v4只读schema/source快照保留，当前canonical候选的兼容审计字段扩展须另记hash并终审。

本组[联合独立终审](../evidence/m0-real-investigation/round-02-initial-evidence-final-review.md)已完成：94项定向测试、固定Holmes单步假传输→strict、带事实报告缺manifest/坏hash/重复/mixed保真unknown、版本差异/缺失、协议非法及原始字节保真反例通过。root make check448 passed/43 PG默认skip（14.72s）。控制当前final的26项PG另已完成并停库。新[离线源码清单](../evidence/m0-real-environment/round-02-initial-evidence-offline-source-manifest.json)覆盖13源/5schema及依赖审查引用；旧manifest/source快照不覆盖。当前v4包已补记兼容初始审计字段、单context限制、完整输入/报告保真、实际Holmes/tool版本绑定；仍无新增真实模型/trace或M1入口放行。待本批最新CI/Code/Security，先前缺口不以旧提交审查冒覆盖。

全新上下文[最终交付核查](../evidence/m0-real-investigation/round-02-final-delivery-gate-review.md)已独立读两份完整真实报告、关键实际送模消息/raw/view及26项当前源码/依赖hash，并复核20HTTP/已知400404tokens/unknown占用；当前披露无新增P1/P2，但真实报告事实错误仍在，M1不得开放，Security未闭环。此前入口review保留各自版本/检查范围。最新本组提交之后继续等CI和已触发的Code/Security，不自动合并。

## PR16 初始发送前权限与CLI交接补正

ced7fd4的CI checks/m0-postgres成功（run34459556690）；09:21Z Code Review3977531749/3977531762指出初始导入缺current接口/时间前置校验、无report的CLI handoff会解引用None。runtime/bridge作者与独立审查者正在补实际wrapper发送前0请求反例和真实CLI保真输出；目标/时间必须在模型数据出口前执行，不能仅靠事后checker。仍无新增真实模型/trace/后端/PG操作，旧原文和schema快照保留。

本组[独立复验](../evidence/m0-real-investigation/round-02-initial-scope-cli-review.md)通过：真实wrapper缩窗/禁接口0复制/0dotenv/0model/tool、原scope宽但query窄的合法正例、实际CLI无报告三组及58组合回归/6自写边界通过。bridge只使用已验证query_window，不再从scope补值；reportless原文/input/violation完整保留。root make check456 passed/43 PG默认skip（14.35s）；全程0真实模型/trace/后端/PG。旧候选manifest/审查仍保留原覆盖范围，新source manifest将记录本组三源修复。待本批最新CI/Code/Security。

## PR16 普通报告失败的统一交接

e5357a8的CI checks/m0-postgres成功（run34461762937）；09:45Z Code Review3977733293指出无initial的普通malformed/empty/length报告仍抛FINAL_RESPONSE_REQUIRED。本组删除初始专用旁路，统一先重建input/所有Artifact/Action/Delivery，再形成report=None的严格handoff，保留实际执行状态、原文/hash（None与真实空串区分），不放宽合法报告解析。实际3×3 CLI矩阵红9→绿9，root make check467 passed/43 PG默认skip（15.12s）；独立矩阵、None/早期unknown delivery以及184项回归已通过，终审记录为[报告失败交接](../evidence/m0-real-investigation/round-02-report-failure-handoff-review.md)。旧schema/source快照仍保留，当前仅原始内容载体兼容空字符串，空报告本身仍拒绝。无新增真实模型/trace/后端/PG。

Security在GitHub仍无确认/结果；有界只读Codex Cloud list查询按官方JSON键核实tasks为空，仅表明该CLI列表未提供审查入口，不能推断服务健康或审查通过。查询自产日志仅保留tmp/m002-codex-cloud-error.log（0600）与hash说明，无内容导出、无平台配置更改、新任务或新付费执行。

## PR16 输入来源必需性补正

10:00Z GitHub首次明确确认96e89e3的Security Review正在运行，取代此前“无确认”的当前状态；尚未取得结果。10:09Z Code Review3977946891指出严格入口缺失/空input-provenance时跳过校验，原始question也可缺失却标合同一致。独立审查者已复现三种缺失组合错误通过，现统一补bridge保真unverified与直接checker的原始输入必需性；不改旧schema，不重放付费请求。96e89e3的两项CI成功（34463673924），PR仍待修复及最新审查闭环。

10:16Z Security对96e89e3明确完成且无安全发现；该结果只覆盖该提交。输入来源修复作者205项通过，root完整make check为488 passed/43 PG默认skip（17.26s；tmp/m002-provenance-final-check.txt），ruff/format与离线锁文件核查通过。正在等待本组独立复验，之后提交新head取得对应复审；原真实报告qualityFAIL和20请求用尽均不变。

本组[独立终验](../evidence/m0-real-investigation/round-02-input-provenance-review.md)完成：18项实际CLI矩阵、6项自写null/非法UTF8/双缺失反例、direct checker及输入导出拒绝均通过，完整Outcome/Artifacts/Actions/Deliveries逐对象保留、stdout仅metadata；205项组合回归亲跑通过，最终两源hash一致，无本组未处置P1/P2。新源码快照将记录该离线候选，等待最新远端复审。

实现与独立证据提交d66db41；[本组源码清单](../evidence/m0-real-environment/round-02-input-provenance-offline-source-manifest.json)记录13源/5schema/12审查依赖，旧快照不覆盖。清单Git reference是生成时历史参考，实际source hash与d66db41工作文件一致，不能将旧base字段误作当前实现版本。

## PR16 无报告交接的实际输入交付核验

f923897的CI checks/m0-postgres成功（34465716757），新Code请求5617086189已确认接收；10:31:22Z返回3978118760：report=None提前返回跳过ACTUAL_INITIAL_INPUT_NOT_DELIVERED，合法hash但实际请求漏/替换输入仍可合同一致。统一核对提前返回后的公共约束并补准备/发送/提交状态及无请求交接矩阵；Security新请求5617086835尚未确认，旧96e89e3的无发现不覆盖新实质变更。无新增模型/trace/环境操作。

作者沿同一路径统一前置actual input与已有capture的身份/hash/唯一交付/响应时间检查；两种投影×准备/发送/提交×缺失/替换/合法矩阵及相邻capture回归通过。root make check516 passed/43 PG默认skip（17.03s，tmp/m002-independent-binding-final-check.txt）；仅验收代码/测试，等待独立终验，不修改旧质量结果。

[本组独立终验](../evidence/m0-real-investigation/round-02-report-independent-binding-review.md)通过：31项reportless定向、12项多交付前错后对反例、9项实际CLI状态/输入矩阵和233项组合回归。核对通用identity/hash/context/control/已知clock/capture约束均在提前返回前，后部仅报告资格条件；完整packet保留，v4最终hash955cdc0b一致，无本组P1/P2。待本批最新CI/Code/Security。

本组代码与独立审查提交90e251c；[离线来源清单](../evidence/m0-real-environment/round-02-report-independent-offline-source-manifest.json)保留新v4源码955cdc0b，13源/5schema/13审查引用逐项核对。旧源码/清单及真实失败不改写。

## 同批审查清单补查与两项补正

完整分页评论与GraphQL reviewThreads（hasNextPage=false）复核确认，f923897在10:31:22Z同批实际有三条发现。root先前只处置了3978118760，漏将3978118746（raw缺采集时钟却允许bundle补值）和3978118777（CLI完整审计输出按umask创建）纳入清单；这是审查汇总遗漏，不能把它们说成新发现或已闭环。78f7af0已推送且CI成功（34467375083），现并行补这两条并独立联合复验，保留该批原意见及处理记录，不修改审查平台。最新Code请求5617339224已确认，Security5617339609尚未确认。

两项作者修复冻结：bridge4b25735c原子独占0600且不覆盖已有/符号链接，7项实际CLI与82桥接回归通过；initial_evidence f3309cd4逐字段raw时钟匹配及unknown来源边界，17定向回归通过。联合独立复验与root完整检查进行中，无模型/trace/环境操作。

[时钟与私有输出联合独立终验](../evidence/m0-real-investigation/round-02-import-clock-output-mode-review.md)通过：18项自写raw/manifest时钟组合、7项实际CLI权限/已有路径保护、122项组合回归。root make check529 passed/43 PG默认skip（18.00s；tmp/m002-clock-permission-final-check.txt）。源码确认首次创建即0600、finally关闭fd，raw时钟双向匹配不补造/擦除；最终helper/bridge hash一致，无本组P1/P2。后续核对完整远端线程及最新head审查，不以本地通过代替。

## PR16 新Run版本与控制状态边界

78f7af0的完整分页复审返回3978285916/3978285917：new_run空versions未经accept式检查可被claim，及最后new_run仍允许旧cancelled/waiting_human。统筹按同组共享版本验证/人控状态约束补正，新上下文new_run_boundary_review独立核验。root已仅重启并验证原专属PG身份，供随机隔离回归；OTel/Colima保持停止，其他PG不操作，0新增模型/trace。前一批时钟/私有输出修复分别提交0e44dc7/f0e7f03，来源清单保留该时点候选，不冒覆盖后续new_run变更。

本组作者27项真实PG/180项离线回归通过，root make check562 passed/44 PG默认skip（17.70s；tmp/m002-new-run-final-check.txt）。版本验证共享于accept/new_run/claim并在事务前执行；非法新Run不改状态/预算/输入/审计。新上下文独立验证尚在进行，专属PG暂为该验证保持运行，验证后由root停库。

[新上下文独立复验](../evidence/m0-real-investigation/round-02-new-run-boundary-review.md)通过：先现场复现空版本new_run/claim和两旧状态错误，再以8项真实PG定向/70项离线验证拒绝零持久变化、合法迁移/旧fence拒绝、预算保留、不兼容blocked及final清除。源码hash与冻结一致，无范围内未处置P1/P2。root随后验证身份并停止专属PG，见[停库记录](../evidence/m0-real-investigation/round-02-new-run-pg-stop.json)；数据保留，其他PG不操作。无新增模型或trace，M1仍not cleared。

实现与独立证据提交f1df631；[新Run候选源码清单](../evidence/m0-real-environment/round-02-new-run-offline-source-manifest.json)逐项验证14源/5schema/20依赖引用，明确上一时钟/权限候选为历史。root工作文件与本清单source hash全部相符，旧快照不覆盖。

本批提交前扫描出现一次SCANNER_SELFTEST_FAILED（尚未报告仓库泄密）；保留失败且不改扫描器/规则。一次有界诊断复跑原check函数，只增加阶段编号/计数输出：五项自检计数1/0/0/1/1符合预期，index/worktree/history均0，SECRET_SCAN_PASSED。首次数值未记录，具体自检失败原因仍未知，不倒推成泄密或已修复扫描器缺陷。

## PR16 完整user输入与报告状态一致性

65d3624两项CI成功（34469634597），完整分页复审3978469715/3978469724指出额外user消息被相等过滤忽略、completed assessment配failed/blocked仍可过。全新上下文delivered_input_state_review在本机复现额外user及四组状态组合误过，并于实施前批准有界方案：可信运行记录final_phase，固定报告指令hash纳入版本，完整user序列按actual input/本次context/可选final instruction核验，旧缺元数据保持unknown。

状态范围按SPEC70与既有v3:354限定：completed assessment必须有completed可信执行；不从模型状态反写execution。既有completed有界执行返回incomplete/inconclusive/gaps/handoff保持可表达，不把它称为调查完成。两作者按独立方案实施，0新增模型/trace/环境操作；Security新请求5617707259仍无运行确认/结果，旧通过不冒覆盖。

作者完整user合同/阶段字段/指令指纹与单向状态一致性已冻结，278组合回归、固定Holmes假pipe及初始拒绝正反例通过；当前Scenario schema兼容新增final_phase，旧schema快照保留，Outcome schema不变。root完整检查与新上下文独立终审正在收尾，0真实模型/trace/后端操作。

[本组全新上下文独立终验](../evidence/m0-real-investigation/round-02-delivered-input-state-review.md)通过：137项定向、独立CLI注入/状态/reportless矩阵、实际prepare_wire及4步固定Holmes假传输、两份schema与DTO一致性均通过；源码hash稳定，历史快照不改，本组无P1/P2。root make check582 passed/44 PG默认skip（17.86s；tmp/m002-user-state-final-check.txt）。完整assistant/tool私有协议保留，新增元数据不改变真实消息；0真实HTTP/trace/环境启动。

本组实现与独立证据提交a100c90；[完整输入候选源码清单](../evidence/m0-real-environment/round-02-delivered-input-state-offline-source-manifest.json)验证14源/5schema/26引用。清单Git字段是生成时历史参考，工作文件实际hash与a100c90对应；旧source/schema快照不覆盖。

## 本轮交接状态（2026-09-10T11:33:52.655660+00:00）

本轮本地修复、自测和适用独立验证已完成；最新实现a100c90、证据提交56b17f7。56b17f7两项CI成功（34471557918），但GitHub于11:30Z明确返回[Code Review额度已用尽](https://github.com/kevinWangSheng/production-ops-agent/pull/16#issuecomment-5617957215)，当前代码远端复审不可用。Security最新请求5617955725无确认/结果；96e89e3的旧无发现不覆盖当前实质变更。**PR16未就绪、未合并**，当前两条发现已修复并独立验证，保留待远端复审状态；不扩大额度、不另建审查平台。详见[机器可读状态](../evidence/m0-real-investigation/round-02-pr-closeout-status.json)。

收尾现场核实主工作区clean main e9d22e9，PR16仍OPEN且mergedAt=null；Colima default/m0-otel均Stopped，专属6端口关闭、PG无postmaster.pid，数据库、26容器/卷/VM、账本、业务证据和所有旧worktree保留。没有合并、部署、删除或额外付费。当前代码make check582 passed/44 PG默认skip，最新PG完整27项及独立8项另已实际运行；新版仅离线/替身/PG机制证明。

20实际模型HTTP、已知18请求400404tokens、0trace；已知保守费用上界1.438848CNY、新未知占用6.88128CNY、旧24CNY未核账占用均保留，实际账单未知。真实正常/故障最终报告有核心结论但完整事实质量仍FAIL；SPEC/M1门槛不打开，feature passes不变。下一步先恢复适用远端审查并核当前代码；产品前置项仍M0-03事实/实际可见证据一致性真实复验，须独立新实验授权。此交接不把PR阻塞或M0实验结束写成产品功能完成。

本次收尾剩余工作为文档状态提交及其CI核对；运行/费用统计截止前述实际模型账本，不因审查等待重置。

## 用户恢复后的现场重试（2026-09-10 11:42 UTC）

用户要求继续后，核实任务worktree干净、PR16仍OPEN未合并、f642020的两项CI成功（34472031328）。重新请求Code5618109992与Security5618110448；GitHub于11:42:39Z再次明确[Code Review额度已达上限](https://github.com/kevinWangSheng/production-ops-agent/pull/16#issuecomment-5618111383)，Security未确认或返回。不能据用户表示恢复就覆盖服务端实际拒绝，也不继续重复触发或扩大额度。两条最新发现仍有a100c90修复/独立验证，待当前实现远端复审；PR未就绪。

未追加模型/trace或启动服务，旧20请求账本及未知占用保持。主工作区发现用户新增.playwright-mcp/未跟踪目录，未读取/修改/清理；任务worktree不吸收该工作。该次仅补重试证据，M1入口与M0-03所需新实验授权不变。

## 本地双轴 Code Review 替代与修复（2026-09-10）

用户明确指定本地eng:code-review替代远端Code Review。按PR16固定base e9d22e9→af24646，Standards和Spec由两个全新上下文Agent并行独立审查；仓库已有SPEC/C3/M0计划与冻结合同，未安装issue-tracker/setup平台。完整报告见[本地双轴审查](../evidence/m0-real-investigation/round-02-local-code-review.md)。Standards确认0硬违规、2非阻塞维护建议，未做无关重构。Spec发现P2：bridge仅凭可解析JSON覆盖运行器incomplete/failed终态，已先复现后修复。

修复仅涉及bridge及两份测试/探针：严格状态由可信运行器记录决定，合法handoff保留完整CLI审计。实际Holmes假传输发现运行器未采纳正文但safe response有候选时，仅failed/incomplete允许保全同次未采纳候选并明确NULL/MISSING来源，不补capture、不改原result、不认证完成。错Run/请求或非空正文冲突仍拒绝。原schema和真实失败不改写。

[Spec独立复验](../evidence/m0-real-investigation/round-02-local-spec-revalidation.md)10状态/CLI+8来源反例+真实Holmes假传输0HTTP+244回归通过；[Standards增量](../evidence/m0-real-investigation/round-02-local-standards-fix-review.md)0新增问题。root隔离tracked代码副本全量复跑601 passed/44 PG默认skip，首次缺.venv相对入口导致的1项环境失败保留，未安装依赖。补丁应用后SHA须与已验证3文件一致，秘密扫描通过后按原流程提交推送。

本轮本地Code Review完成后，用户表示bot额度恢复，要求后续直接使用GitHub bot。最新修复将提交Code/Security复审，不再启动本地双轴流程；旧额度拒绝保留为历史，新的服务端结果现场核验。PR不自动合并。M1仍因真实报告质量FAIL不开放，任何新模型实验仍须新授权；本次0模型/trace/后端/服务操作。主工作区.playwright-mcp/无关工作未触碰。

## 恢复bot后的时钟sidecar修复（2026-09-10）

bot已恢复接收，cec52d2两项CI成功（34478903940）。12:56:16Z bot返回P1 3979271073：initial-timings-file可覆盖bundle已验证时钟。实现者以实际Holmes fakepipe先复现缺时钟却补造成功，再改为完整已验证Timing回显，删除赋值覆盖；匹配/等价UTC/no-flag保持，补造/清空/身份或来源依据变更在凭据读取与transport前拒绝。完整11场景及615 passed/44 PG默认skip通过。见[修复与hash](../evidence/m0-real-investigation/round-02-bot-timing-sidecar-fix.md)。

此组按用户最新要求直接交GitHub bot复审，没有再次本地双轴流程；不是新的产品功能或付费实验。所有既有数据/worktree/服务停止状态保持，M1仍因真实质量FAIL不开放。当前修复待最新CI/Code/Security结果，不自动合并。

## bot 控制代次时间关系修复（2026-09-10）

61850c1两项CI通过（34482332739）。bot返回P1 3979613299：旧dispatch能重标签为稍后创建的新Run/generation并通过；已以cancel/correct两反例现场复现。严格v4增加本代控制事件下界、下一事件的dispatch上界、控制时间顺序及缺钟/未知代次检查；不对迟到response/capture加下一代时间上界，仍由原身份/最终控制绑定防止旧结果成为当前结论。见[实现与版本](../evidence/m0-real-investigation/round-02-bot-control-time-fix.md)。

最终636 passed/44 PG默认skip、ruff通过；仅v4与两测试文件，旧schema/legacy/DB未改。原合成正例时序错误纠正且保留对应负例，真实历史时间没有重写。按照用户最新要求等待GitHub bot对该补丁复审，没有本地双轴流程或真实模型/服务操作。现行Code/Security仍待最新提交结果，M1继续not cleared。

## bot projector 源码认证安全修复（2026-09-10）

d61f80a两项CI成功（34509309368），bot返回安全P1 3981914677：report-only跳过runtime绑定，bundle自报source/dependency路径+hash可导致选中Python函数在guard前执行。已增加独立于输入的固定可信组合清单，所有import/replay/standalone dependency入口在执行前验证整组字节；不从目录或bundle自动信任，不增加生产测试开关。default SOURCE回归当前仓库固定路径，历史source仍明确指定保留。见[修复与证据](../evidence/m0-real-investigation/round-02-bot-projector-trust-fix.md)。

实现者先用无害marker复现5类代码执行，再验证拒绝；不读取真实秘密或provider reasoning。651 passed/44 PG默认skip、333相关安全/合同及实际wrapper report-only正反例通过；真实历史重放只读保原结果。active fakeprobe在原基线和候选均受旧2026-09-10T17:14:30Z查询截止限制而失败，两个日志保留；未延长旧授权、未重跑真实模型。该限制及新版真实质量验证仍需下一独立实验合同，不影响原已在有效期内采集的历史证据。

本修复没有schema/PG/服务变化，旧数据/代码快照保留。按用户最新要求等待bot最新Code/Security，不调用本地审查Agent、不自动合并；M1仍因完整报告质量FAIL保持not cleared。

## bot question 文件与人工状态修复（2026-09-10）

a783fc2两项CI成功（34515962307），Code Review返回P1 3982439739/3982439749；同提交Security 18:54:30Z完成无发现。已将question敏感路径检查放在所有内容读取前，合法问题仍在原scope/phase校验后才读，归档前完成检查；使用合成路径/read-spy证明无真实秘密读取。paused无当前审计能力故严格拒绝，cancelled/waiting_human须匹配最后接受控制；无控制状态同样不能认证。

[合并修复与版本](../evidence/m0-real-investigation/round-02-bot-question-human-fix.md)记录原红例、校验顺序回归及最终677 passed/44 PG默认skip。保留原测试语义，未增加pause平台、schema/DB/legacy变化或本地review流程；0真实模型/trace/服务操作。安全/审查通过不替代仍失败的真实报告质量，M1不开放。此批提交后等最新bot Code/Security，不自动合并。

## CI 截止时间 rollover 修复（2026-09-11T02:25:28.885466+00:00）

CI 34553834336 曾因固定实验deadline已过期导致4个Budget测试在reserve前失败；生产deadline保持真实值。补丁仅增加Budget默认real-clock seam和四个测试的显式predeadline fixture，作者48项定向通过；root在合并现有修复后`make check`全量通过（详见round-02-bot-budget-full-check.txt），无模型/trace/PG/服务操作。旧失败及原因保留，不把它改写为产品质量证据；最新代码仍需bot复审。

## 2026-09-10 当前交接更新：M0 仍未完成

### 现场状态

- 当前任务 worktree：`/Users/shenghuikevin/dev/AI/production-ops-agent-m0-01`。
- 当前提交：`055e646`（`fix: tighten M0 evidence and control validation`）。
- 本地 `make check`：689 passed，44 skipped；ruff 与 format check 通过。
- PR #16 仍为 OPEN；当前 HEAD 的 `checks` 与 `m0-postgres` CI 均 SUCCESS。
- Code Review 已返回并覆盖 `055e646`，未产生新的 finding。
- Security Review 已多次触发，但对 `055e646` 没有运行反应、完成评论或失败/限额结果；按外部服务不可用处理，不记为通过。

### 已核实的实现

`055e646` 已包含并通过本地回归：输入重组与实际交付绑定、prepared generation 控制授权、Holmes 实际 checkout 代码摘要、日志凭据投影拒绝；历史 schema、旧账本、原始失败和旧工作区保留。

### M0 未完成项

- 真实主动故障调查的最终报告质量仍 FAIL；本地/离线 v4 证据不能替代新的真实模型复验。
- 动态证据、目标/依赖授权范围、实际来源、查询、时间窗、view/artifact hash、截断与缺失字段仍需对应 M0-03 验收证据。
- 首流程逐步骤恢复、取消后迟到结果、版本不兼容 handoff 的真实组合证据仍不完整。
- 实际 Holmes 宿主 OS 隔离仍是明确边界限制，不能用容器探针拒绝证据替代。
- SPEC 的 feature implementation gate 继续为 `not cleared`；不得更新 feature passes 或安排 M1-01 实施。

### 当前授权与额度边界

- 历史轮次的 `20 CNY / 20 模型 HTTP / 5 trace`、旧 deadline、实际用量和费用估算均为历史记录，不能改写或复用旧账本。
- 当前没有新的具体费用上限记录；不得自行推断无限付费或自行创建额度规则。
- 在没有新明确预算合同前，继续执行本地代码、测试、证据核对和文档交接；不新增模型/trace 请求。
- 当前执行入口的过期保护不得通过改写历史合同解除；如需真实复验，先记录新的独立授权与预算合同。

### Handoff

接手者先核对 `git status`、`git log -1`、PR #16 HEAD、CI 和 bot review 是否覆盖当前提交。然后从 M0-03 开始：逐项把动态 evidence provenance、主体/依赖授权、当前 control generation、步骤恢复与取消结果写成确定性合同和测试；每项保留失败样例、修复 diff、测试输出和来源 hash。不要启动已停止的专属 OTel/PG 环境，不要读取或复制凭据，不要重用旧 20 请求账本。真实模型/trace 复验只有在新合同落盘并核对账户状态后才能执行。完成本地工作后更新本记录、ROADMAP、SPEC 决定证据，推送 PR #16；Security Review 仍需以当前 HEAD 的实际返回为准，不能用旧提交结果替代。

## 2026-09-11 M0-03 接续（质量审查）

上段交接之后，环境已被后续执行者重启，`038401b` 已加入 round03 runner；真实 Holmes 已跑。本段起按 [M0-03 质量审查合同](../evidence/m0-real-investigation/round-03-quality-review-contract.md) 推进：**先审查已返回报告，不新增模型/trace**。旧交接中「不要启动已停止环境」对当前 Running 的 `m0-otel` 不再适用；本步也不 stop。

### 现场核对

- 任务 worktree：`/Users/shenghuikevin/dev/AI/production-ops-agent-m0-01`，HEAD `038401b`，分支 `chore/m0-02-convergence` 与 origin 同步、工作区干净。
- 环境 worktree：`/Users/shenghuikevin/dev/AI/production-ops-agent-m0-environment`，HEAD `2e73f7a`（同 runner 信息的本地分支，无 upstream）。tmp 运行数据只在此目录。
- PR #16 OPEN；`038401b` 的 checks 与 m0-postgres SUCCESS。Code Review 对 HEAD 无 major issues。Security Review 对 HEAD 为 unknown error，不记通过。
- Colima `m0-otel` Running，Compose `opspilot-m0` 约 26 容器在跑。专属实验 PG 55431 未监听。系统 PG 不动。
- 主工作区 `main` 有用户未提交文档（取消历史 20/20/5 总量上限）及未跟踪 `.playwright-mcp/`；任务 worktree 不吸收。

### 已执行但未审查的 M0-03 Run

| Run | HTTP | 机器状态 | 处置 |
|---|---|---|---|
| m003-fault-01 | 4 | incomplete / FACT_SCOPE_REQUIRED | 失败保留 |
| m003-fault-02 | 4 | incomplete / FACT_TIME_SCOPE_NOT_DELIVERED | 失败保留 |
| m003-fault-03 | 0 | failed / phase HTTP budget | 失败保留 |
| m003b-fault-01 | 4 | investigation_returned | 窗无可诊断失败，不计入故障正向 |
| m003c-normal-01 | 3 | investigation_returned | 质量 FAIL（P2 span 计数） |
| m003c-normal-02 | 3 | investigation_returned | 质量通过 |
| m003c-fault-02 | 4 | investigation_returned | 质量 FAIL；本窗无失败 trace |

`m0-03c` 账本 10/16 HTTP（normal 6/8，fault 4/8，pg 0/4），0 trace。所有 returned 的 `quality_assessment` 仍为 `pending_independent_evidence_check`。本步 0 新增模型请求。

### 当前进展

质量审查合同已落盘。协调者已完成四份 returned 报告与三份合同失败的只读核对（独立子代理宿主 402，结论不标独立 Agent 通过）：[汇总](../evidence/m0-real-investigation/round-03-quality-summary.md)。正常 1/2 质量通过（仅 normal-02）；故障 0/2 正向通过。SPEC 门槛保持 not cleared。

下一步：先取得本窗可诊断故障工程前提，再决定是否用 `m0-03c` 剩余 fault HTTP 做新 Run；不打开 M1，不新增未授权模型调用。

### 一次完整故障案（2026-09-11）

已执行注入保持 300s + 独立观察 + Holmes `m003d-fault-02`（4 HTTP / 16 工具，returned）。前提：10 条失败 checkout trace，Charge code2 increase=7.5。flag 已 restore。协调者审查见 [m003d-fault-02](../evidence/m0-real-investigation/round-03-m003d-fault-02-review.md)。`m0-03c` fault 8/8、总 14/16。v4 故障仍缺第 2 个独立 Run。M1 不开放。

## 2026-09-11 M0-03 真实调查线推进

- 专属环境分支 `chore/m0-02-environment` 提交 `2e73f7a` 保存 round03 runner、真实 normal/fault/recovery 观察及 M003 环境证据；Ruff 通过。`m0-otel` 当时保持 Running，未清理旧数据。
- 经过基线筛选，仅将新增 `round03.py`、M003 观察工件及 runner 配置切换带回任务分支；任务分支新提交 `038401b`，未带入环境分支的历史删除/差异。
- `038401b` 本地完整测试：689 passed，44 skipped；CI run `34587894742` 的 `checks`、`m0-postgres` 均 SUCCESS。
- `m003c-normal-01/02` 真实报告均为 partial，明确“可见证据未发现失败”及日志/trace/baseline 缺口；`m003c-fault-02` 真实报告支持 checkout→PaymentService/Charge 依赖方向，但明确窗口 residency 和逐请求因果未知。报告没有宣称 healthy 或故障因果已证实。
- 当前 PR #16 HEAD 为 `038401b`。Code Review 已返回“Didn't find any major issues”，明确覆盖该 HEAD。Security Review 于 2026-09-11 10:13Z 重试后出现 `eyes`，截至本记录没有完成或失败结果；不能记为通过。
- 当前可执行 M0-03 证据已从“无最终报告”推进到“正常与故障均有最终 partial 报告”，但 M0 gate 仍因报告覆盖缺口、完整恢复/取消验收、动态证据边界及 Security Review 未闭环保持 `not cleared`。下一步是安全审查结果出现后逐项处理，或将其记录为外部服务阻塞；不得用本地测试替代真实证据。

## 2026-09-11 本地安全替代与环境收尾

- 按用户指示不再等待 GitHub Security Review，改用本地确定性安全检查：秘密扫描、凭据投影、projector trust、question path、runtime strict 共 57 passed。
- GitHub Code Review 已明确覆盖 `038401b` 无 major issues；Security Review 对后续 HEAD 未形成可用结果，不记为通过。
- 专属 `m0-otel` 已停止；`default` Colima 未操作，容器/卷/VM 和原始证据保留。`m003d-fault-observation.json` 作为开发观察原样保留，明确不构成完整健康或故障因果证明。
- 当前 PR 仍需根据最新 HEAD 的 CI/Code Review 重新核对；本地安全替代不改变 SPEC gate，M0 仍保留真实报告覆盖、动态证据完整性和恢复/取消边界缺口。

## 2026-09-11 最终入口审计前状态

- m003e-fault-05：4 HTTP / 14 工具，strict `completed/supported`，time policy 已送达；支持 checkout→payment Charge code2 / PlaceOrder code13，保留 HTTP 直接映射、payment 日志、采样和 SLO gaps。
- m003e-normal-02：3 HTTP / 11 工具，strict `completed/supported`，time policy 已送达；关键 checkout ERROR/PlaceOrder/Charge error rate 为 0，保留采样、日志源和 SLO gaps。
- `m003e-baseline-comparison.json` 已对同 integration 相邻 normal/fault window 的 calls 与 checkout-rpc 做确定性对照；不把比较写成总体失败率或健康认证。
- m0-otel 已停止，default Colima 未操作；原始 observations、raw/view/hash、容器/卷/VM 和所有历史失败保留。
- 最新任务 HEAD：`08800ed`。本地 `make check`：689 passed / 44 skipped；CI `checks`、`m0-postgres` 对该提交通过。Code Review 最新明确覆盖代码提交 `f10ebe0` 无 major issues；其后仅为文档/证据提交。Security Review 按用户指示忽略；本地 57 项安全替代检查通过。
- 当前剩余不是代码执行阻塞，而是人类入口判断：是否接受仍明确披露的 telemetry coverage、日志来源、HTTP 直接映射、唯一请求数和 SLO unknown；以及是否审核/合并 PR、是否保持 SPEC gate not cleared 或另行记录 M1 入口决定。未经该判断不更新 feature passes、不开始 M1 产品实施。


## 2026-09-11 独立审查与 unknown 处置

- 四份 returned 候选由独立 Agent（全新上下文、0 模型请求、只读）逐 claim 对照 view/raw/工程观察审查，见 [汇总](../evidence/m0-real-investigation/round-03-independent-review-summary.md)。结果：正常 1/2（m003c-normal-02 通过；m003e-normal-02 因 Envoy 字段误读 P2 失败），故障 1/2（m003e-fault-05 通过；m003d-fault-02 因不可见跨 trace 关联与投影省略当缺失两处 P2 失败）。**v4 有界开发包未通过。**
- 用户决定接受报告保留的五条 unknown 为已披露限制；三项后续归属（checkout/payment OTLP 日志、代理日志 path/status 过滤、HealthProfile）记入汇总，不随接受关闭。
- 附带发现：m003e-fault-05 送模 question 含 m003d 残留窗口字段，报告未受影响，runner 拼装需修正并加回归。
- SPEC gate 保持 not cleared。下一步需新授权：离线修 runner 输入拼装，再补 1 正常 + 1 故障真实 Run 并独立审查；不重用旧账本、不改 passes。

## 2026-09-11 用户决策：接受已披露限制

用户明确接受 M0-03 报告中的五条 unknown 为当前环境与工具契约的固有限制，不把它们视为报告错误或当前 M0-03 通过条件：bounded trace/log sampling、当前集成日志源缺失、日志过滤/allow-list 与展示上限、HTTP 500 无法总由当前 access-log view 直接映射、缺少 HealthProfile/SLO 因而不能认证总体健康/恢复/失败率。日志源、日志过滤和 HealthProfile/SLO 记录为后续任务。原始报告、raw/view/hash、失败和 unknown 原样保留；本决策不自动打开 M1 gate 或修改 feature passes。

## 2026-09-11 Envoy projection P2 修复与独立复验

- 独立复验 Agent 对 `f6e9c53` 全新只读复验：定向 11 passed，Ruff 通过，无新 P1/P2。
- 修复内容：Envoy parser 严格要求完整 22-token pinned 格式、有效 ISO 时间、拒绝嵌入/截断/尾部垃圾；v4 对正常/错误 proxy.access 行统一接线；v2/v3 legacy replay 保真；bridge AST replay 白名单包含 `_envoy_access_fields`/`_log_projection_v3` 及 legacy 依赖；question scope/run metadata stale mismatch 在发送前拒绝。
- 旧 m003e-normal-02 的模型报告原文及其独立 P2 仍保留为历史失败；代码修复不能倒推旧报告已通过。当前未新增模型/trace，若要取得新报告质量正向样本需另行真实 Run；在当前约束下不重跑。

## 2026-09-11 Envoy P2 最终复验与环境收尾

- `f6e9c53` 的独立只读复验通过：定向 11 passed、Ruff 通过；parser 严格 22-token/ISO/CRLF/尾部校验，正常 200 与错误 500 v4 接线，v2/v3 replay 保真，bridge helper/legacy dependency whitelist 完整，stale scope/window/revision/run_id 拒绝通过；无 P1/P2。
- 任务分支完整 `make check`：699 passed / 44 skipped；CI 对当前 PR 仍需以最终 HEAD 结果为准。
- 本轮复用专属 OTel 后已停止，default Colima 未操作；原始容器/卷/VM/observations/账本保留。
- 旧 m003e-normal-02 与 m003d-fault-02 报告质量失败原样保留；新 projection 修复不倒推旧报告通过。在当前不新增模型/trace约束下不重跑旧报告；SPEC gate 继续按既有决策处理。

## 2026-09-11 PR16 最终 review disposition

- `original/actual` 输入绑定、prepared stopped generation、标准凭据路径 denylist、Envoy v4 projection/replay P2 已修复并通过定向/完整测试及独立复验。
- Security Review 按用户明确指示忽略，采用本地确定性安全检查；旧 security findings 不作为未处置代码门。
- aggregate paid-request limit 评论与当前 AGENTS 的用户授权冲突；当前实现保留 per-Run deadline/request/tool/query、usage ledger、unknown 费用和只读出口边界，不恢复上一轮固定总量 cap。
- 旧 m003e-normal-02/m003d-fault-02 报告质量 P2 仍是历史失败；修复代码不倒推旧报告变绿，当前 no-new-model/trace 约束下不重跑。

## 2026-09-11 M004 独立复审完成

- M004 normal/fault 两个新 Run 的 committed raw evidence 各 7/7 SHA-256 匹配 observation 引用。
- 全新上下文独立审查确认两 Run 无 P1/P2，normal/fault 均可计入 v4 正向样本；质量计数更新为 normal 2/2、fault 2/2。
- 报告仍为 partial、保留五条已接受 unknown 和日志/SLO/采样边界；没有把 partial 改写成 healthy/recovery。
- 这完成了本轮可执行的 M0-03 质量包；SPEC gate 是否开放、PR 是否合并仍保留给用户最终判断。

## 2026-09-11 PR16 收束与 B1–B3 离线补证

### 目标与依据

依据用户本轮任务 A1–A4、M0 八个工作包（`docs/plans/m0-validation-plan-2026-09-07.md`）和 SPEC gate（仍 `not cleared`），在不新增模型/trace、不修改 feature passes 的前提下完成 PR 处置、退出矩阵、预算校准记录和确定性 B3 合同。

### 已完成

- A2：默认 v4 log projection 固定 `normal02` model-visible 行数为 14，并在测试中注释 14,000-byte 上限与 Envoy 标签取舍。
- A3：parser docstring 明确 pinned 字段严格但容忍 shell quoting/重复空白；scope binding 明确 best-effort 顶层检查；移除 `.aws/.docker` 下 `credentials/config.json` 的死规则，新增 `.pypirc`、`id_ed25519`、裸 `credentials` 读前拒绝用例。
- B1：[`m0-exit-matrix.md`](../evidence/m0-real-investigation/m0-exit-matrix.md) 汇总八包状态、证据路径及 M1-01 约 28h 拆分。
- B2：从 M002 token/timing 与 M003/M004 HTTP/工具计数回算可见分布；旧 128KiB/8192/4/20/180s/20s/780s 标为不得冻结，v4 写入候选 512KiB/2MiB、16384 output、4/20、360s/1800s、30s/240s，工具累计与清理上界仍待实测、最终待用户批准。
- B3：新增 no-data/stale/缺 profile/HealthProfile 最小 schema 确定性回归；专属 55431 PG 实际运行 StepStore 27 passed、Budget 9 passed、DB stop/start 1 passed，服务已停止且数据目录保留。发布竞争、HealthProfile 乱序、暂停/resume 和单独观察并发仍保留失败样例和证据不足状态，见 [`m0-b3-deterministic-contracts.md`](../evidence/m0-real-investigation/m0-b3-deterministic-contracts.md)。

### 独立复验处置

全新上下文独立复验见 [`round-03-convergence-independent-review.md`](../evidence/m0-real-investigation/round-03-convergence-independent-review.md)：初次发现 A3 的 `id_ed25519` 用例被 `.ssh` 父目录规则遮蔽、B2 统计把缺失 usage 当 0 且混入 M004 的 9 工具下界。已补普通目录 basename 用例并改正校准统计；最终定向 162 passed，完整 `make check` 716 passed/44 skipped，历史发现和原始输出均保留。

### 验证与待办

待运行本 worktree 的定向测试与完整 `make check`，输出保存到 `docs/evidence/m0-real-investigation/`。PR review thread 回复、最新 HEAD CI/bot 覆盖和 PR 描述更新属于 A1/A4 收尾；B4–B8 不在本轮离线授权内。
