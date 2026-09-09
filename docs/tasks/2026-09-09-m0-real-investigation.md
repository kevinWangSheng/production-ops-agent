# M0：从协议链路进入真实调查

日期：2026-09-09；状态：本轮有界实验结束、服务停止保全；产品入口未满足。目标为取得首个产品纵向流程所需真实环境/上游/协议证据，依据SPEC实施门槛、C3、M0计划及F1/F2/F3/F7/F8/F12/F14。产品实施与passes未打开。

当前摘要：[完整结果](../evidence/m0-real-investigation/results.md)。20模型/1trace；Flash链路单次通过，正常第三次报告有质量限制，主动故障0/2交付、最后新Run输出耗尽。下一项M0-02收束预算/动态证据验收与首片必要状态前提；对应产品任务M1-01，SPEC不开放。下面各“进行中/等待”是按顺序保留的过程，最终事实以本摘要、最终用量及环境closeout为准。PR #15交付当前修复和失败证据，合并仍由用户审核。

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
