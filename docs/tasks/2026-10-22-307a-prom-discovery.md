# #307 A：Prometheus 指标发现工具

- 状态：待用户合并门（PR #318，合同 r4）
- 更新日期：2026-10-22
- 依据：#307 A 部分；C3 §5、§8；PRODUCT-CONSTRAINTS；F3 合同 U1-b / PR-3a D3；F7 合同；ADR-0006；上游 HolmesGPT `a0009195468ebc55a7dff7d602b61feabe3cda1b` `holmes/plugins/toolsets/prometheus/prometheus.py`、`prometheus_instructions.jinja2`
- 工作区：`/Users/shenghuikevin/dev/AI/poa-307a`，分支 `feature/F3-307-prom-discovery`（起点 main `a4eea42`）

## 目标与范围

调查模型目前只能用 `metrics_range_query` 和 `traces_search`，无法列出指标名和标签值，只能靠工具描述里写死的名单或猜测。本任务照搬上游 `get_metric_names` / `get_label_values` 的设计，新增有界的只读发现工具：过滤参数必填，结果条数与字节有上限，时间窗有默认值，超限时明确标记结果不完整。新工具纳入 F7 的只读校验、目标绑定、脱敏与审计。

不在范围：#307 B（Kubernetes 只读与日志工具）、新凭据或连接器、报告 schema 改版。

## 前提与完成条件

- 前提：lab 只读代理已放行发现端点（`scripts/kind_lab.py:328-365`），不需要新凭据；kind 与另一个会话共用，真实 Run 前确认没有观察窗在跑，并查 `vm.swapusage`。
- 完成条件：lab 真实 Run 中模型调用发现工具并拿到有界结果（附 LangSmith trace 与 `summary.json`）；F7 审计与脱敏覆盖新工具；全新上下文 Agent 编写的验收测试全部通过；Codex 独立审查的发现已处置；功能 PR，合并前给用户简报。

## 合同（r4）

完整决定表见 [2026-10-22-307a-prom-discovery-contract.md](2026-10-22-307a-prom-discovery-contract.md)。U1、U2 由用户 2026-10-22 裁决，U3、U4、R1–R12 按用户 2026-10-17 常设指示采纳推荐。

- U1-a：只做 Prometheus 发现；#307 审计评论扩出的 trace tag 发现和去掉写死枚举另开 issue。
- U2-a：保留 D3-a 的 KSM 名单，metrics 描述加一句先发现再查询的引导；名单去留等本 PR 合入 main 后由 F3 第 5 步评测决定。
- U3：两者都在 PR-3b 之前合并，后合并的一方 merge main 吸收对方；被钉住的合同测试由独立测试作者改。
- U4-a：lab Run 提问不点名工具；模型至少一次调用发现工具，结果 ok 或 no_data、≤100 条、有证据、不完整标记与 warnings 一致。
- U3 r2（实施中追加）：实现者发现另外两处「恰好两个工具」的断言（`tests/acceptance/test_f7_step5_audit_postgres.py:471`、`tests/test_m1_04_alert_window_impl.py:452`），同归测试作者更新；后者改为按带窗口参数的工具数推导。U3 r3：CI PG 任务发现 `tests/integration/test_m1_otel_demo_contract_postgres.py:464`，同样处理，测试作者全量 grep 后无其它。U3 r4：F3 PR-3a 先合入 main（`dc474da`），本分支 merge main（`6ff9d68`，lead 解冲突：保留 PR-3a 的 KSM 段并接上 U2 引导句）；PR-3a 验收测试的 revision 钉住与工具集合由测试作者更新，精确钉住保留为新值 `otel-demo-5270fd8c543f`，并断言不等于 PR-3a 的 `otel-demo-c7737da709ee`。
- R1–R12：两个工具照搬上游名；match、时间窗、label 校验、凭据样式 label 拒绝、limit=100、warnings 作不完整标记、exact bytes、错误分类扩到 Prometheus 类工具、auth 与 scope 从注册派生、引导写进工具描述、fixture 不改。

## 执行进展与证据

- 2026-10-22 合同预审：Codex 三个模型 5 轮均返回 at capacity，改由全新上下文 Claude Opus 5.5 只读子 Agent 完成；r1 写入。
- 实现：Opus 5.5 子 Agent，提交 `a78569d`，对齐 r1；`TOOL_SCHEMA_REVISION` `otel-demo-4d7168180749` → `otel-demo-021751107140`。可逆默认：start/end 用 `.3f` 秒（与 metrics 一致）；Prometheus 无视 limit 时只靠字节与 token 上限兜底。
- 独立验收测试：Codex 先 gpt-6.1-sol，容量中断后续跑落 gpt-6-astra（同一会话 `01a1639d-1388-7092-a98c-a083c121558f`），worktree `poa-307a-tests`，提交 `f854a0c`，已 merge 进实现分支。
- 定向测试（合并后）：6 个文件 283 passed、12 skipped（PG 门控）。全量 pytest 因本机 swap 紧张交 CI。
- CI 首轮红：测试文件格式（lead 跑 ruff format，不改断言）；PG 集成第五处钉住两个工具（U3 r3，测试作者改，`cc1a66b`）。`cc1a66b` CI 三项全过。
- 独立审查（Codex，astra 起、gpt-6-sol 收尾）：pass，无阻塞；可选项为 label_values 的 warning 路径未单测。[审查记录](../evidence/307a-discovery-live/review-2026-10-22.md)
- merge main 后：`ruff check`、`ruff format --check` 通过；`tests/acceptance/` 全量加受影响合同测试 2732 passed、133 skipped（PG 门控）。
- 机器人分诊（`4b93506`）：code review 完成且无发现；security review 报错未完成，已重试一次，不阻塞。
- lab 真实 Run：14:02Z 首次因 DeepSeek 余额 -0.01 CNY（402）未发起；用户充值后 16:17Z 跑一次，U4-a 通过，未重跑。Run `d689c695-c9f4-5492-9a64-37a5a77a3cb7`，HEAD `4358a5b`，revision `otel-demo-5270fd8c543f`；提问不点名工具，模型 5 次调用发现工具，全部 ok、≤100 条、有证据；其中一次真实触发 Prometheus 截断（100 条 + warning），视图 incomplete=true、truncated=false。费用上界 0.12 CNY。[记录](../evidence/307a-discovery-live/run.md)、[summary.json](../evidence/307a-discovery-live/live-runs/d689c695-c9f4-5492-9a64-37a5a77a3cb7/summary.json)、[trace](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/0e83e022-a3bd-4b81-9673-7a5119574180/r/00000000-0000-0000-6afd-48097428da50?poll=true)

## 下一步与交接

- 用户合并门：功能 PR #318。合并后清理 worktree `poa-307a`、`poa-307a-tests` 与两条本地分支（先查 `--ignored` 与 tmp/，lab ledger 在主仓库 tmp/）。
- 后续（不在本 PR）：#307 审计评论扩出的 trace tag 发现与去掉写死枚举；KSM 名单去留由 F3 第 5 步评测决定（U2）；#307 B Kubernetes 工具。
- 收尾四个数：实施期间新冒出的合同问题 3（U3 r2、r3、r4 的钉住断言）；因合同变更重写的测试 0；环境重跑 1（余额不足，未发起 Run）；用户裁决轮数 1（U1/U2）。
