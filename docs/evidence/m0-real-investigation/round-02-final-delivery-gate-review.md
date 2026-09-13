# 本轮最终交付与 M1 入口独立核查

日期：2026-09-10。全新上下文审查者，未参与实现；工作区 `production-ops-agent-m0-01`，基线 `6eab0a4b139a130afde98f4105c39e1d15c145a8` 加本组尚待提交的初始证据、执行版本与当前 final 指针修复。仅写本记录，不修改实现、不提交推送。

## 结论

**M1 入口仍未通过。** 当前首流程 v4/report-v2 合同及初始持久证据补正有明确的离线独立证据；当前 final 指针修复有真实 PostgreSQL 加合成响应的独立机制证据。它们不能消除真实报告事实错误，也不能将 Holmes 主动调查与另一条 PG 两阶段续传合成完整真实调查崩溃恢复证明。SPEC、ROADMAP、当前首流程包及结果文档对此披露一致。本次没有发现需另外修复的交付披露 P1/P2；原模型报告质量 P2 仍未通过新版真实候选验证。

PR #16 尚不能宣称就绪或合并：本核查未查询远端最新 CI/审查；父任务交接明确最终安全审查尚无确认/结果。须由父执行者核对最新提交的 CI 和所有已触发审查闭环，本记录不替代该门槛或合并授权。

## 亲自核查的原始证据

已完整阅读 AGENTS、SPEC、ROADMAP、[当前冻结首流程包](../../testing/first-investigation-v4-2026-09-10.md)、[本轮结果](round-02-results.md)、[原入口核查](round-02-final-entry-v4-review.md)、[初始证据终审](round-02-initial-evidence-final-review.md)、[当前 final 指针复验](round-02-final-pointer-review.md)及最终用量。旧审查作为分工证据使用，不将其 PASS 当成本次亲自执行结果。

完整读取两份 `tmp/m002-v4-replays/*explicit-legacy-v3.json` 中的安全报告，包括 summary、所有 claims、gaps 和 next_steps，并对应原环境 worktree 的 `tmp/m0-environment/holmes-runs/m002-fault-01/`、`m002-normal-03/` 的 `result-business.json`、实际 `delivered-business.json`、选定原 raw/view/manifest。未读取 `.env`、私有 reasoning/wire、工程注入配置或答案。业务全文留在本地，不复制进 Git。

- fault01 原报告 SHA-256：`73de833693906686613b298ae7cd3de6d1c39e4ff6d5be0e085ef70e8acf6395`。
- normal03 原报告 SHA-256：`5b63ffc5394ab4773ec187e0aae3334a12fa5b2b3dc33b4f9c051e9efaf01461`。
- fault01 e8/e11/e18 与 normal03 e2/e8/e9/e11/e12：逐件重算原 raw 文件 hash、view 文件 hash、canonical view hash，均与 manifest 相等；从最后实际业务请求的 tool 消息剥离 `tool_call_metadata` 前缀后解析，逐对象等于保存 view。对应最终物理请求分别为 `m002-fault-01-http-17`、`m002-normal-03-http-20`，没有 union 不同请求。
- 首次简易核查脚本错误地将带 metadata 前缀的 tool content 当纯 JSON，出现 JSONDecodeError；识别真实格式后修正只读核查并全部通过。没有改写原工件来通过。

fault01 的核心有限定位有据：e11 的 checkout Charge code2 窗口增量 `11.297780195409754`、code0 为0；e18 的 PlaceOrder code13 同值、code0 为0；e8 实际显示 HTTP500 至 checkout PlaceOrder code13 的关联链。这不能证明隐藏触发机制。其完整报告仍错误写20/349，而实际 e8 `sampled_spans` 只有14、335省略；source query limit=20 不等于展示数。此错误同时进入 counter_evidence 和 gaps，已经足以不满足零未处置 P1/P2 判据。cart 跨 trace 与原始遥测缺失泛化沿既有逐项审查保留，本次未重新逐条核其原日志或全部原始 span。

