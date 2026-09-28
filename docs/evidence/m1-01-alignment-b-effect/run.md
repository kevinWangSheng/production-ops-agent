# M1-01 对齐上游第二批效果测量：候选 `e22e6c4` 上 6 个真实 Run（对比 6f、第一批 limits-effect）

- 日期：2026-09-28 UTC 12:58–13:49（本机日志时区 05:58–06:49 PDT）。候选冻结于 `feature/m1-01-window-points` `e22e6c4`（docs/tasks/2026-09-28-m1-01-upstream-alignment-b.md：B1 trace/视图体量对齐上游、B2 时间窗模型自选、B3 报告校验失败反馈重试一次、B4 压缩阈值 0.95、B5 重复调用检测、B6 上下文 1,000,000）。6 个 Run 期间 `opspilot/` 零改动：`git diff e22e6c4 HEAD -- opspilot/` 为空，HEAD 全程未动。
- **与前两包的关键差异（合同「审查后处置」第三条）**：问题文本把"for the authorized 300-second window"改成"for the last 5 minutes"，其余逐字不变；观测窗不再由工作台固定 300 秒，而是 runner 给一个 24 小时授权外框（默认提交时刻前 24h 至提交时刻），模型通过可选 `start`/`end` 工具参数在外框内自选实际查询窗（省略时钳制为 1 小时回看）。独立观察脚本仍按固定 300 秒采集（与前两包一致，作为不受模型窗口选择影响的对照记录）。
- Run 顺序：两个 normal 先跑，其后 fault-1/fault-2（paymentFailure），最后两个留出（pc-fault=productCatalogFailure、cart-fault=cartFailure）；每个故障恢复后独立观察确认 0 失败再进下一个（`observe-after-restore.json`/`-after-pc-restore.json`/`-after-cart-restore.json`）。故障钩子前后 SHA256 往返一致，`fault-timeline.jsonl` 按时间顺序共 6 条（payment 注入 13:03:39Z/恢复 13:16:48Z，productCatalog 注入 13:29:20Z/恢复 13:34:53Z，cart 注入 13:40:33Z/恢复 13:43:14Z）。
- 流程其余部分沿用第一批：`POST /intake/ui` → 常驻 `python -m opspilot.worker_main` → `InvestigationRunner`；web 先起 worker 后起，`OPSPILOT_TOOL_PROFILE=otel-demo`。`worker.log` 首行 `versions` = `prompt-replay-candidate-3507addfe1e6` / `ctx-ctx-policy-v1-a30e65ea52f4` / `otel-demo-c32944a57000`（三者均与第一批不同，因为 B1-B6 同时改了提示词、上下文策略与工具 schema）。
- **环境异常（工程侧，非产品/模型缺陷）**：fault-1/fault-2 期间（约 13:04–13:10Z）checkout 调用速率跌到接近 0 持续约 6 分钟，同期 frontend/load-generator 仍有流量；已排查非 OOM、非容器重启（`docker inspect` 确认零重启），判断为本机当日第二次拉起 colima m0-otel 造成的资源争用瞬时影响；两个 Run 的独立前提检查（precondition）等到流量恢复后才确认通过再提交，未在故障未确认的窗口提交；该异常已在 fault-1/fault-2 的 review.md 中记录，审查者确认未导致报告内容错误，只是证据略稀疏。
- 独立审查：每个 Run 一个全新上下文 Agent（Opus），给 v4 判据、PRODUCT-CONSTRAINTS、ADR-0005、模型可见的 `REPORT_CONTRACT`/`RUN_COVERAGE_TEMPLATE`/`REPORT_RETRY_TEMPLATE`、工具描述、B1-B6 合同背景、本 Run 证据目录与 55431 只读 SQL，不给执行者评估。除常规 v4 判据 + a/b/c/d/e 分类 + 上游口径外，本次新增**窗口污染核查**（要求审查者核对每条引用证据的真实查询窗是否落入了不属于本 Run 的其他故障事故区间，并明确写出"未发现污染"或具体发现），以及对 fault-2（触发了 B3 重试）要求审查者独立复算 round 8 报告是否真的该被拒、round 9 是否真的修对了问题。结论原文见各目录 `review.md`。
- 费用：DeepSeek 余额 27.32 → 25.86 CNY（`deepseek-balance-before.json`/`-after.json`；中途 26.18（4 Run 后）、25.99（5 Run 后），见 `deepseek-balance-mid.json`），6 个 Run 合计 **1.46 CNY**（第一批 0.56 CNY 的约 2.6 倍）。token 总量 prompt 3,499,044 / completion 121,417（第一批 605,530 / 82,259，约 5.8×/1.5×），主因见下方"资源体量对比"。
- 资源守卫：VM/PG 启动前与每 Run 前查内存压力，观测区间 26%–47%，均在阈值内；两个留出场景用前台短轮询（每次 ≤2 分钟一次状态检查）等待间歇/瞬时故障确认，未依赖长时间静默后台等待。
- 安全：写盘后按 key 全文/末 12 位、工作台口令、`Bearer`/`Authorization`、本机路径全文扫描本目录：全部 0 命中。

