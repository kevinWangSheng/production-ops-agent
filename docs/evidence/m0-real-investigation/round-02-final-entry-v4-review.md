# M0-02 严格 v4 接续后的独立实施入口复核

日期：2026-09-10。审查者以全新上下文启动，未参与实现。工作区 `production-ops-agent-m0-01`、分支 `chore/m0-02-convergence`，起始 HEAD `da02563` 加未提交的严格 v4/report-v2 候选。

**入口建议：保持 SPEC 的 not cleared，不进入 M1-01。** 原正常与故障报告存在独立可复现的事实/可见范围错误；新版严格合同和投影仅有离线证据。机制修复完成、schema冻结或历史结构检查通过，均不能改写真实报告质量 FAIL。

## 本次亲自核查

读取 AGENTS、完整 SPEC、ROADMAP、M0 执行计划、当前任务、v4候选包、统一严格接缝设计、结果和最终用量；沿原故障、normal03、PG独立记录核对安全业务原件。静态检查 `outcomes_v4.py` 的完整报告/事实性scope DTO、`holmes_bridge.py` 的默认严格与显式legacy分派、`step_store.py` 的业务重建和发布权威边界，并阅读独立 runtime 复验记录。联合机制审查的最终稳定证据在下文单列，区分亲自读取原业务证据与其他独立审查者执行的机制测试。

本次0模型、0trace、0后端/数据库调用，无PG/Colima启停。未读取 `.env`、真实provider reasoning/private wire、注入配置或答案。不通过读取秘密来验证隔离。数据库、启停及注入还原事实引用原安全审查/收尾记录，未重新现场执行。

原业务目录为环境 worktree 的 `tmp/m0-environment/holmes-runs/`。亲自读取 fault01 与 normal03 的完整 `result-business.json` 安全报告（含 summary、claims、gaps、next_steps），最后 `delivered-business.json`、注册 view 和必要 raw：

- fault01：19份manifest，每份raw/view文件SHA256及canonical SHA256均重算匹配；最终 `m002-fault-01-http-17` 为response_received，19个view，messages canonical hash匹配。
- normal03：12份manifest，同样全部匹配；最终 `m002-normal-03-http-20` 为response_received，12个view，messages canonical hash匹配。
- canonical算法采用manifest写明的 `json.dumps(ensure_ascii=False, sort_keys=True)` UTF-8；没有拿full-wire hash冒充安全business hash，也未打开受限wire验证。
- 本地 `tmp/m002-v4-replays/` 两份strict compatibility的完整原文、解析对象与原报告逐项相等，原文hash匹配；scope/freshness均unknown，current_acceptance_pass=false。旧报告没有被补造target/time或重写为v2。版本管理中的同名工件是元数据导航，不是原业务报告全文。

## 实际质量与能力边界

故障核心有限定位有据：亲自核e11中checkout→PaymentService/Charge code2的5m increase约11.29778、code0为0；e18中PlaceOrder code13约11.29778、code0为0；e13 payment窗口交易增量为0。e8实际显示的trace `490fdb46cfa2ce55c61bbe602016604e` 可按CHILD_OF连通load-generator、proxy、frontend至checkout PlaceOrder，HTTP500与code13一致。payment更深定位来自client metric，不能说14条显示span已经给出完整payment链，也不能据此猜隐藏触发机制。

原故障报告质量仍FAIL：e8实际14/349、335省略，而counter_evidence/gaps写20；安全raw有payment错误详情而原view省略，报告summary/gaps将可见缺口泛化为permitted telemetry缺失。cart跨trace/覆盖错误沿原逐claim审查保留，本次未重新逐条核cart日志。建议中的integration称cluster亦未获精确cluster身份支持。核心成立不足以抵消这些P2。

正常有限观察有据：e8 checkout Charge code0增量5、code2为0，e9 checkout server code0增量5、code13为0；e2 checkout ERROR显式0。它支持该窗未观察到checkout失败，不认证全环境健康或独立持续恢复。

normal03质量仍FAIL：e11实际14/248、234省略，全文多处写20；e2 accounting只返回UNSET=5，没有ERROR系列，claim5却写ERROR=0。实际显示的 `ef94...` 有frontend gRPC PlaceOrder status0，而 `048623...` 只显示至frontend API路径，claim7把gRPC显示事实扩展到后一trace。raw存在被省略span不能倒推模型看到了。ad/recommendation ERROR各1.25仍保留，不能把正常对照解释为全系统无异常。

