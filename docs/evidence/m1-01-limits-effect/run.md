# M1-01 去预算上限效果测量：候选 `64cad64` 上 6 个真实 Run（对比 6f）

- 日期：2026-09-28 UTC 10:11–10:55（本机日志时区 03:11–03:55 PDT）。候选冻结于 `feature/m1-01-window-points` `64cad64`（docs/tasks/2026-09-28-m1-01-loop-limits.md：每 Run 模型请求上限 100、模型自行决定何时停止调用工具并作答、取消工具次数/累计时长上限、单次输出上限 65536、上下文上限 1,048,576、Run 总 wall 7200 秒仅作卡死兜底、提示词开场预算句换成一句停止规则、非最后一轮收到无工具调用回复即按 L1a 结束或立即强制最终报告一次）。6 个 Run 期间 `opspilot/` 零改动：`git diff 64cad64 HEAD -- opspilot/` 为空（HEAD 在运行期间移动到 `5e45009`，另一 worktree 内的队友提交，是纯测试文件改动，未触碰产品代码，6 位独立审查者各自在评审时重新核对过这一点）。
- 流程：完全沿用 6f 的 `run_case.sh`（问题原文、`target_id=m0-otel-20260909`、SSE/incident.html/window.json/ledger 导出流程一字不改，只换目录与日志名，见 `run_case.sh` 副本，本机路径已占位）、同一独立观察脚本 `scripts/otel_demo_observe.py`、同一账本导出 `../m1-01-v4-acceptance/extract_ledger.py`。`POST /intake/ui`（HTTP，Basic 认证）→ 常驻 `python -m opspilot.worker_main` → `InvestigationRunner`；web 先起、worker 后起（避开空库 install 竞争）；两者均以 `OPSPILOT_TOOL_PROFILE=otel-demo` 启动。`worker.log` 首行 `versions` = `prompt-replay-candidate-cc542dd15dd8` / `ctx-ctx-policy-v1-b4b4c61e0eec` / `otel-demo-44ed0d63b0d3`（`prompt_revision` 与 6f 不同，因为 L5 改了系统提示开场；`tool_schema_revision` 与 6f 相同，因为本次改动未涉及工具/视图）。模型 `deepseek-flash`；工具后端是本 worktree 独占的 PostgreSQL 55431（`tmp/m0-b/postgres`）+ colima `m0-otel` 锁定的 OTel Demo 2.0.2。
- 留出场景（productCatalogFailure、cartFailure）流程沿用 `docs/evidence/m1-01-replay-ablation/scripts/heldout_fault.py` 与其安全规则（原字节先备份、拒绝重复注入、恢复前核对快照、逐条时间线），未改 `scripts/m0_environment/development_fault.py`。四个核心窗口（normal-1/2、fault-1/2）互不重叠；两个留出窗口在核心窗口之后单独提交，互不重叠也不与核心窗口重叠。
- 前提：每个 Run 提交前用 `scripts/otel_demo_observe.py` 独立核实控制窗前提（≥2 条不同 checkout trace 且相关调用有正增量）或故障前提（≥2 条不同 checkout 失败 trace 与授权依赖的失败调用关联）。`paymentFailure`（100%）与 6f 相同、立即成立；`productCatalogFailure` 对 checkout 是间歇故障，30 秒轮询 5 次（~3 分钟）后窗内 2/11 失败 trace 成立；`cartFailure` 轮询 7 次（~3 分钟）后窗内 3/11+ 失败 trace 成立。三个故障均在恢复后独立观察确认 0 失败（`observe-after-restore.json`、`observe-after-pc-restore.json`、`observe-after-cart-restore.json`），故障钩子前后 SHA256 往返一致（`fault-timeline.jsonl`，payment/pc/cart 共 6 条记录）。
- 独立审查：每个 Run 一个全新上下文 Agent（Opus），给 v4 判据（`docs/testing/first-investigation-v4-2026-09-10.md`）、PRODUCT-CONSTRAINTS、ADR-0005、模型可见的 `REPORT_CONTRACT`/`RUN_COVERAGE_TEMPLATE`（`opspilot/investigation/reports.py`）与工具描述（`opspilot/tools/otel_demo.py`）、本次限制变更的行为合同（`docs/tasks/2026-09-28-m1-01-loop-limits.md`，明确指示"轮数少于 100 上限本身不是缺陷"）、该 Run 证据目录与 55431 只读 SQL，不给执行者的评估。审查者按 v4 判据给 P1/P2/P3，并按 6f 的分类口径（a 空状态/`not_recorded` 写成 status 0/OK/200；b 缺失序列写成 0；b-svc 按服务分组的缺失序列写零；c trace limit 与 span 行数混淆；d 不完整/截断视图计数错误；e 其他可观察数值/时间/范围/因果错误）逐类计数，另答上游口径（是否正确指出根因/正确判定无故障）。结论原文见各目录 `review.md`，未经执行者改动。
- 费用：DeepSeek 余额 27.89 → 27.33 CNY（`deepseek-balance-before.json`/`-after.json`；中途查询 27.42，见 `deepseek-balance-mid.json`，5 Run 后），6 个 Run 合计 **0.56 CNY**。token 见各 `ledger.json` 的 `usage_totals`；本包共 27 次模型请求（旧 4 请求上限下 6 Run 固定为 24 次，本包按需从 3–6 次不等）。
- 资源守卫：VM/PG 启动前与每个 Run 前查内存压力，观测区间 23%–47%，一次逼近 23%（cartFailure 注入后）随即回升，均在阈值内未停止；两个留出场景用前台短轮询（每次 ≤ 2 分钟一次状态检查）等待间歇故障，未依赖长时间静默后台等待。
- 安全：写盘后按 key 全文/末 12 位（不打印 key 本身，从 `.env` 读出后只取后缀比对）、工作台口令、`Bearer`/`Authorization`、`/Users/shenghuikevin`、scratchpad 路径全文扫描本目录：全部 0 命中。`run_case.sh` 中的本机路径已换占位符。

