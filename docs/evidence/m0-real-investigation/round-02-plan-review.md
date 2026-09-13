# M0-02 实施前独立方案审查

日期：2026-09-10。审查者未参与候选方案编写；先独立读取 SPEC、C3 §5–7/12、首片入口计划、现有 wrapper 和固定 Holmes 源码，再审两个候选实文。仅本地只读与 GitHub 只读核验，没有模型、trace、余额请求，未读取 .env 或真实 provider reasoning 正文。

## 结论与边界

**两方案可进入本地实现；未发现需改架构的阻断项。真实付费执行仍须完成下列执行合同冻结与实现验证。** 本结论不代表实现通过、真实调查通过、费用已核或 SPEC 产品实施门槛开放。候选文件为本目录 round-02-mechanism-plan.md 及环境 worktree 的 docs/evidence/m0-real-environment/round-02-report-plan.md；主授权为 round-02-authorization.json。

机制方案明确 v3 与 v2 隔离、Subject/AccessScope/实际来源分开、raw/view/实际输入快照可见性、最终可信人控、PG 原子步骤与预留、同 provider/Run 私有协议、current Run/owner/epoch/lease 采纳权，符合 C3 必要接缝。真实 PG 与模型替身可以先实现，真实模型组合另行报告。

报告方案根据已有失败同时调整输出、信封、超时与预算，明确禁止隐式 compaction 和私有字段丢弃，保留原上游循环最后一步报告槽；采用完整供应商输入上界预留，对未知 tokenizer 保持保守。方案没有承诺参数一定产出报告，失败时停止依赖阶段，范围合理。

## 真实执行前条件与最小补充

1. **硬停止必须可验证。** 候选写明模型360秒、Run1800秒、工具30秒/累计240秒，但仍须在执行合同中写清实际监督者、绝对deadline取最小、终止后的有限清理宽限和强杀回退。工具 terminate 后不能无限 join；须有限 join→kill→有限 join，并测试子进程仍不退出时明确阻断后续调用。模型响应传输、响应bytes超限与Run退出也必须真正有界，不能仅依赖客户端IO timeout。此项阻断真实执行，不阻断本地实现。
2. **费用语义分开。** 新请求预留3.44064 CNY为供应商最大输入+32768输出的保守值；已验证usage只能将本轮预留转为已知峰值未命中费用上界并释放差额，不能标记为实际账单已核。旧24 CNY不受此算法影响。usage缺失/异常/中断保留全额，每个物理尝试递增计数。须验证本轮全局与phase原子守门、不能自行借款，以及响应后崩溃不重复释放。合计20CNY/20HTTP/5上传不变。
3. **trace费用仍需单独守门。** 候选分配0.5 CNY/5上传，但未给出每次保守收费或免费额度适用证据。实际上传前冻结该依据；否则本轮暂保持0上传即可继续模型与PG验证，不因已有LangSmith凭据推断可用免费额。
4. **精确参数名称。** 固定上游 llm.py get_context_window_size 与 input_context_window_limiter.py 明确131072为总context tokens，且比较 input估计+32768输出，因此本地估计输入空间为98304；512KiB是另一独立信封限制。须在工件分别记录，不能把131072写成已保证输入tokens上限。源码当前禁用compaction后会超限拒绝，可测试这个真实入口以证明没有额外压缩请求。
5. **竞态与恢复测试必须穿过边界。** 在检查→发起之间设置屏障并交错取消/纠正，确认共同串行化线性化后无新请求；分别测试错Run/owner/epoch/lease。PG断点至少包含真实跨进程退出重建，不能只在同进程对象中模拟。私有协议仅使用合成哨兵做确定性检查；实际续传由受限程序验证，不让评审读取正文。

以上为候选已有承诺的具体执行判据，不新增产品能力。若模型/工具硬终止、账本事务或交付日志无法实现，应停在局部失败而非标记整体通过。

## 接手前提附录

- 主仓库实际HEAD为 e9d22e9183601ae322f006f4a42d9dd2bbcbae84；检查时干净。PR15 head bc463701374dce1c5305d16f2df089f09f002156 已合并。最新Code无重大问题评论5607609355、Security无问题评论5607640019，均覆盖该head；合并main CI34425221461 success。最新无发现结果在issue comments，不能只查reviews数组。
- 环境原 holmes-private-artifact-manifest.json 的88项真实文件全部存在，size/SHA256全部匹配；closeout.json三份原停止命令文件hash全匹配。
- 原Holmes物理模型HTTP16次、均200；批次fixture另4次，共20模型HTTP、1trace上传。加旧Pro2次和首次Flash2次，所核这三轮累计24模型HTTP。不能将工具HTTP混入模型请求计数。
- 原旧账本2+2 CNY均unreconciled；本轮旧批次2+2+16 CNY关闭于请求上限。旧4+旧批次20合计24 CNY未核账占用，不是花费24。没有释放或请求新账单。
- 原Flash result-1/result-2业务结果、usage、attempts与公开flash-results对应字段一致；公开row仅省略approval_hash/contract_hash。两个私有原文件SHA256分别74a97e95e89666bd8ac80e8bbd9a997aaca4ff0ff76602548b3fd5d2f1246933、c84b62eb8dc1e6eb359d3228d6f6e9f55e14f546564a9234ac0b074613d17d10。
- 接手核查时colima default/m0-otel均Stopped，55431无监听；原PG_VERSION=17目录存在且无postmaster.pid，系统PG4391仍运行。未启动PG，本核查不是表级重新回读证明。
- tracked旧任务与results尚未补记PR15实际合并、最新双审查和main CI；父任务应在本轮状态变更中补记。保留旧过程文字的历史身份。

审查仅覆盖上述方案及所列源码/工件，不替代实现后的独立复验、确定性测试、真实正常/故障质量判断或最终入口决定。