上述由原文、实际view及必要raw独立复现，足以维持0未处置P1/P2判据下的FAIL；不是只转述实现者或先前审查的结论。没有进行新模型复验、完整盲测、候选固定4次质量测试或产品验收。

## 恢复、费用与隔离披露

[PG独立结果](round-02-pg-live-outcome-review.md)证明的是固定fixture、真实DeepSeek/PG、两个stage正常退出后的同Run消息重建及续传；其中数据库布尔/行hash证据由原独立审查取得，本次没有重做。`StepStore.rebuild` 从提交业务记录按Run/step及hash重建、`publish` 约束提交响应与fence的静态结构与此边界一致。主动Holmes报告与PG机制是两条链；不能合成“真实Holmes完整崩溃恢复已通过”。父硬退出/取消的PG替身机制也不得升级为真实模型崩溃证据。

亲自重算[最终用量](round-02-final-usage.json)逐Run合计：20 HTTP、360798输入、39606输出、400404已知tokens；unknown预留6.88128 CNY。原账本文件hash与汇总的source_ledger_sha256一致。1.438848 CNY为已知usage峰值全miss上界；另两次unknown和旧24 CNY不释放。实际可归属账单为null，不能报实际花费1.438848或全部预留为实花。0trace与62真实Holmes查询/1fixture读取按本轮记录披露；本次不重新访问供应商账户核账。

[收尾记录](round-02-closeout.json)记载容器/VM停止、卷和业务工件保留、故障原字节还原；最新PG再次停止记录应与PR修复过程一同导航。共享宿主、worktree和fresh上下文均不证明OS隔离，旧container探针不覆盖实际Holmes宿主进程。原业务查询raw/view保留不等于未查询的易失Jaeger历史完整保留。当前结果文档对此限定准确，不要求为当前离线修复另建完整隔离平台。

## 下一最小任务与交付条件

继续M0-03报告事实与实际交付证据一致性：先完成v4/report-v2联合稳定代码/schema、离线反例与独立审查，冻结版本/政策/来源时间依据；保留原质量失败。获得新的具体费用、HTTP次数与绝对截止授权后，执行已规定正常/故障各2个独立Run，固定候选不临时调参，按实际交付证据全文审查，0未处理P1/P2且必要机制回归不退化才重评M1-01入口。没有理由重审未改变的C3架构，也不能以20次旧授权继续付费。

v4若对源时间缺失严格返回unknown，必须在候选前明确哪些真实来源具备合格时间依据；不能为使真实指标过关，把Prom求值时间或HTTP完成时间补成底层sample新鲜度。完整自然语言数量/因果/目标仍需独立审查。

历史导航发现：初审时任务顶部仍链接当前冻结v3，严格设计文档顶部仍称“未修改schema/运行时”。2026-09-10收尾复核确认已处理：任务顶部改为当前v4冻结首片包；设计顶部记录离线实现/联合终审完成并保留方案历史；v4顶部有冻结日期和联合终审依据。SPEC、ROADMAP、M0计划及结果同步离线冻结状态，继续保留真实质量FAIL与M1关闭。最新CI及已触发Code/Security Review未闭环前，PR也不算就绪。本记录不是合并授权。

## 联合稳定证据复核

已读取[严格接缝联合终审](round-02-pr16-v4-final-review.md)，并亲自重算四份当前源码SHA256，与该独立审查者固定范围全部一致：outcomes_v4 `ab47bf8c4e5f968771ff9980c200f9be54c68b61f528abccd54e2bee3b42896d`；bridge `bc257b2b6f0095b72148d1f790aefc352766b0f0d999701c3a65a898b9196f17`；runtime `89e4a26d5c997f734cf6a0386733fb3e1528fc12bc82a7623ecb85d117328d85`；report contract `f5d0adf01c48b0ad404f3737ee9d9d81598f38c583f1f15f2c9b3b125267546e`。

该审查者亲跑62项定向、9组独立外部输入反例、schema等值及真实旧Run严格拒绝/legacy保真；runtime另有固定Holmes假传输检查。可据其范围完成离线合同冻结，本次不重复宣称自己执行了这些测试。稳定离线修复已有独立证据，真实模型报告质量缺口和M1未开放结论仍不变。

本次导航收尾复核只读取上述文档并更新本记录，未重跑测试或修改代码。父任务另负责将任务/结果顶层入口链接切到本记录，并保留旧entry-review作为历史；本记录不提前声称该最后导航切换已完成。