## 汇总表（6 Run）

| Run | 案例 | 观测窗（提交→发布，UTC） | 模型请求 | 每轮工具数 | 工具合计 | 自行结束 | B3 重试 | 用时 | token（prompt/completion） | 报告状态 | P1/P2/P3 | P2 类 | 上游口径 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | normal-1 | 12:58:03→12:59:09 | 5/100 | 2,3,3,2,0 | 10 | 是 | 否 | 66 s | 343013/13197 | `completed`/`partial` | 0/0/3 | 无 | 正确判定无故障 |
| 2 | normal-2 | 13:00:52→13:02:27 | 6/100 | 2,4,4,4,3,0 | 17 | 是 | 否 | 95 s | 536702/18864 | `completed`/`partial` | 0/1/2 | a1 | 正确判定无故障 |
| 3 | fault-1 | 13:11:26→13:12:25 | 4/100 | 4,4,4,0 | 12 | 是 | 否 | 59 s | 200524/12796 | `completed`/`partial` | 0/3/4 | a1 e2 | 正确指出根因（payment Charge） |
| 4 | fault-2 | 13:12:56→13:15:48 | 9/100 | 7,5,4,3,4,3,2,0,0 | 28 | **否（round 9 强制最终）** | **是（round8 引用无效被拒→round9 修复并发布）** | 172 s | 1736127/39008 | `completed`/`partial` | 0/4/3 | e4 | 正确指出根因（payment Charge） |
| 5 | pc-fault（留出） | 13:32:27→13:34:06 | 7/100 | 3,4,4,3,3,1,0 | 18 | 是 | 否 | 99 s | 478880/23998 | `completed`/`partial` | 0/1/4 | e1 | 正确指出根因（product-catalog GetProduct） |
| 6 | cart-fault（留出） | 13:41:39→13:42:43 | 4/100 | 2,3,2,0 | 7 | 是 | 否 | 64 s | 203798/13554 | `completed`/`partial` | 0/4/2 | b1 b-svc1 e2 | 正确指出根因（cart EmptyCart/valkey） |

合计：模型请求 35（第一批 27），工具 92（第一批 82），6/6 发布（`execution=completed`、`handoff=false`）；**5/6 在无工具调用的一轮自行结束**；fault-2 是全部 12 个 Run（本包 6 + 第一批 6）中唯一一次触发 B3/L1a 合并重试机制的 Run（round 8 产出报告但引用未交付证据被拒，收到诊断反馈后 round 9 重写并通过发布）；`RESULT_TOO_LARGE` 与 `DUPLICATE_TOOL_CALL`（B5 去重）在全部 6 个 Run 中出现 **0 次**；`evidence_raw_hash_mismatch` 全部为空。

## B2 实际查询窗分布（模型自选，非固定 300 秒）

