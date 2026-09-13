# M0-02 本轮执行合同

实验 `m0-02-20260910-convergence`。开始采用保守时间2026-09-10T01:49:00Z，绝对截止2026-09-11T01:49:00Z；准备与等待计入24小时，重启不延长。用户本轮授权20 CNY / 20实际模型HTTP / 5次trace上传，旧24 CNY未核账占用完整保留。该合同与[授权](round-02-authorization.json)、[实施前审查](round-02-plan-review.md)共同约束执行；源码实现和独立定向验证完成后才可实际发起。

## 目的与顺序

先用fault-02保存的12份业务view运行独立新Run `m002-saved-report-01`，不查询工具、不继承旧provider私有字段。这仅检验输出额度及报告合同，不改变旧主动故障0/2的结果。得到可评分报告并独立核验后，恢复专属OTel Demo，固定新的正常窗和一个有独立可诊断事实的故障窗，模型自己使用Holmes原循环查询并报告。

同时本地验证v3外部合同和PG必要步骤/取消机制，随后取得新的真实DeepSeek/PG组合证据。只完成首流程入口前提，不建设完整平台，不改产品passes。SPEC/M1入口须等最终独立证据判断。

## 参数、超时与费用

全部真实请求直接deepseek-v4-flash / thinking enabled / high；固定Holmes 5e983c17，原循环最后一步tools=None。context131072 tokens包含32768输出预留，估计输入98304；HTTP bytes另限524288，响应2097152。禁止隐式compaction和自动重试，保留全部同Run必要私有协议，不通过删除字段或改模式避限。token估计用于容量诊断，不宣称未知tokenizer是硬计费上界。

请求360秒总wall、Run1800秒总wall，每次同时取本轮绝对deadline较小值。实际HTTP在可终止子进程内，清理预留2秒TERM+2秒KILL/join，包含在对应总wall内；外层独立进程组supervisor结束整个Run和子进程。每工具30秒，累计工具工作240秒，最多20查询；不把IO timeout或线程退出当硬清理。主动源另受既有只读proxy的更早截止2026-09-10T17:14:30Z限制，未改原runtime/archive。

每实际HTTP先持久预留3.44064 CNY：供应商1Mi输入按峰值3 CNY/M、32768输出按9 CNY/M。只在响应usage完整、自洽且未超假设时将本轮预留转为已知峰值全cache-miss费用上界；这是计费上界依据，不是已核账实际费用。未知/失败/崩溃保留全额。旧4+20 CNY完全不参与释放。每个物理尝试、失败、HTTP状态、usage、起止和上界单记；禁止只计成功请求。

子额度：report4 CNY/2HTTP（默认1），normal5/5（默认最多4），fault5/5（默认最多4），pg4/4（默认2），未分配contingency1.5/4，trace0.5/5。合计20CNY/20HTTP/5上传；contingency不足单请求预留，必须由父执行者在总额不变时先落盘转拨，不能自动借款。trace适用费用尚未核验，当前实际上传固定0。新总账在环境worktree `tmp/m0-environment/m0-02-request-ledger.json`，全Run持有对应.lock；不复用旧账本。PG实验预算只处理对应机制的持久预留，不冒充与文件总账跨存储原子。实际PG模型请求也经过同一全轮守门。

## 交付合同与判据

报告明确列事实、假设与因果置信度、反证、未知/缺测/截断和建议。区分调查未完成与完成但结论不确定；每个事实引用完整evidence_id，不把spans当订单，不在未冻结SLO/覆盖度时宣称healthy或恢复认证。建议只提供人工参考，模型不能执行修复或获得发布门禁。

保存完整原始业务证据，实际模型view另存投影版本/hash、来源/实际身份/查询窗、保留及省略计数。真正HTTP请求的业务message快照由可信运行器提取，不依赖模型自报可见；准备但未发送不算已交付，timeout对provider消费状态保留unknown。主动Run初始输入只有症状、目标、接入与授权清单，无最终证据和工程注入答案。完整同Run私有协议只由程序在受限私有目录/PG字段处理，不交评审、模型业务报告、trace或Git；凭据只由可信客户端从主仓库.env读取供认证。

正常报告须以实际流量、窗口和可见来源支持“未观察到异常”或有依据unknown，披露缺口。故障报告须主动取得证据，结论与独立工程故障事实及模型实际view相符；不能拿工程事实替代模型结论。两例各一次开发案例，不作泛化、盲测或可靠性统计。saved报告不是主动调查成功。

关键机制按候选矩阵逐一确定性断点，真实PG跨进程重建至少覆盖ModelStep/工具结果提交前后，保留原观察时间和配对；新Run/owner/epoch/lease以及取消提交前后发起竞态分别验证。真实模型/PG续传另列证据层级。版本不兼容blocked/handoff，旧记录不删除。

## 失败和收尾

无新证据不重复同一失败请求；遇length、超限、超时或合同不符先保全诊断，不默默抬值/降thinking/换Pro。任一累计次数、费用或期限到限停止付费，继续必要本地修复与记录。未知费用留账。

