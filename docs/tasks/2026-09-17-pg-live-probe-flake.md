# PG live-probe 无效体测试偶发超时

- 状态：已完成
- 更新日期：2026-09-17
- 依据：调度者派发的修复任务（`briefs/flake.md`）；[SPEC.md 第 6 行门槛段](../../SPEC.md)（工程维护，不涉及功能门槛/验收变更）
- 工作区：`/Users/shenghuikevin/dev/AI/production-ops-agent-flake`，分支 `fix/pg-live-probe-flake`（自 `origin/main`）

## 目标与范围

修复 `tests/test_m0_pg_live_probe.py::test_private_worker_invalid_body_has_no_export_or_config_read`
在全量 `make check` 下偶发超时失败（单独运行必过）。范围限定在定位真实根因并修复，
不改验收步骤、`feature_list.json` passes、冻结哈希与上限，不新增依赖/框架。

## 前提与完成条件

- 前提：worktree 由调度者创建，起点为 `origin/main`；此前一个 Codex agent 因模型容量
  错误未能开工，`git status`/`git diff` 确认无残留改动（工作区在接手时即为干净的
  `origin/main` 状态），无需继承任何未提交工作。
- 完成条件：定位真实根因（非仅调大超时）；修复不削弱该用例"无效体不得导出、不得读
  配置"的安全断言；修复后循环验证 0 失败；`make check` 结论行记录；全新上下文只读
  子代理独立审查并逐条处置；PR 开启并等待 CI。

## 必要上下文

- 目标文件：`scripts/m0_pg_private_transport.py`（被 `scripts/m0_pg_live_probe.py`
  以 `python -m` 子进程方式调用的私有传输 worker；对无效请求体，设计要求在读配置/
  发网络前立即拒绝并以 `os._exit(2)` 退出、不打印任何内容）。
- 该测试用 `subprocess.run(..., timeout=5)` 给子进程 5 秒预算，输入 `{}`（无效体）。

## 执行进展与证据

### 根因定位

1. **首次表征失败**：仅用 12-worker CPU 忙等 + 已预热的 `.venv` 循环 30 次原始代码，
   0 次失败（`scratchpad/red_under_stress.log`，每次 1.7–2.2s）。说明单纯 CPU 争用
   （不含冷启动）不足以复现。
2. **真实复现**：重建全新 `.venv`（`rm -rf .venv && uv sync`，触发 251 个原生扩展
   `.so` 文件的 macOS 首次加载/`com.apple.provenance` 校验成本）后，立即用 10 个并发
   线程各自完整重放测试的 `subprocess.run(...)` 调用（脚本
   `scratchpad/repro_concurrent.py`），在真实高负载（其它 worktree 并发
   `make check`，观测到 `uptime` load average 一度达 41）下：round 0/1 共 20 次调用
   **全部超时**（各 ~5.01s，即在 5 秒预算内未完成任何工作就被杀），round 2 才降到
   3.3–3.5s 转为通过（见 `scratchpad/repro_red_cold.log`）。
3. **归因**：`m0_pg_private_transport.py` 在模块顶层 `import httpx2` 及
   `from scripts.m0.budget import PostgresBudget`（引入 `psycopg`）、
   `from scripts.m0.step_store import ...`（经 `.outcomes_v3`/`.protocol` 间接引入
   `pydantic`、`langsmith`、`openai`、`requests` 等一整套与"验证请求体"完全无关的
   SDK），**无论请求体是否合法都无条件先执行这些 import**，验证/拒绝逻辑反而排在
   之后。冷启动下多个原生扩展（含 `openai`/`langsmith` 传递依赖的 Rust/​C 扩展）需要
   macOS 首次代码签名校验，叠加机器整体高负载时单次 import 耗时可达数秒到十余秒
   （`.venv/bin/python -c "import scripts.m0_pg_private_transport"` 冷启动
   11.86s，热启动 0.47–0.68s；其中仅 `openai` 一项热启动即约 0.45s）。5 秒的子进程
   预算因此在高并发/冷启动叠加时不够用——这是真实的资源竞争，不是随机噪声，也不是
   fd 继承、临时目录或端口冲突问题。

### 修复