每个 Run 的真实查询窗口（从 `ledger.json` 每条证据的 `view.window`/`tool_results[].result.window` 提取，不是问题文本里的"last 5 minutes"字面值）：

- **normal-1**：首轮用 5 分钟窗（12:53:03–12:58:03），round 2 起全部切换为工具默认的 1 小时钳制窗（11:58:03–12:58:03）并保持到底，未再收窄。
- **normal-2**：大多数轮次用 5 分钟窗（12:55:52–13:00:52），round 3 探了一次 30 分钟窗（12:30:52–13:00:52，`step=300s`，只返回终点一个值），round 4 显式探了两个更早的 5 分钟历史窗（12:45:52–12:50:52、12:35:52–12:40:52），均 `no_data`（环境 12:55:47Z 才起，之前确实无数据）。
- **fault-1**：全程只用当前 5 分钟窗（13:06:27–13:11:27），未探索历史。
- **fault-2**：探索最广的一次——round 1-2 用当前 5 分钟窗，round 3 先探了两个更早 5 分钟窗（`no_data`）后**尝试查询完整 24 小时外框**（12:07:56 前一天同一时刻至提交时刻）被 executor 拒绝（`status=error`，非法/超限），round 4-7 反复探索 12:07–13:10 之间多个 5 分钟/1 小时窗口寻找基线对比，其中 13:05:00–13:10:00 窗口显示 checkout 调用为 0（对应上方记录的环境流量异常段）。
- **pc-fault**：当前 5 分钟窗（13:27:27–13:32:27）为主，一次 12:22–12:27 历史窗 `no_data`。
- **cart-fault**：当前 5 分钟窗（13:36:39–13:41:39）为主，一次基线对比窗 13:31:39–13:36:39（与更早的 productCatalogFailure 事故有 3 分 14 秒重叠，见下方污染核查）。

**授权外框（`window.json` 记录的 `time_policies[0].window`）在 6 个 Run 中均为 24 小时**（提交时刻前 24h 至提交时刻），确认 B2「runner 决定外框、模型在外框内自选」的产品行为按合同落地；模型从未在任何 Run 中把 5 分钟窗当作硬性上限，而是按需扩展查找基线，这是 B2 想要的行为，不是缺陷。

## 窗口污染核查（团队要求，6/6 结果）

逐 Run 独立审查者的结论：**6/6 均为"未发现污染"**——没有一次把不属于本 Run 的其他故障事故的证据当作本 Run 结论的支撑。唯一的边界情况：

- **cart-fault 的基线对比窗**（13:31:39–13:36:39Z）与更早的 `productCatalogFailure` 事故（13:29:20–13:34:53Z）重叠约 3 分 14 秒，该基线窗内确实混入了尚未清空的 product-catalog GetProduct 失败序列（审查者用只读 Prometheus 查询验证）。报告把该窗仅当作"紧邻前一个 5 分钟"的延迟基线使用，未把这些残留错误归因于 cart，也未在报告中说明该基线窗并不干净——审查者记为 P3（表述问题，未产生错误归因）。
- **pc-fault** 有两条证据的窗口落入更早的 `paymentFailure` 事故区间，报告明确把这两条证据标注为"更早、独立的事故"，未与 product-catalog 混淆。
- 正常两个 Run（normal-1、normal-2）提交时环境当天尚无任何故障（payment 故障 13:03:39Z 才注入，两个 normal Run 分别在 12:58:03Z、13:00:52Z 提交），审查者确认全部证据窗口都早于任何故障注入时刻，不存在"看到残留故障误报"的情况。

## fault-2 的 B3 重试机制复核

fault-2 是本次唯一触发 B3（报告校验失败反馈重试，与第一批 L1a 合并为同一机制）的 Run。独立审查者独立复算了产品自己的 `unsupported_citations` 校验：

