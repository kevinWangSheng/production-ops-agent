# flake 报告：test_private_worker_invalid_body_has_no_export_or_config_read 偶发超时

- 分支：`fix/pg-live-probe-flake`（自 `origin/main`），worktree
  `/Users/shenghuikevin/dev/AI/production-ops-agent-flake`
- PR：**#36**（https://github.com/kevinWangSheng/production-ops-agent/pull/36），
  HEAD `080c62c`，CI 两项 checks（`checks`、`m0-postgres`）均 `SUCCESS`，
  `mergeStateStatus: CLEAN`、`mergeable: MERGEABLE`。**状态：PR 已就绪，待用户审核
  合并**（未合并、未删分支）。
- 任务记录：`docs/tasks/2026-09-17-pg-live-probe-flake.md`（含完整过程、含一次
  尝试后回退的方案）。
- 接手时该 worktree 已是干净的 `origin/main` 起点（`git status`/`git diff` 确认无
  Codex 残留改动，无需继承任何 WIP）。

## 根因

`scripts/m0_pg_private_transport.py`（`m0_pg_live_probe.py` 以
`python -m` 子进程方式调用的私有传输 worker）在**模块顶层无条件** `import
httpx2`，以及 `from scripts.m0.budget/config/contracts/postgres_lab/step_store
import ...`——其中 `step_store` 经 `.outcomes_v3`/`.protocol` 间接引入
`pydantic`/`langsmith`/`openai`/`requests`。无论请求体合法与否，子进程都先付这笔
import 成本，验证/拒绝逻辑排在其后。

- 冷启动（全新 `.venv`，触发 macOS 对 251 个新装原生扩展 `.so` 的首次代码签名
  校验）下单次 `import scripts.m0_pg_private_transport` 实测 **11.86s**；热启动
  0.47–0.68s（其中仅 `openai` 一项热启动即约 0.45s）。
- 测试给子进程的预算是硬编码 `subprocess.run(..., timeout=5)`。冷启动叠加机器
  整体高并发负载（其它 worktree 并行 `make check`）时，仅 import 阶段就可能超过
  5 秒——这是真实的资源竞争，不是随机噪声，也不是 fd 继承、临时目录或端口冲突。
- 单纯用 CPU 忙等（不含冷 `.venv`）复现失败：**0/30 次失败**（说明纯 CPU 争用不足
  以复现）；重建全新 `.venv` 后立即用 10 并发线程各自完整重放测试的
  `subprocess.run` 调用，在真实高负载（观测到 `uptime` load average 一度达
  41）下：**round0/round1 共 20 次调用 100% 超时**（各 ~5.01s，即在预算内完全没
  做完 import），round2 才降到 3.3–3.5s 转为通过。

## 修复

把除 `json`/`os`/`sys`/`time`（stdlib）外的全部 import 从模块顶层移到 `main()`
函数体内、紧跟请求体校验（`raise ValueError` 分支）之后、`load_config(...)` 之前
（`scripts/m0_pg_private_transport.py`，+14/-11 行，单文件）。效果：

- 无效体路径现证明为**零第三方 import**（已用 `sys.modules` 断言
  `psycopg`/`httpx2`/`openai`/`langsmith`/`requests`/`pydantic` 均未被触碰），
  只剩 stdlib + 本文件自身编译，安全断言（无效体不得导出、不得读配置）不但未削弱，
  反而从"不会执行"变得更彻底可证。
- 合法体路径的 import 时机语义不变（仍在校验通过、发 HTTP 前完成），只是从"进程
  启动时无条件执行"改为"确认合法后才执行"。

**一次尝试后回退**：最初同时把 `scripts/m0/step_store.py` 里 `.outcomes_v3`/
`.protocol` 的顶层 import 也下放到 `commit_response`/`commit_tool`/`rebuild`/
`control_snapshot` 函数体内（这几个方法是唯一用到这些符号的地方，本子进程实际只
调用不需要它们的 `send_guard`）。这一步单看目标测试也有效，但接入本地 PG lab 跑
真实集成测试后暴露回归：`tests/integration/test_m0_step_store_postgres.py` 里两个
用极短 `lease_seconds=0.2` 练习"租约过期边界"的测试稳定失败
（`BudgetError: CONTROL_DENIED`，3/3 次复现）——`commit_response`/`commit_tool`
首次调用时才触发的 `langsmith`/`openai` 惰性 import 吃掉了大半租约窗口。生产路径
（`m0_pg_live_probe.py`）用的租约是 60 秒且每 10 秒续租，这点延迟完全无感，但
0.2 秒是刻意收紧的测试值扛不住。复核确认这层改动对修复目标测试并非必要（只要
`m0_pg_private_transport.py` 层面把 import 推迟到校验之后，无效体路径根本走不到
`from scripts.m0.step_store import ...` 这一行），于是用 `git apply -R` 撤回，
最终 diff 只有一个文件。

