# 评测 PR-C：live kind/OTel 场景驱动合同（r1）

- 状态：r1，2026-10-18 裁决。U1–U3 与 A1–A11 均按推荐采纳（按用户 2026-10-17 常设指示「决策的部分先按推荐的走,如果涉及到重大的改动在找我」；三项均为收紧或实验侧协议，不放宽验收、不扩权限、不改 PRODUCT-CONSTRAINTS）。U1 阈值为提案，首次真实生命周期探测后按实测值修订并记入本文；用户可推翻任一项。
- 核查：2026-10-17，全新上下文预审者，未参与实现；分支 `feature/eval-c-live-driver`，工作区 `/Users/shenghuikevin/dev/AI/poa-eval-c`，HEAD 与本地 `origin/main` 均为 `65b0b979642186d08ac780219f03b8796860b433`，开始时工作区干净。按本次只读范围未 fetch、清理 worktree 或更新 ROADMAP。
- 依据：下文 S = [套件合同 r8](2026-10-12-eval-suite-contract.md)，B2 = [录制接口](2026-10-14-eval-pr-b2-interface.md)，E = [B2 真实证据](../evidence/eval-b2-live/README.md)。代码短名 `runner.py/limits.py` 位于 `opspilot/investigation/`，`otel_demo.py` 位于 `opspilot/tools/`，`recording.py/replay.py/storage.py/runs.py` 位于 `opspilot/evaluation/`。用户给出的 `2026-10-14-eval-pr-a-interface.md` 不存在；实际接口是 [2026-10-13 版本](2026-10-13-eval-pr-a-interface.md)。
- 边界：只新增本文；已读取 `gh issue view 234`。未执行 kind/kubectl、端口探测、PG、模型或 trace 调用，未读 `.env`。部署可达性、flagd 方法/实际传播延迟和当前空闲状态均**未确认**。

## 需用户裁决

| 编号 | 问题（代码/合同证据） | 选项与代价 | 推荐 | 影响面 | 裁决 |
|---|---|---|---|---|---|
| U1 | 独立观察何时允许调查？S:32 与 C3 `docs/design/technical-proposal-2026-09-07.md:396` 要求先判断可诊断；E:19–21 只证明曾等错误 rate>0，未定义六例门槛；场景刺激固定最近 5 分钟（`evals/scenarios/eval-cart-failure/scenario.json:12–15`）。 | A 固定 sleep，便宜但可能采到旧故障；B 先核对 flagd，再查独立遥测并隔离旧窗口，增加等待与 setup 失败。 | **B**：建议每阶段最多 600s、5s 轮询、单探测≤5s；用 UTC 记请求前后与遥测时间、monotonic 计超时，不容许跨边界时间容差。故障：确认目标 flag 后，取得其后且在调查窗内的 ≥1 条 `answer.signature` 错误 span，并有 checkout 流量；正常：全部故障关闭后取得完整 300s 新窗口、有 checkout 流量、三类签名零命中，数据缺失/截断不算健康。独立观察原文只存评测侧；Run 授权框沿 D12，不缩成固定 300s。 | **needs user：新增 setup 合格门槛/时间合同**；阈值是提案，未实测。正常窗口仅证实该有限目录，无全系统健康承诺。 | 采纳 B（2026-10-18，常设指示） |
| U2 | 中途失败如何恢复与认定 flag off？`scripts/kind_lab.py:1033–1051,1080–1082` 只在注入回读成功后存快照，且重复 restore 会拒绝；`scripts/f11_u2_harness.py:1851–1857,1895–1940` 区分 daemon 生效与应用生效。 | A 仅 finally 调旧 restore，代码少但写成功/回读失败会无恢复依据；B 写前持久化恢复意图、状态核验后幂等恢复，增加日志与崩溃路径。 | **B**：写前保存原文/预期注入 SHA、对象身份与版本、owner/attempt；正常、异常、超时和可捕获退出均进入恢复。当前值=注入快照才以版本条件恢复，=原快照则验证后成功，其他值停止覆盖。ConfigMap 原 SHA、flagd off 均通过后按 U1 洗净窗口；硬终止不承诺 finally，保留故障占用供显式恢复。传播/洗净各≤600s，失败停止后续场景、保留占用，禁止强制回滚他人变化。 | **needs user：实验恢复状态合同**，不授权产品修复；不会改 PRODUCT-CONSTRAINTS。 | 采纳 B（2026-10-18，常设指示） |
| U3 | 调查成功、restore 或录制失败时整次是否成功？`opspilot/evaluation/model.py:151–162` 没有 cleanup 状态；`runs.py:212–217` 对非 ran 记失败；S 的 P10、D13 要求失败不丢分母。 | A 只报产品 Outcome 成功，清理另列，可能掩盖驱动未完成；B attempt 失败、保留原 Outcome，增加工程失败分类说明。 | **B**：setup 前登记 started attempt；无产品 Run 也占该槽。仅调查结束且录制/恢复收尾完整时标 ran；setup 未成用 setup_failed，调查后收尾失败用 aborted，阶段/原因进私有 ledger 和公开脱敏摘要，不伪造产品 handoff。恢复失败终止整轮，其余未开始槽单列；失败不重抽替换。 | **needs user：验收/计分语义收紧**；不增加枚举、不改产品最终状态，须让 loader 重算与展示一致。 | 采纳 B（2026-10-18，常设指示） |