- **round 8 确实该拒**：其中一条 fact 类 claim 引用了一个 `status=no_data`、`citable_as_fact=false` 的视图，违反 `REPORT_CONTRACT`；其余全部 claim 校验通过。
- **round 9 修对了那一条**：把该 email 相关陈述移入 gaps，重新校验 `unsupported_citations=False`，成功发布。
- **但 round 9 同时引入了两个新的 P2**（详见 fault-2/review.md 的 P2-1、P2-3）：一条关于"部分失败而非全部失败"的反例解读有误，一条证据引用从 round 8 的正确视图（t2）退化成 round 9 的错误视图（t0/t1）。净效果：修复了会导致发布失败的硬伤，但引入了不影响发布资格、只影响内容质量的新错误——B3 机制本身工作正常（阻止了不合格报告发布），但重写过程不保证不引入新的内容错误，这是该机制设计上的已知局限，不是本次实现的回归。
- 审查者额外指出：本次发给模型的重试反馈信息只给了固定原因码（"REPORT_INVALID … citing only evidence_id values already delivered"），未具体点出"引用的是一个 no_data 视图"这一具体错误——B3 合同文本要求"哪条 claim、哪个 evidence_id、何种错误"，`docs/tasks/2026-09-28-m1-01-upstream-alignment-b.md` 的实现判断点已记录这是有意为之的简化（只对"引用未交付 evidence_id"给具体值，其余失败原因只给固定原因码），审查者认为这是留给 lead 的合同取舍点，不是缺陷。

## 与 6f、第一批（limits-effect）的三包对比（同 4 类核心场景：normal-1/2、fault-1/2）

| | 6f（`f8fac31`，固定 300s 窗+旧预算） | 第一批（`64cad64`，去预算，仍固定 300s 窗） | 本批（`e22e6c4`，去预算+模型自选窗+B1-B6） |
|---|---|---|---|
| 问题文本 | "for the authorized 300-second window" | 同 6f | **"for the last 5 minutes"** |
| P1/P2/P3 合计 | 0/14/16 | 0/10/10 | 0/8/12 |
| P2 类：a | 6 | 2 | 2 |
| P2 类：b | 0 | 2 | 0 |
| P2 类：b-svc | 0 | 0 | 0 |
| P2 类：c | 0 | 0 | 0 |
| P2 类：d | 0 | 0 | 0 |
| P2 类：e | 8 | 6 | 6 |
| 上游口径 | 4/4 | 4/4 | 4/4 |
| 触发格式修复/校验重试 | 0 | 0 | 0 |

**核心 4 场景的 P2 合计三包连续下降：14 → 10 → 8**，其中 b 类（缺失序列写成 0）从第一批的 2 回落到 0，其余类别与第一批基本持平；上游口径三包均 4/4，未因去预算或模型自选窗口而下降。

## 六 Run 全量对比（第一批 vs 本批，各含 2 个留出场景）

| | 第一批（6 Run） | 本批（6 Run） |
|---|---|---|
| P1/P2/P3 合计 | 0/21/15 | 0/13/18 |
| P2 类：a | 7 | 2 |
| P2 类：b | 2 | 1 |
| P2 类：b-svc | 0 | 1 |
| P2 类：e | 12 | 9 |
| 上游口径 | 6/6 | 6/6 |
| token（prompt/completion） | 605,530 / 82,259 | 3,499,044 / 121,417 |
| 费用 | 0.56 CNY | 1.46 CNY |

**六 Run 全量 P2 合计从 21 降到 13（-38%）**，a 类降幅最大（7→2）；P3 从 15 升到 18（三个 Run 的措辞类发现增多，与本包审查者对"表述是否精确对应实际查询窗"的核查更细有关，可能含审查粒度差异，非确证的质量下降）。

## 资源体量对比（新发现，第一批未观察到）

