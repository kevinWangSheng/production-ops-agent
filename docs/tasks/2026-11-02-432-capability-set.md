# #432 场景集 v2 能力组、参考报告与第一次开发轮

- 状态：进行中（第 2 步离线实现完成；probe 窗口、参考报告、开发轮未开始）
- 更新日期：2026-11-02
- 依据：GitHub #432 Agent Brief 与评论；[收口合同 r1](2026-10-27-m3-closure-contract.md)（六类表、V2-R6/R12/R14/R15、Q19/Q20）；[M3 收口](2026-10-27-m3-closure.md)；F1 第 2、3 步
- 工作区：分支 `feature/F1-432-v2-capability-set`，worktree `/Users/shenghuikevin/dev/AI/poa-432-impl`（基线 main `4fbbbcb6`）

## 目标与范围

能力组 12 题（六类各一正一反）入库；每题跑一次 probe Run，由未写题的全新上下文 Agent 依据冻结的已交付证据手写参考报告并离线过确定性 gate；再跑第一次 v2 开发轮（18 题 × 2 = 36 槽），按 Q20 判 F1 第 2 步。只改评测侧（数据集、`scripts/`、`opspilot/evaluation/`），产品运行时代码不改（M3 Q6 候选冻结）。F1 `passes` 不翻。

## 前提与完成条件

- 前提：#430、#431 已合并；#453（PR #454）先合入 main，本分支 merge 后再开开发轮，代码维度以开窗时的 main 为准（M3 Q6）。probe 窗口不受此限。
- 完成条件：#432 验收清单全部勾选（12 题过加载器与泄漏检查、参考报告离线过 gate 且作者身份已记录、36 槽 summary 与 Q20 结论、trace 链接与 `summary.json`、费用对账、kind 窗口纪律、独立审查）。

## 合同

接口与决定表在仓库外：`~/.agent/handoffs/m3-b3-2026-11-02/seams-432.md`（§1–§7 接口，§8 决定表，§9 lead 裁决，§10 实现修订 A1–A8，§11 lead 确认 A1–A5）。

- D1 严格：setup 失败的已开始 attempt 计 Q20 失败。D2–D5、D7–D13 按推荐。D8 输出根 `~/.agent/eval-probes/432/`。
- D6（用户 2026-11-02 裁决，按推荐）：#7（模型故障正例 `dv2-c39f60`）、#11（错目标正例 `dv2-9f0fd3`）的正确行为是不出报告或不建 Run，不写 ReportV2；`reference_report` 为 null，参考为 probe 的 `reference/projection.json`，离线过 `check_expectations`。
- D11 附加（用户 2026-11-02 裁决）：#453（PR #454）在开发轮第 1 轮前合入 main；开发轮在该合并后的 main 上跑，本分支届时 `git merge origin/main`（merge，不 rebase）。

## 必要上下文

- 题目：`evals/datasets/dev-v2/capability/`；ID 与类别对照在本记录（下表），canary 与注入文本只在答案侧，不进模型输入。
- Q20：`opspilot/evaluation/expectations.py`（检查器）、`scripts/eval_live_inject.py::attempt_projection`（PG 投影）、driver 写 `runs/<attempt>/{projection,expectations}.json`。
- 参考：`opspilot/evaluation/reference.py`、`scripts/eval_reference.py`（export/check）。
- 开发轮汇总：`scripts/eval_dev_round.py`。

| 类 | 正例 | 反例 |
|---|---|---|
| 噪声 | `dv2-8b262a` | `dv2-eac1f4` |
| 缺测 | `dv2-fcd9eb` | `dv2-7261fe` |
| 重复事件 | `dv2-30921c` | `dv2-7a98c2` |
| 模型故障 | `dv2-c39f60` | `dv2-fa0e58` |
| 提示注入 | `dv2-bec37f`（canary 只在 `answer.json`） | `dv2-28535a` |
| 错目标 | `dv2-9f0fd3` | `dv2-dc387a` |

## 执行进展与证据

- 第 1 步：接缝草稿与决定表（seams-432 §0–§8），lead 裁决见 §9。
- 第 2 步（离线，提交 `fa755c37`、`46fa7f45`、`d70bcf57` 及本记录所在提交）：
  - PG 实测（零模型，`tests/integration/test_432_projection_impl_postgres.py`，`OPSPILOT_432_PG=1`）：持续 503 → Run 行 `waiting_human`，outcome `failed` / `handoff` / `MODEL_UNAVAILABLE`，物理请求 2 次、`forwarded 0`；Run 输入帧在 `opspilot_runs.input`，两种入口都是 24 h。
  - **#431 遗留缺陷（顺带修复）**：错目标正例在真实 investigator 里 `_outcome` 抛 `PersistenceError("INCONSISTENT_STATE")`，原代码只捕获 `ValueError`，正确的交接会被记成 `aborted/INVESTIGATOR_FAILED`；`_handed_off` 现两者都捕获。回归测试 `tests/unit/test_432_impl.py::test_handed_off_survives_the_store_refusing_a_runless_rebuild`（去掉修复时失败、恢复后通过），PG 测试 `test_unresolved_target_projection_has_no_run` 同样覆盖。
  - 验证：`tests/unit/test_432_impl.py` 23 passed；定向 eval 套件 492 passed；eval 合同、结构、B2 回放 1317 passed；PG opt-in 10 passed；ruff check / format --check 全仓干净；mypy 154 文件通过。未跑 `make check`。
  - 已知失败：3 个 #430/#431 独立验收测试钉了 6 题数据集与单绑定类的 `["all"]`，由 test-432 按合同变更修改，合并测试分支时并入。
- 第 3 步 probe 窗口（2026-11-02，lead 持 `kind-lab` 锁；占用 holder `impl-432` / batch `b432`；输出根 `~/.agent/eval-probes/432`；代码 `ae123e18`）：
  - **作废的 probe 轮 `dv2-ad8306`**（12:24Z–13:19Z，问题入口 6 题）：`M0_ENV_FILE` 指向主仓 `.env` 里的旧 DeepSeek key，所有到达 DeepSeek 的请求都是 HTTP 401（`MODEL_REJECTED`），6 个 attempt 都没有取到证据；余额读同样失败，费用记 unknown，实际约 0。轮次本身正常结束（exit 0、无遗留意图、四个 flag 均回到 `off`）；噪声正例的 ad 前置观察门通过，注入 span 写入成功。`dv2-c39f60`（模型故障正例）两次 503 都在进程内拦截、从未发往 DeepSeek，D6 投影有效，保留为该题参考来源；其余 5 题经 lead 批准换新轮名重跑。证据原样保留在输出根下，不入库。
- 未执行：其余 probe 轮、参考报告、开发轮、独立审查、费用对账。

## 下一步与交接

1. probe 窗口（kind，lockf，按 §5 四个绑定类各一轮，`--output-root ~/.agent/eval-probes/432`）；顺带核实 §0 三项 kind 未确认（缺测正例 metrics 是否已足以支持 cart 根因、注入 span 是否进入模型视野、`intake_key` 是否进模型请求）。
2. `scripts.eval_reference export` 导出证据 → 全新上下文 Agent 写参考报告 → `check`；作者身份记入本记录。
3. #454 合入 main 后 `git merge origin/main`，再开开发轮三个窗口，`scripts.eval_dev_round` 出汇总。