## 已定约束与可逆默认

以下 A 项为实现默认建议；有条件分支超出既定权限、修改验收或 PRODUCT-CONSTRAINTS 时仍须用户决定。

| 编号 | 问题（真实依据） | 选项与代价 | 推荐 | 影响面 | 裁决 |
|---|---|---|---|---|---|
| A1 | 入口与代码位置：`scripts/m1_live_runner.py:254–258` 默认 2 请求/12min；E:17 使用忽略目录驱动；S:238 指出无正式 adapter。 | A 扩旧演示，容易继承 fixture 默认；B 独立脚本组合已有产品与 B2。 | B：`scripts/eval_live.py --mode live --round … --scenario … --n … --parallel 1`，显式 live/round/N，支持单例或冻结六例；PG/占用/注入/恢复在 scripts，纯录制/评分仍在 `opspilot/evaluation`。不在产品 worker 中启动 shell。 | 可逆模块组织；S D3/D4、PR-D 显式入口已定。 | 采纳 B |
| A2 | 两个留出故障是否已支持？`scripts/kind_lab.py:101–104,992–1007,1137–1140` 仍硬编码 payment，无 flag 参数；两个 `evals/scenarios/eval-{product-catalog,cart}-failure/answer.json:19–20` 均要求 on。 | A 在驱动直接 patch，复制安全逻辑；B 扩已有工程故障入口。 | B：白名单 paymentFailure→100%、productCatalogFailure/cartFailure→on；原命令默认 payment 保持兼容；读取实际 variants 并拒绝不存在的配置，恢复用原快照。normal 不注入。 | D1/D3 已授权三类；不增加任意 flag/命令或产品工具。 | 采纳 B |
| A3 | 与其他线的占用互通：`scripts/f11_u2_harness.py:78–91,140–152,265–325,2010–2013` 有公共目录、occupation、active 与 fault guard。 | A worktree 私锁，互不排斥；B 同协议，需处理遗留占用。 | B：以 git-common-dir 定位主仓库 `tmp/lab-ledgers`；沿 holder/batch claim/check/release、`.active-*`、`.fault.guard` 与 `fault-active.json` 协议，故障状态须可被既有恢复逻辑识别，不覆写异主记录；锁顺序沿现有代码、避免嵌套重拿。D15 的会话空闲确认仍要做，过期不偷占；全流程串行，正常观察也占用，恢复失败不释放。 | D4/D15、R5 已定；不新造第二套锁。 | 采纳 B |
| A4 | 端口/PG 复用：`scripts/kind_lab/kind-config.yaml:13–24` 固定 18080/19090/16686；E:18 用专属 55497，U2 记录 `2026-10-14-f11-u2-calibration.md:42` 用 55561。 | A 自动抢端口/连现有库，易污染；B 已有 lab 身份核验＋独立临时 PG。 | B：先核对既有 kind/端点身份；这些端口由预期 lab 占用是正常，未知监听者或身份不明 fail-closed。PG 显式新目录/端口，监听冲突或目录非本 attempt 所有即失败，不换端口掩盖冲突、不杀进程；仅停止自己启动的实例。迁移只对新隔离库，owner 仅 setup，运行沿 web/worker 限权角色（`opspilot/schema.py:6,70–79`）。 | R5 已定；不复用共享库、不扩角色权限。 | 采纳 B |
| A5 | B2 接线缺口：`runner.py:103–115,344–346` 无 delivery 参数；`otel_demo.py:1585–1616` 私有 DTO 构造 scope；B2:123–134 要求 record 前冻结。 | A monkeypatch/预测 epoch，脆弱；B 公开只读 DTO 与最小注入接缝，有少量产品代码变更。 | B：Runner 透传默认 None 的 `capture_delivery`；公开与 executor 同源的无凭据注册 DTO。按真实 lease/input 准备 executor/scope，在任何模型/工具调用前 bind snapshots、取返回 manifest、进入 record；不能在已开始的 record 中补快照或硬编码 epoch=1。接缝允许分离准备/执行，但不得另行领取或绕开 fence。 | 可逆产品接线；默认 None 不改控制/授权/报告行为，须独立审查。扩大权限或改变领取语义则 **needs user**。 | 采纳 B |
| A6 | 录制内容与答案隔离：B2:52–56,98；`recording.py:826–836` 入口锁定配置；PR-A:22–32 分开 Stimulus/Answer。 | A 只包工具或事后补录，缺物理调用；B 四个挂点完整透传。 | B：`hooks.model/tools`、`DeepSeekClient.capture_http=hooks.http`、Runner delivery；原始业务输入用 hooks.input，结束调用 finish。每个真实 model 事件恰有一个 http 子事件；raw headers/凭据/注入答案不进模型、trace、judge。snapshot/hash/UTC 窗绑定真实 scope；预观察不是调查已交付证据。 | B2 已定，无新出口；PRODUCT-CONSTRAINTS:16,32,38,48 保持。 | 采纳 B |
| A7 | #234 两项：`recording.py:1278–1297` 按 view hash 扩证据集合；`replay.py:441–445` 返回可变 session.id_map。 | A 仅约束可信 driver，漏洞仍在；B 核验层派生身份，需针对性回归。 | B：交付从经验证 parent 的 tool/refusal 推出具体证据，仍要求实际后续 HTTP 模型消息交付（`recording.py:1067–1077`）；无 parent 缓存只能用可核验唯一身份，歧义拒绝。id_map 从重新验证的新录制 replay_source 派生，不信 session 公共 map。测同字节不同 evidence ID、缓存/拒绝、driver 改 map 的反例及合法对照。 | issue 已明确纳入 PR-C；旧包不改 hash，无法证实身份的包拒绝新验证，历史摘要只读。 | 采纳 B |
| A8 | 输出 schema：`storage.py:85–104,424–438` 已有严格 eval summary；`scripts/lab_evidence.py:250–293` 是 lab-summary，不能混写。 | A 沿旧 freeze 任意加字段，不能经 B1 loader；B 复用 B1 DTO，工程生命周期另存。 | B：每 attempt `summary.json` 用 `opspilot-eval-summary-1`/EvaluationCapture，含 Outcome、manifest/versions、usage/cost、trace.entries、recording/ledger refs；生命周期日志在 ignored ledger，公开脱敏索引另文件。复用 lab_evidence 的 project/read-back 能力，不用旧 freeze 覆盖 eval schema。 | D7/D11、B2:43,71 已定；schema 不另起一套。 | 采纳 B |
| A9 | trace、存储与提交：E:9、B2:43,71；`storage.py:383–393` 禁止源 trace 冒充当前。 | A 提交原录制便于复现，但泄露风险；B 私有原件＋公开摘要/hash。 | B：原 ledger 放 ignored `tmp/lab-ledgers/<round>/<attempt>`，录制固定 worktree `tmp/eval/<recording-id>`、私有权限；共享占用仍按 A3。只提交脱敏 summary、版本/哈希及证据说明，原始 HTTP/ledger/凭据不入库；摘要含 Answer 的既有公开字段须核对，不追加私有实验答案。每轮 lab project、longlived、真实 trace URL/read-back，缺失如实记失败；外置原件丢失不得称可复核通过。 | D11/P13 已定；旧 lab summary 只读兼容。 | 采纳 B |
| A10 | 上限和费用：`limits.py:22–26,87–93`；旧脚本限额见 A1。 | A 继承短预算，改变结论；B 产品冻结默认。 | B：每 Run 100 模型请求、单次 360s、7200s stuck-run backstop，8MiB 请求/2MiB 响应及现有工具大小保护；不用额外工具累计/套件费用/套件墙钟上限。setup/restore 超时按 U1/U2 单独记，不吞 Run 额度；费用按供应商前后余额差，未知/他线污染明确标注。 | D10 已定；7200s 不是第二个循环次数预算。 | 采纳 B |
| A11 | PR-C 要跑多少？S:230 至少 1 次；D5 live=2 是六场景基线共 12 次；E 的 3 次是旧临时驱动。 | A 本 PR 跑完整基线，越到 PR-D/M3；B 按驱动验收，基线另行执行。 | B：最低 **1 次新正式驱动真实故障 Run** 完成注入→独立观察→真实调查→restore，带 trace/read-back、冻结摘要；启用 B2 完整录制并零模型结构回放。三种注入、正常洗净、冲突与中途失败须有独立合同测试；另建议三类故障各一次真实生命周期探测（可不调模型，不冒充 Run），新增两个 flag 未实测须明确披露。完整基线仍 N=2×6=12，失败保留；本 PR 不要求质量全过、不翻 passes。 | 既有最低验收不削弱；结构回放验证新增接线；不把“至少一次”说成三类现场均证明。 | 采纳 B，含三类故障各一次生命周期探测 |

## 交接与未确认项

主仓库 `tmp/lab-ledgers/occupation.json`、`fault-active.json` 本次只读检查均不存在；这不是占用许可。F11 的协议要求恢复后才释放，`ROADMAP.md:20–25` 还有 F2/F3 实验排期与基线归 M3 的约束。PR-C 保持驱动范围，不启动完整基线。

九类预审覆盖：时间 U1/A10；计数 U3/A11；状态 U2/U3；主体权限 A4–A6；数据与部署 A5/A8/A9（无共享库迁移）；旧证据 A7/A9；并发 A3；外调 A6/A10；验收 A11。U1–U3 裁决后由 lead 写 r1，独立测试作者按该合同写测试；本文不替代完成前审查。若要求改变只读产品边界、放宽证据/验收、抢占共享资源或覆盖异主状态，均为 **needs user**，不包含在推荐授权内。