**本批 prompt token 是第一批的约 5.8 倍（3,499,044 vs 605,530），费用约 2.6 倍。** 主因是 B1（视图字节上限从 ~16 KiB 放宽到 ~100 KiB）与 B4（压缩阈值 0.8→0.95，上下文长到接近整个 1,000,000 token 预算才触发压缩）叠加：视图本身更大、且更晚被压缩，两者共同推高每轮的累计上下文体量。fault-2 一个 Run 就用了 1,736,127 prompt token（占本批总量一半），因为它触发了最多轮的历史窗口探索（7 轮工具调用 + 1 次 B3 重试）。**本次 6 个 Run 没有一次触发 `RESULT_TOO_LARGE` 或上下文耗尽**，说明当前视图体量与压缩阈值组合在真实 Run 规模下仍有余量，但费用/token 增长本身是一个需要 lead 关注的真实效果，不只是"验收通过与否"能反映的维度。

## 产品侧发现：提示词与工具 schema 不一致（pc-fault 独立审查发现，cart-fault 审查复核确认未造成实际影响）

`opspilot/instructions/discipline.py` 第 82/127/172 行，`replay-candidate` 变体（本包实际使用的变体）仍包含 `FIXED_WINDOW` 段落原文："The authorized query window is fixed by the trusted runner; do not supply start/end tool parameters."——这与 B2 的实际工具 schema（`start`/`end` 现在是合法可选参数）直接矛盾。**实测 6 个 Run 中模型均无视这句提示、正常传了 `start`/`end` 参数**（见上方"B2 实际查询窗分布"，每个 Run 都有非默认窗口的显式查询），未造成任何一次报告错误或空白查询，但这是一处未同步更新的模型可见文本，且按仓库惯例 `prompt_revision` 应随模型可见文本变化而变化——若后续要求"提示词与工具描述互相一致"作为合同项，需要 lead 决定是否要求 batch B 补一次修订（删除或改写这句话）。本次未发现它造成任何可观察后果，按规则只记录、不在本 PR 修。

## 未执行 / 未验证

- 未做暂停/挂起/取消下的执行器控制拒绝演示、多 worker、重启恢复（不在本包范围；有 PG 合同用例覆盖）。
- 独立审查者未能对每条 claim 的措辞做穷尽核对；各 `review.md` 列出了实际核对的条目。
- 供应商余额差含本机本时段的全部调用；未做独立核实是否有其他并发调用方。
- 6+6+4=16 个真实 Run（跨三包）无法建立统计显著性；P2 类别升降含审查者定级噪声。
- 环境流量异常（fault-1/fault-2 期间约 6 分钟 checkout 调用趋零）未深入排查根因，只确认非容器崩溃/OOM，归因为本机资源争用；若未来复现且更严重，需要单独排查。
- token/费用增长的确切归因（B1 视图体量 vs B4 压缩阈值 各自贡献多少）未做消融实验，本文档只报告合并后的整体效果。

## 目录

`run_case.sh`（本机路径已占位）、`fault-timeline.jsonl`（payment/pc/cart 共 6 条）、`observe-after-restore.json`（payment）、`observe-after-pc-restore.json`、`observe-after-cart-restore.json`、`deepseek-balance-before.json`/`-mid.json`/`-after.json`；每个 Run 目录：`request.json`、`intake.json`、`window.json`、`observe-pre.json`（pc-fault/cart-fault 另有 `observe-pre-attempt1..N.json` 间歇故障轮询记录）、`sse.txt`、`events.jsonl`、`incident.html`、`worker-attempt.txt`、`ledger.json`、`report.json`、`review.md`、`stats.json`（本包新增：从 `ledger.json` 提取的每轮工具数、查询窗、`RESULT_TOO_LARGE`/去重计数、token 用量，可按 `ledger.json` 复算）。

> 记录格式说明（2026-09-28）：各 Run 目录 `request.json` 中提交时使用的幂等键字段在本证据中记为 `idempotency_label`（值与提交时及数据库记录逐字相同）。原字段名 `idempotency_key` 与值 `alignb-pc-fault-1` 组合触发仓库密钥扫描（gitleaks generic-api-key）误报；扫描器按项目策略禁用一切豁免，故只改记录字段名，不改值。