## 汇总表（6 Run）

| Run | 案例 | 事故 / Run | 观测窗（UTC） | 模型请求（轮） | 每轮工具数 | 工具合计 | 自行结束 | 强制最终 | 用时（提交→发布） | token（prompt/completion） | 报告状态 | P1/P2/P3 | P2 类 | 上游口径 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | normal-1 | `a78a963c`†/`dc197ba3` / `1224763b` | 10:08:17–10:13:17 | 5/100 | 4,8,6,3,0 | 21 | 是（round5 `final=false`,`stop`,0 tools） | 否 | 64 s | 144232/15205 | `completed`/`partial` | 0/4/1 | a1 b1 e2 | 正确判定无故障 |
| 2 | normal-2 | `842a3709`/`78380522` | 10:13:44–10:18:44 | 4/100 | 8,4,1,0 | 13 | 是 | 否 | 47 s | 127278/10775 | `completed`/`partial` | 0/3/4 | a1 e2 | 正确判定无故障 |
| 3 | fault-1 | `06c748a4`/`92386e29` | 10:16:24–10:21:24 | 6/100 | 3,4,4,3,2,0 | 16 | 是 | 否 | 83 s | 114880/18841 | `completed`/`partial` | 0/2/2 | e2 | 正确指出根因（payment Charge） |
| 4 | fault-2 | `eb9d2b90`/`6e23da7b` | 10:21:40–10:26:40 | 4/100 | 2,3,3,0 | 8 | 是 | 否 | 54 s | 51242/12366 | `completed`/`supported` | 0/1/3 | b1 | 正确指出根因（payment Charge） |
| 5 | pc-fault（留出） | `cbef6303`/`1a99de30` | 10:33:51–10:38:50 | 5/100 | 2,3,6,2,0 | 13 | 是 | 否 | 57 s | 112649/12760 | `completed`/`partial` | 0/6/3 | a3 e3 | 正确指出根因（product-catalog GetProduct） |
| 6 | cart-fault（留出） | `5a7a4cd0`/`9b53a56e` | 10:43:48–10:48:48 | 3/100 | 3,8,0 | 11 | 是 | 否 | 50 s | 55249/12312 | `completed`/`partial` | 0/5/2 | a2 e3 | 正确指出根因（cart EmptyCart/valkey） |