`scripts/m0_pg_private_transport.py`：把除 `json`/`os`/`sys`/`time`（stdlib）外的全部
import（`httpx2`、`datetime`/`pathlib`/`uuid`、
`scripts.m0.{budget,config,contracts,postgres_lab,step_store}`）从模块顶层移到
`main()` 函数体内、紧跟在请求体校验（`raise ValueError` 分支）之后、
`load_config(...)` 之前。效果：

- 无效体路径现在**零第三方 import**（验证过 `psycopg`/`httpx2`/`openai`/
  `langsmith`/`requests`/`pydantic` 均不会被触碰），只需 stdlib + 本文件自身编译，
  从而不再受这些库首次加载/冷启动成本影响。
- 合法体路径的 import 时机不变（仍在校验通过后、发起 HTTP 前完成），行为等价，只是
  从"进程启动时无条件执行"变为"确认请求体合法后才执行"，语义未变。

**曾尝试但已回退的方案**：最初还把 `scripts/m0/step_store.py` 里
`.outcomes_v3`/`.protocol` 的顶层 import 也下放到 `commit_response`/`commit_tool`/
`rebuild`/`control_snapshot` 函数体内（因为这些符号只在这几个方法里用到，
`send_guard`——本子进程唯一调用的方法——完全不需要它们）。单看无效体测试这个改法
也有效，但用本地 PG lab 跑真实集成测试
（`M0_STEP_POSTGRES=1 M0_CONTROL_POSTGRES=1 pytest tests/integration/test_m0_step_store_postgres.py tests/integration/test_m0_control_contracts_postgres.py tests/integration/test_m0_pause_observer_postgres.py`）
发现回归：`test_actual_process_cut_partial_tools_rebuild` 与
`test_tool_returned_before_commit_is_bounded_repeat` 稳定失败
（`BudgetError: CONTROL_DENIED`，3/3 次复现）。原因：这两个测试用极短的
`lease_seconds=0.2` 租约练习"租约过期"边界，`commit_response`/`commit_tool` 首次
调用时才触发的 `langsmith`/`openai` 惰性 import 耗时吃掉了大半租约窗口，导致租约
在真正的业务提交前就判定过期。生产路径（`scripts/m0_pg_live_probe.py`）用的租约是
60 秒且每 10 秒续租，这点 import 延迟完全无感，但 0.2 秒是刻意收紧的测试值，扛不住。
复核后确认：**这层改动对修复目标测试并非必要**——只要
`m0_pg_private_transport.py` 层面把 import 推迟到校验之后，无效体路径根本不会走到
`from scripts.m0.step_store import ...` 这一行，`step_store.py` 内部的 import 结构
（惰性与否）与此无关。于是用 `git apply -R` 撤回了 `step_store.py` 的改动，只保留
`m0_pg_private_transport.py` 的改动。最终 diff 只有一个文件、+14/-11 行。

### 循环与压力验证（均为修复后的最终版本，即仅改 `m0_pg_private_transport.py`）

- 冷启动 + 10 并发 × 4 轮（`scratchpad/repro_green_final.log`）：全部 40 次调用
  0.02–0.05s 内通过，0 失败（对照修复前同条件 round0/1 共 20 次 100% 超时）。
- 按任务书字面要求的朴素 30 次循环（`scratchpad/flake_loop.sh` shell 循环，
  `pytest -p no:randomly --count` 在当前 pytest 9.1.1 下不可用，按预案退回 shell
  循环）：`SUMMARY green_final_plain: pass=30 fail=0 total=30`。
- 相关单元测试整体：
  `pytest tests/test_m0_pg_live_probe.py tests/test_m0_step_store_controls.py tests/test_m0_step_store_protocol.py tests/test_m0_step_store_versions.py`
  → `48 passed in 1.59s`。
- PG 定向测试（本地 synthetic lab，`.venv/bin/python -m scripts.m0.postgres_lab start`
  / `stop`；`M0_ENV_FILE` 未设置——该变量只被 `scripts/m0_lab/round07/launch.py`
  消费，目标改动路径未读取任何 `.env`/凭据，故不适用；密钥全程未被脚本以外的任何
  地方读取或打印）：
  `M0_STEP_POSTGRES=1 M0_CONTROL_POSTGRES=1 pytest tests/integration/test_m0_step_store_postgres.py tests/integration/test_m0_control_contracts_postgres.py tests/integration/test_m0_pause_observer_postgres.py`
  连续 4 次运行均为 `36 passed, 1 skipped`（跳过项为
  `M0_PG_OUTAGE` 门控的 PG 服务重启测试，与本次改动无关，未启用）。