## 循环验证结果

- 冷启动 + 10 并发 × 4 轮（修复后）：40 次调用全部 0.02–0.05s 通过，**0 失败**
  （对照修复前同条件 20 次 100% 超时）。
- 任务书字面要求的朴素 30 次循环（`pytest -p no:randomly --count` 在当前
  pytest 9.1.1 下不可用，按预案退回 shell 循环 30 次）：
  **`pass=30 fail=0 total=30`**。
- 相关单元测试整体：
  `pytest tests/test_m0_pg_live_probe.py tests/test_m0_step_store_controls.py tests/test_m0_step_store_protocol.py tests/test_m0_step_store_versions.py`
  → `48 passed`。
- PG 定向测试（`.venv/bin/python -m scripts.m0.postgres_lab start`/`stop`；
  `M0_ENV_FILE` 未设置——该变量只被 `scripts/m0_lab/round07/launch.py` 消费，
  本次改动路径未读取任何 `.env`/凭据，不适用；密钥全程只由脚本自身读取，未打印、
  未导出）：
  `M0_STEP_POSTGRES=1 M0_CONTROL_POSTGRES=1 pytest tests/integration/test_m0_step_store_postgres.py tests/integration/test_m0_control_contracts_postgres.py tests/integration/test_m0_pause_observer_postgres.py`
  连续 4 次运行均 `36 passed, 1 skipped`（跳过项为 `M0_PG_OUTAGE` 门控的 PG 服务
  重启测试，与本次改动无关，未启用）。

## make check 结论行

连续运行两次，原样记录：

```
================= 1050 passed, 75 skipped, 2 xfailed in 24.31s =================
================= 1050 passed, 75 skipped, 2 xfailed in 24.63s =================
```

`ruff check`："All checks passed!"；`ruff format --check`："410 files already
formatted"；`mypy`："Success: no issues found in 13 source files"（该项目
`pyproject.toml` 的 `[tool.mypy] files = ["opspilot"]` 本不含 `scripts/`）。

## 独立审查

全新上下文只读子代理（general-purpose，未参与本次实现/调查）审查最终 diff，
**结论：APPROVE，0 条待处理发现**。逐条核实：导入搬移对所有可达路径行为等价；
逐行确认无效体的三处 `raise ValueError` 均在新 import 块之前，Python 顺序执行
保证 unwind 到 `except BaseException` 时新 import 块从未执行；安全断言未削弱；
仓库内仅 3 处引用本文件，均以 `python -m` 子进程或 `read_bytes()` 哈希方式使用，
无任何调用方以模块属性方式依赖被移动符号；改动与仓库既有惰性 import 先例
（`scripts/m0/__main__.py` 的 "No SDK import ... on the denied path" 模式）一致；
独立复跑 `ruff check`（通过）与 `mypy scripts/m0_pg_private_transport.py`（4 处
提示，逐一比对改动前版本确认为已存在、非本次引入，且不在 `make check` 的 mypy
检查范围内）。

## PR 状态与未完成项

- PR #36：CI 绿、可合并、无待处理 review thread。Codex 机器人安全审查在 PR 开启时
  自动触发，`Code Review`/`Security Review` 两项状态均为 "Failed"（未产出任何
  inline 发现，`gh api .../pulls/36/comments` 长度为 0），与 AGENTS.md 记录的
  "当前额度不足以稳定执行" 一致；按项目规则非交付门槛，未等待、未阻塞，因其未返回
  实际发现，无需逐项处置。
- 未完成项：无。合并授权、合并动作、合并后清理留给用户按默认流程处理，本任务
  未合并、未删除分支。
- 环境备注（超出本次任务范围，仅记录）：`scripts/m0/postgres_lab.py` 本地 PG 端口
  55431 是硬编码常量，与同机其它 worktree 并发跑 PG 定向测试会互相占用端口——
  本次执行期间实际遇到（另一 worktree `production-ops-agent-m1-progress-ui`
  占用了该端口），未触碰其进程，未处理。