normal03 有限正常观察有据：e2 checkout ERROR=0、UNSET=67.5；e8 Charge code0=5、code2=0；e9 checkout server code0=5、code13=0。它支持该窗未观察到 checkout 失败。完整报告仍有三类可亲自复现的错误：e11/e12 实际14/248、234省略，被 summary/claim/gaps 写为20；e2 accounting 仅有 UNSET=5，缺 ERROR 系列，被 claim5 写为 ERROR=0；e11 的 ef94… trace 显示 frontend gRPC PlaceOrder status0，而048623…只显示至 frontend API 路径，claim7却将 gRPC 显示事实同时扩展到后一 trace。原始文件中可能存在未交付 span 不能为模型可见性补证。正常对照不证明全环境健康或持续恢复。

## 当前代码、预算与机制边界

亲自重算[新离线源码 manifest](../m0-real-environment/round-02-initial-evidence-offline-source-manifest.json)列出的26项源码/schema/依赖及审查引用 SHA-256，全部匹配；有 snapshot 的项目逐字等于当前 source。包括 initial_evidence `3cbe4be7…`、bridge `765ecd5c…`、outcomes_v4 `880b924b…`、runtime `db7f76db…`、Scenario v4 `3de162f3…`、step_store `9e865752…`。原入口核查中的旧源码 hash 是上一稳定候选的历史范围，当前候选应导航此新 manifest，不能用旧 hash 声称覆盖本次新增代码。

初始证据终审实际运行94项及固定 Holmes 假传输探针、schema导出等值和独立反例；本次核其稳定 hash 后采用其有界独立证据，不重复声称自己执行这些测试。单一兼容 ProjectionContext、原字节/投影/实际 user 输入绑定、来源缺失保留 unknown、合法单步 report-only、upstream/tool版本及全文保真仍是明确约束；混合 context 不支持，缺真实时间依据不得补成 fresh。结构化合同不认证自然语言因果与数量正确。

当前 final 指针复验亲跑26项真实 PG 检查与额外合成探针，其稳定源码 hash 仍匹配当前文件；证据为成功 cancel/correct 同事务清当前 final、历史 report 不变、旧 fence 拒绝、新 Run 合法继续。它不是既有行迁移，也没有新模型证据。本次没有连接或起停 PG/Colima，没有复跑这些测试。

亲自按[最终用量](round-02-final-usage.json)逐 Run 重算：20模型 HTTP，输入360798、输出39606，已知400404 tokens；2次 usage 缺失，unknown 预留6.88128 CNY。原 `m0-02-request-ledger.json` 文件 hash 与汇总 `97a0fd38…` 相等。1.438848 CNY 是已知用量按峰值全 miss 的估计上界，实际可归属账单为 null；旧24 CNY与未知预留不释放，不把缺失记零。0 trace、62 Holmes 查询与1 fixture读取按原运行记录披露，本次未访问供应商核账。此次审查新增模型/trace/后端请求均为0。

共享宿主、worktree及新上下文不证明 OS 隔离。旧容器探针不证明实际 Holmes 宿主进程的隔离；原业务 raw/view 保留不等于未查询的易失历史也完整保存。PG 两个正常退出 stage 的协议重建与 Holmes 主动 Run 是独立实验，不能拼接为端到端恢复通过。

## 具体未达条件与下一最小任务

1. 新严格合同/投影之后尚无真实候选质量证据；本轮两份完整报告有上述未消除 P2，旧 v1 缺目标/时间声明不能补写成当前 v4 合格。
2. 尚未执行当前固定候选要求的正常、故障各2个独立 Run及逐份全文评审；必须固定版本、真实来源时间依据、预算、环境与窗口，不能用不同阶段调参后的旧 Run 充当重复。
3. 真实 Holmes 与 PG 当前业务控制/恢复仍未整体集成证明；后续首片只能在相应门槛与有界合同下推进，完整产品、72小时 soak与其他生命周期要求不在本轮删减。

下一项保持 M0-03：在当前已审离线候选基础上，先固定真实源的时间资格和候选版本，准备正常/故障各2 Run、最多16模型 HTTP的具体新费用与绝对截止合同；获新授权后才执行，按实际交付全文评审并做必要机制非退化核查，再重评 M1 入口。当前20次额度已尽。无需重审未改变的 C3，也无需为当前交付扩建平台。当前 PR 本身先完成最新 CI 与已触发安全/代码审查闭环，产品 passes 和 SPEC 门槛保持不变。