- `make check`（含 `doctor`、`uv lock --check`、`ruff check`、`ruff format --check`、
  `mypy`、`pytest`）连续运行两次，结论行均为
  `1050 passed, 75 skipped, 2 xfailed`（第一次 24.31s，第二次 24.63s）；
  `ruff check`："All checks passed!"；`ruff format --check`："410 files already
  formatted"；`mypy`："Success: no issues found in 13 source files"。
- 独立审查：全新上下文只读子代理（general-purpose，未参与本次实现/调查）审查最终
  diff，核对导入移动的行为等价性、无效体路径不可达 import 块、安全断言未削弱、
  无其它调用方以模块属性方式依赖被移动的 import、改动最小化且与仓库既有惰性
  import 先例（`scripts/m0/__main__.py`）一致，并自行运行 ruff/mypy。结论见下方
  「独立审查结论」。

## 独立审查结论

全新上下文只读子代理（general-purpose）审查结论：**APPROVE**，无阻断问题。逐条：

1. 导入搬移行为等价：校验前代码段（超时/长度/字段校验，行 12–29）只用
   `sys`/`time`/`json`/`os`，被移动的 10 个符号首次使用均在新 import 块（行
   32–42）之后；没有任何路径在旧位置与新位置之间提前用到被移动符号。
2. 无效体路径逐行核实不可达新 import 块：三处 `raise ValueError`（超时、超长、
   字段不匹配）均在行 32 之前，Python 顺序执行会在到达 import 块前就 unwind 到
   `__main__` 的 `except BaseException`；对测试的 `{}` 输入，`model` 字段不匹配
   在行 29 即抛出，`httpx2`/psycopg 链路/`load_config` 可证明从未被 import。
3. 安全断言未削弱：无效体路径完全不会尝试 import，不存在"换一种方式仍会加载"的
   情况；合法体路径仍在同一 `except BaseException: os._exit(2)` 之下，只是
   import 时机后移，异常处理范围未变。
4. 无其它调用方受影响：仓库内仅 3 处引用本文件，均以 `python -m` 子进程或
   `read_bytes()`（用于哈希）方式使用，没有任何地方 `import
   scripts.m0_pg_private_transport` 后访问其模块属性（如 `.httpx2`/`.main`）。
5. 改动最小且与仓库既有惯例一致：`scripts/m0/__main__.py` 中
   `from .live import run_cli` 同样是"先挡掉会被拒绝的路径、再 import SDK"的
   惰性 import 先例。
6. 独立运行 `ruff check` 通过；独立运行 `mypy scripts/m0_pg_private_transport.py`
   报 4 个错误，但审查方对比 `git show HEAD:...` 的改动前版本用同一命令复核，
   发现是相同的 4 处（仅行号随改动平移），改动前即存在——且项目
   `pyproject.toml` 的 `[tool.mypy] files = ["opspilot"]` 本就不含 `scripts/`
   目录，`make check` 的 mypy 步骤不检查此文件，非本次改动引入的回归。

无需处置的发现：0 条待处理意见。

## 下一步与交接

- 本任务范围内改动已完成、已验证，`docs/tasks/` 记录已建；ROADMAP 无需更新（本任务
  为工程维护，不改变任何功能门槛/验收状态）。
- 待独立审查结论补充后，按 AGENTS.md「变更、Git 与交接」推送任务分支、开/更新 PR，
  等待最新提交的 CI 与已触发的 code review 结果，逐条处置后再报告 PR 就绪。
- 本地 PG lab 已在验证完毕后 `stop`，未遗留进程；`tmp/m0-b/` 为本 worktree 私有
  数据目录。后续如需在同一物理机重跑 PG 定向测试，需注意端口 55431 是
  `scripts/m0/postgres_lab.py` 的硬编码常量，其它并发 worktree 的 PG lab 会与之
  抢占同一端口（本任务执行期间已实际遇到：另一 worktree
  `production-ops-agent-m1-progress-ui` 占用了该端口，本任务未触碰其进程）——这是
  已知的仓库级环境限制，不在本次任务范围内处理。