root为唯一OTel/PG环境负责人；工程注入使用新experiment-id目录保存原字节，最后按字节还原并采独立有流量观察。仅停止专属服务/profile及原专属PG，保留容器、卷、数据库、归档、失败及所依赖worktree，不动系统PG/其他项目。共享宿主和worktree不能证明OS隔离；本轮明确为开发环境证据。最终代码自测、独立验证及远端PR最新CI/Code/Security审查闭环后交用户，不自动合并。

## 02:24Z 第二saved请求前的有据调整

首m002-saved-report-01实际1HTTP200、0工具，本地响应校验RuntimeError且没有业务报告。安全状态与代码路径指向model字段校验分支，但返回model具体值丢失、unknown，不能放宽身份或称length。修复先保存受限raw及安全model/usage/finish/content再拒绝异常，合成异名/非JSON响应和13定向检查及独立复验覆盖。第二请求m002-saved-report-02仅获取有诊断保全的报告结果，不同Run、无原私有协议恢复；其12业务views与预算参数保持。

首3.44064未知全额保留。父在发送前持总账锁转拨：report7、normal4.5、fault4.5、pg3.5、contingency0、trace0.5 CNY，总20不变；各HTTP上限2/5/5/4/4不变。新请求后若仍异常，按实际安全诊断处理，不重复同一未解释失败。旧24 CNY未核账不动。

## 服务报告名称与后续主动验证决定

独立核查实际官方/models元数据、第二安全响应及官方文档后，允许本轮response名称deepseek-v4-flash或deepseek-flash，出站仍只deepseek-v4-flash/thinkinghigh，拒绝Pro或其他。详见provider-identity-decision/review与models安全工件；reported alias不是固定权重证明。历史两次failed不回改。第二完整usage可追加记录0.243099 CNY峰值上界（账本accounting_reviews保留此前unknown状态），首3.44064未知及旧24不释放。

保存证据实验已实际验证32k空间可产出15104输出tokens/stop正文，同时独立报告质量评估发现6项P2，保留为未通过报告质量。原计划要求saved质量先完全通过再进active，是本轮候选顺序而非用户产品范围；为不重复保存证据请求，父决定将已识别的通用约束及父子/来源视图改进带入真实正常/故障主动验证。它不把saved或原主动故障改记通过；后续质量仍须逐claim独立核查。新限定模型服务映射和view/prompt版本先离线审查，尚未发送active。

当前子额度调整为report4/2HTTP（已2，停止该phase）、normal6/7、fault6/7、pg3.5/4、trace0.5/5上传（仍禁用）、contingency0；总20 CNY/20模型HTTP不变。默认每active Run仍最多4HTTP，不因phase余量无限重试。

## 正常末步协议与PG首请求兼容失败

normal01实际4HTTP200/16工具，末步stop却仅DSML工具调用正文（2416 completion，非length），独立评审报告0/1。具体生成原因未知；下一有据适配为显式closed collection控制消息+none tool_choice+json_object及本地报告schema检查，完整同Run私有字段保留；4/32768/512KiB/360不变，不把格式失败归结为诊断能力。normal02另立Run，复用相同真实历史查询窗而非预载最终证据。

PG第一组合请求ordinal7实际HTTP400，官方安全error为Thinking mode does not support this tool_choice。原driver强制具体function不兼容；原已成功固定工具链使用auto。保持thinking/high，修为auto且仍校验exact read_fixture/target，另立新Run/新record，不修改旧稳定input或冒称同Run恢复。该400无usage，3.44064未知仍保留。response原始body已在受限PG保全，安全诊断见round-02-pg-first-diagnostic.json；初期错误分类误写modelmismatch也保留并修正新路径。

当前累计7实际模型HTTP（其中此400），子额度report4CNY/2HTTP、normal4.3/8、fault4/7、pg7.2/3、trace0.5/5且禁用，总20CNY/20HTTP不变。PG额度含原失败，最多另2请求；默认active仍4/request Run，工具20。达到任何限额即停止付费，无新证据不重复。

## 最后两个主动开发案例的冻结安排

按normal02真实质量失败，新增语义保真view：保留原query/result，明确instant evaluation/授权窗与值的时间范围不同；raw counter与bucket不能当窗内增量，类型未知不假定；日志区分backend returned/model visible/omitted。不替模型改query，不给工程答案。允许有界3步（上限仍4），全部active权限与末步闭合不绕过；独立48tests及实际Holmes假pipe3/4步通过。fault使用4步，normal03使用最后3步，输出32768/512KiB/360/1800等不变。

17HTTP之后已还原故障，normal03用新的还原后300秒窗口作正常对照；只给请求与来源，实际再查询，不把工程恢复摘要当初始证据。当前子额度report4/2HTTP、normal6/11、fault5.5/4、pg4/3、trace0.5/5且禁用，总20/20/5不变。保留旧24及两未知3.44064，不回写任何旧失败。
