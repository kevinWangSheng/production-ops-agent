# M0-02 本轮结果与入口决定

日期：2026-09-10 UTC。**本轮取得真实正常/故障最终报告与真实 PG 跨进程组合，但完整报告事实质量仍未通过，M1 入口不打开。** 20 次模型 HTTP 已用尽，全部付费执行停止；原失败不改写，feature passes 未改。完整入口判断见 [独立整体审查](round-02-final-entry-v4-review.md)。

## 实际取得的结果

- PR #15 已在接手前合并为 e9d22e9，最新 bc46370 Code/Security Review 与 CI、合并主线 CI34425221461均核查完成，主工作区已快进同步。两个原实验 worktree 的数据库、账本、原始证据与归档保留。
- 保存故障证据的两次新 Run：第一次 HTTP200 后响应名称校验失败且返回 model/usage 未保留，不能倒推实际返回值；第二次保留了15104 completion tokens、stop及正文，证明32768输出空间可产生报告，但完整质量未过。它们不算主动调查或旧 Run 恢复。
- 官方实时 `/models` 与保留响应证实请求名/报告名接缝：出站保持 `deepseek-v4-flash` thinking enabled/high，当前响应 `deepseek-flash`；冻结两个 Flash 报告名的允许集并拒绝 Pro/其他。仅报告身份兼容，不证明不变权重。旧失败不回填通过。
- **真实主动故障报告收束并有限定位成功**：新 fault01 的4HTTP/19工具在固定真实故障窗得到 JSON 报告，核心 payment Charge(code2)→checkout(code13)→HTTP500 有据。独立核19份实际交付和raw/hash/refs；仍有 cart 跨trace误关联/样本泛化、实际14/349误写20/349、把投影省略误作原始遥测缺失等P2，完整质量FAIL。[故障独立结果](round-02-fault-outcome-review.md)。
- **真实正常报告有据核心结论但质量未过**：normal01最后只有DSML工具调用文本；normal02经明确末步JSON协议后仍混淆累计/窗口及日志可见数量；normal03最后3HTTP/12工具形成JSON，checkout本窗未观察到失败有据，但仍有14显示写20、accounting缺ERROR序列写0、跨trace扩展未显示gRPC路径三项P2。[normal03独立结果](round-02-normal03-outcome-review.md)。正常对照不等于全环境healthy，ad/recommendation背景异常保留。
- **真实 DeepSeek/PG 同Run重建组合通过**：新Run 5822fb34-c343-4085-aca5-6337ff2ad40d，两个进程PID39801→44306/epoch1→2，PG完整响应/工具观察和配对消息重建后真实续传，最终固定JSON通过。独立只做DB端布尔/安全业务检查，不读推理正文；原7表53行hash不变。[独立组合结果](round-02-pg-live-outcome-review.md)。它是两个stage正常退出后的真实组合，中途崩溃/取消用例另有真实PG+可控替身，不冒充真实Holmes Run整体恢复。

## PR 审查后的合同接续

本文件下面的v3结构通过是当时evaluator的历史结果。PR16后续发现完整summary/next_steps没有进入Outcome、事实性claim缺目标/时间约束以及最终控制一致性遗漏；[新上下文设计审查](round-02-pr16-full-report-design-review.md)已独立复现并批准有界修复设计。[v4/report-v2严格包](../../testing/first-investigation-v4-2026-09-10.md)已离线实现并经[联合独立终审](round-02-pr16-v4-final-review.md)冻结，旧schema、原全文和原投影保留。历史v1缺target/time字段时只能标unknown、不能通过当前strict入口，不替模型补造字段来维持旧结构PASS。该变化不提升任何真实报告质量，也没有新增模型/trace样本。

## 合同、机制和保留缺口

[首流程v3开发验收包](../../testing/first-investigation-v3-2026-09-10.md)已冻结schema、身份/动态交付/最终请求与人控、预算、正常与故障各2独立Run的后续候选评估判据、0未处置P1/P2及非退化规则。它在下一候选评估前冻结，本轮不冒称已执行或通过该包；旧v2不变。