†事故 id†/Run id。合计：模型请求 27（4/4/4/4/4/4=24 之外的差值来自本包无固定预算，按需 3–6 次不等），工具 82 次，全部 6/6 发布（`execution=completed`、`handoff=false`）；**6/6 在无工具调用的一轮自行结束（`context.final=false` 且 `finish_reason=stop`），无一次触发 L1a 的立即强制最终报告，无一次逼近 100 请求上限**（最高 6/100）；`evidence_raw_hash_mismatch` 全部为空（raw sha256 重算一致）。

## 与 6f 的对比（同 4 类核心场景：normal-1/2、fault-1/2）

| | 6f（`f8fac31`，旧 4 请求预算） | 本包（`64cad64`，去预算，同 4 场景） |
|---|---|---|
| 模型请求 | 固定 4/4/4/4=16 | 按需 5/4/6/4=19 |
| 是否自行结束 | 否——4 次全部在预算耗尽的第 4 轮强制去工具收尾 | 是——4 次全部在模型自愿的非强制轮结束，0 次触发强制最终 |
| 工具合计 | 60（18/14/12/16） | 58（21/13/16/8） |
| P1/P2/P3 合计 | 0/14/16 | 0/10/10 |
| P2 类：a（空状态写成好状态） | 6 | 2 |
| P2 类：b（缺失序列写成 0） | 0 | 2 |
| P2 类：b-svc | 0 | 0 |
| P2 类：c（limit 与行数混淆） | 0 | 0 |
| P2 类：d（不完整视图计数错） | 0 | 0 |
| P2 类：e（其他数值/范围/因果错误） | 8 | 6 |
| 上游口径正确率 | 4/4 | 4/4 |

- **P2 总数下降 14→10（-29%）**，主要来自 a 类（6→2）与 e 类（8→6）；b 类从 0→2 是本包新出现的两例（normal-1 P2-2「无非零 gRPC 状态」被读成「全部 0」、fault-2 P2-1「其余依赖无 ERROR span」被当作「payment 是唯一有错误的依赖」），两例都是审查者按"缺失序列=unknown"这条现有合同判的，不是新规则、只是本包样本恰好各出现一次；4 样本下 a/b/e 的升降含审查者定级噪声（见 6f 自身「审查粒度变异」记录），不作强因果结论。
- **P3（措辞不改变含义）从 16→10**，方向与 P2 一致：报告整体更少出现表述模糊或过度概括。
- **上游口径不变，仍 4/4 正确**：去预算没有让模型在核心判断上变差，也没有变好——两次正常窗都正确判无故障、两次故障窗都正确定位 checkout→payment `Charge`，这与 6f 完全一致。
- **报告 gaps 中与"预算/未查询次数/请求上限"相关的条目：6f 与本包均为 0**——逐条读了 6f 与本包全部 8 份报告（本节 4 份 + 留出 2 份 + 6f 4 份）的 `gaps` 字段，双方从未出现"因预算/请求数耗尽而无法查询"这类措辞；两边的 gaps 全部是证据本身的局限（缺基线窗口、trace 采样截断、`no_data`/缺失序列、`parent_is_visible=false` 导致调用链不完整）。**这一项没有变化，不构成本次改动的证据**——旧的 4 请求预算本身没有让模型在报告文字里抱怨预算，去掉预算后也没有出现相应的措辞减少，两者都是 0，是一个未观察到差异的对比点，如实记录。
- **工具调用不再被压缩进固定 4 轮**：6f 每次都在第 4 轮（预算耗尽轮）被强制去工具收尾（`_STEPS_SLOT`/`FINAL_REPORT_INSTRUCTION`），模型看不到工具定义只能作答；本包 4/4 场景都是模型在完整看到工具定义的一轮里自己选择不再调用工具（round 的 `context.final=false`、`tool_calls=[]`、`finish_reason=stop`），命中的正是 L1a 新增的路径。这是本次改动机制层面的直接效果，独立于 P2/P3 数值升降。

## 留出场景（productCatalogFailure、cartFailure）——首次在无预算脚手架下真实验证

- 这两个场景不在 6f 范围内，无法与 6f 直接比较；`docs/evidence/m1-01-replay-ablation` 阶段 B 曾对这两个故障做过重放消融实验，但那次方法不同——**只重放已固定上下文的最后一步**（10 次重放共享同一个已提交的调查历史，测的是最终作答步骤在不同提示词变体下的行为），不是本包这种从提交到调查到作答的完整真实 Run；阶段 B 基线组（组 0）在这两个留出故障上 a=21 e=46（2 Run×10 次重放，即人均 a=1.05 e=2.3/次），与本包（pc-fault a=3 e=3、cart-fault a=2 e=3，两次真实 Run 各一次）在方法论上不是同一个分母，不做数值直接比较，只记录口径差异。
- 两次都是本候选第一次真实面对训练/调优时从未用过的故障类型：`productCatalogFailure`（product-catalog `GetProduct` 对单一商品报错，checkout 上是间歇故障，2/11 trace 失败）与 `cartFailure`（cart 连不上 valkey，`EmptyCart` 失败）。**两次都正确指出了真实根因**（product-catalog `GetProduct` / cart `EmptyCart` 连接 valkey 失败），且都没有把间歇故障过度概括成全量故障（pc-fault 报告如实写 ERROR 2.5 对 UNSET 136.25 的比例，而不是笼统说"product-catalog 全部失败"）。
- P2 数值（pc-fault 6、cart-fault 5）高于核心 4 场景的均值（2.5），P2 类型集中在 a（空状态写成好状态，3+2=5 例）与 e（其他数值/范围错误，3+3=6 例），无 b/c/d 类。两位审查者都指出至少一例可能与产品侧工具描述有关但未确认成因：`parent_is_visible` 只在单个视图内部计算、跨视图不传递，工具描述未说明这一点，可能促成了模型把"该视图内看不到父级"误读为"该 span 完全没有可见父级"（pc-fault P2-6、cart-fault P2-2，两位审查者独立给出同一个技术假设，未做进一步验证，留给产品侧判断是否值得补一句工具描述）。

## 未执行 / 未验证

- 未做暂停/挂起/取消下的执行器控制拒绝演示、多 worker、重启恢复（不在本包范围；有 PG 合同用例覆盖）。
- 独立审查者未能对每条 claim 的措辞做穷尽核对；各 `review.md` 列出了实际核对的条目。
- 供应商余额差含本机本时段的全部调用；另一个只读复验 Agent 与本 worker 共享同一 DeepSeek key，若它在本时段有真实调用会计入余额差——未做独立核实（团队沟通显示对方是"只读复验"，预期无新增真实模型调用，但未逐笔核对供应商账单区分调用方）。
- 4+2 样本（6 Run）无法建立统计显著性；P2 类别升降含审查者定级噪声，不能排除样本噪声主导观察到的差异。
- 两个留出场景各只有 1 次真实 Run，不能判断间歇故障（productCatalogFailure）在不同触发时机/失败比例下报告质量是否稳定。
- gaps 中的"预算相关措辞"这一项对比是 0 vs 0，本身不能证明"去预算改善了报告对证据边界的表述"，只能证明"两种预算机制下报告都不会在文字层面抱怨预算"，二者是不同的问题。

## 目录

`run_case.sh`（本机路径已占位）、`fault-timeline.jsonl`（payment/pc/cart 共 6 条）、`observe-after-restore.json`（payment）、`observe-after-pc-restore.json`、`observe-after-cart-restore.json`、`deepseek-balance-before.json`/`-mid.json`/`-after.json`；每个 Run 目录：`request.json`、`intake.json`、`window.json`、`observe-pre.json`（pc-fault/cart-fault 另有 `observe-pre-attempt1..N.json` 间歇故障轮询记录）、`sse.txt`、`events.jsonl`、`incident.html`、`worker-attempt.txt`、`ledger.json`、`report.json`、`review.md`。`web.log`/`worker.log`/`lab-up.log` 未单独复制到本目录（内容与 scratchpad 日志一致，worker 首行 revision 已在本文件顶部记录）。