PG有界机制覆盖接收幂等、ModelStep/ToolOperation提交前后、只补未完成工作、历史时间/配对、unknown费用/次数/期限不重置、取消发起竞态、父硬退出后的子进程迟发、Run/owner/epoch/lease拒绝和版本不兼容。独立发现的过期lease仍发起、取消被版本检查覆盖、无对应尝试也能提交、初始view漏校验等均修复复验。不是完整调度/升级平台或产品功能验收。

实际Holmes→v3桥接检查raw/view/最后physical request；fault19/19与normal03 12/12结构一致不代表自然语言事实正确。主动Holmes基线与PG恢复是两条实验，固定generation0不冒充已与PG当前人控整体集成。旧源码7fd732和22a96按hash保全，旧投影不使用新代码重签。

20HTTP耗尽后只做离线修复：trace-v3明确真实可见数量、raw/省略/限制与错误详情覆盖，按query.service通用选取；metric-v2明确缺series不等于零。原raw/view/报告保留，49项测试与独立真实raw重放通过，**没有新模型效果证明**。因此不能靠离线修复消除上述质量FAIL。[离线审查](round-02-offline-view-review.md)。

共享宿主/worktree不是OS隔离或盲测。旧container探针不能认证实际Holmes宿主进程；此运行边界明确保留，不为本轮另建OS平台。全部产品能力仍只读，无修复执行或发布门禁。

## 用量、版本和时间

[逐请求来源及逐Run统计](round-02-final-usage.json)：**20模型HTTP、0trace上传、62真实Holmes工具查询、1次固定fixture读取**；另有1次官方/models只读metadata请求。18次有usage，共输入360798/输出39606、已知总400404tokens；首次丢失usage和400请求均不能记零tokens。

已知usage的峰值全cache-miss费用上界合1.438848 CNY；两次未知各保留3.44064，合6.88128 CNY。旧24 CNY未核账占用不释放。这些是上界/预留，**不是可归属实际账单**，实际费用仍unknown。所有重试/失败计入本轮20上限，没有追加调用。

运行固定OTel Demo2.0.2/源码63649d6、Holmes5e983c17，26镜像digest与配置沿用；Flash thinking/high、context131072含32768输出、请求512KiB/响应2MiB、360秒请求/1800秒Run，工具30秒/累计240秒。Holmes专用依赖沿原锁（LiteLLM1.89.0/OpenAI2.44.0/HTTPX0.28.1）；PG组合用主CPython3.12.13/HTTPX2 2.12.0/psycopg3.3.3。实际代码/source snapshot与每Run配置留存；最新离线候选不混写为真实运行版本。

[活动计时](round-02-activity-timing.json)：模型HTTP总wall约265.62秒；固定观测窗共900秒，与编码/审查并行。VM启动约15.56秒、容器启动约17.89秒（输出文件时间边界）；下载/安装0。其余主要为源码/合同、编码调试、检查、独立审查与收尾；没有可靠独占CPU时间，不虚构百分比分布或告警SLA。

## 收尾与下一项

[资源收尾](round-02-closeout.json)：故障按原字节还原；26容器与专属VM停止，命名数据卷、旧PG目录与全部实验worktree保留，系统PG4391仍运行。已捕获业务query完整raw/view和失败保全；不声称未查询的Jaeger易失历史也完整保存。

下一项仅为 **M0-03：报告事实与实际交付证据的一致性复验**，先用本轮失败做离线约束，再在新的明确授权下执行冻结候选包。必要时用结构化事实引用/可信呈现减少数量、缺测与关联歧义，不再逐次抬token/HTTP常数。待实际质量条件通过才记录M1-01入口决定；M1-01仍是认证提交→实际查询→证据结论→跟进/取消→持久重建，本轮不提前实施。

PR交付及最新CI/Code/Security Review结果在当前任务收尾段补记；没有合并授权，不自动合并。
