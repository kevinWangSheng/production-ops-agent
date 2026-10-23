# F2 D 有界真实 Run：三个断点各一次真实 `kill -9` worker_main 后恢复

- 日期：2026-10-23 03:20–04:17Z；分支 `test/F2-kill-resume`。harness 在运行之间有两处读数修正（见「harness 修正与重判」），对应 commit 记在各 `summary.json` 的 `versions.commit` 和 `rejudged[]` 里
- 合同：[F2 合同 r1](../../tasks/2026-10-17-f2-contract.md) U7（三断点与判据）、R7（宿主侧卡顿代理）、U8-A（#242 压缩取证）
- 冻结摘要：每个断点一份，见 [model_in_flight](model_in_flight/summary.json)、[response_committed](response_committed/summary.json)、[tool_in_flight](tool_in_flight/summary.json)；另有补充轮 [tool_in_flight-serial](tool_in_flight-serial/summary.json)。原始 ledger（kill 前后与结束时的行快照、代理日志、两个 worker 的日志、事件）在被忽略的 `tmp/lab-ledgers/f2-kill-resume-live/<断点>-<时间>/` 下，不入库，sha256 记在各 summary 的 `raw_ledger`（ADR-0006）
- 类别：有界真实运行。真实 DeepSeek `deepseek-flash`，真实 kind OTel Demo 实验环境（otel-demo 档，注入 paymentFailure），真实 `kill -9`。不是生产证明
- **结论：断点 ① 和 ② 的判据全部通过；断点 ③ 在产品默认 `OPSPILOT_TOOL_PARALLELISM=2` 下，「重派工具查询 ≤ 1」判据失败**：重派了 2 条，除了在途那条，还有一条已经结算、但结果没落库的同轮兄弟查询。`OPSPILOT_TOOL_PARALLELISM=1` 的补充轮里这条判据通过，但它不替代默认档的结果。#242 的压缩取证没做，阻塞点见最后一节

## 环境与命令

- PG 17.9：本 worktree 新建 `tmp/pg-f2-d`（127.0.0.1:55893，trust，`log_connections` 开启），库 `m0_budget`。owner `m0_lab` 执行 `python -m opspilot.schema migrate`，升到 `0020_release_comparisons`；`scripts.m0.postgres_lab runtime-roles` 建 `opspilot_web_lab`、`opspilot_worker_lab`。结束后 `pg_ctl stop`
- web：`python -m opspilot.web serve`，最小环境，`OPSPILOT_BIND=127.0.0.1:8096`（不用 8080，避免 Alertmanager 的 webhook 另建事故），`OPSPILOT_TOOL_PROFILE=otel-demo`，`OPSPILOT_SELF_HEAL_INTERVAL_SECONDS=0`。UI 口令和事件 token 都是本次随机生成的，只把哈希放进进程环境
- harness：`scripts/f2_kill_resume.py run --breakpoint <断点>`。完整参数见脚本 docstring；`--secrets-env-file` 指向私有 env 文件，`--prometheus-env-file` 指向 `prometheus-investigator.env`，`--lab-round f2-kill-resume-live-20261023`。每个断点由 harness 自己做以下事情：
  1. 起进程内的卡顿代理（`scripts/f2_stall_proxy.StallProxy`，监听 127.0.0.1:19092，上游 19090，初始 `count=0`，即直通）
  2. 起 worker A（`python -m opspilot.worker_main`，最小环境；Prometheus 地址指向代理，`OPSPILOT_TRACE=lab`，轮询 2 s，其余用产品默认：容量 2、工具并行 2、租约 420 s）
  3. `POST /intake/events` 建一个事故，目标 `m0-otel-20260909`
  4. 用 owner 连接每 0.1 s 读一次 PG 行，定位断点（见下表）。kill 前核对 PID：`ps` 显示的父进程必须是 harness，命令行必须含 `-m opspilot.worker_main`，然后 `os.kill(pid, SIGKILL)`；之后 `pg_terminate_backend` 结束 `opspilot_worker_lab` 剩下的后端
  5. 起 worker B，每 0.2 s 读一次 `opspilot_runs.epoch`，等 Run 结束后 SIGTERM 停掉 worker B
- 防循环上限：产品默认，每个 Run 最多 100 次模型请求；工具注册超时 30 s
- 版本（`opspilot_runs.versions`）：`prompt-replay-candidate-dd9018031487` / `ctx-ctx-policy-v1-a30e65ea52f4` / `otel-demo-2a944edd110a`
- 实验环境：`occupation claim`，holder `lane-f2-d`，batch `f2-d-live`（03:20:54Z），`release` 在 04:16Z，之后 `show` 回读为 `null`。注入前检查（03:21Z）：`kind_lab.py health` ok；frontend 2 分钟请求率 1.53/s；checkout PlaceOrder p95 90 ms；内存空闲 45%；swap 已用 6909/8192 MiB（84%，偏高，如实标注）。补充轮之前再查一次：swap 已用 7640 MiB，frontend 2.47/s，p95 48 ms
- 故障：主仓库 `scripts/kind_lab.py fault inject/restore --experiment-id f2-kill-resume-live`（paymentFailure，03:22Z 注入，04:16Z 恢复，两次都 verified）。注入后约 2.5 分钟，checkout 和 payment 的 `STATUS_CODE_ERROR` 速率大于 0

## 断点怎样定位（读行，不靠 sleep）

| 断点 | 何时开始等待 | kill 条件 | kill 后的核对 |
|---|---|---|---|
| ① `model_in_flight` | Run 已有 ≥ 1 条已提交证据 | 恰好一条 `opspilot_budget_reservations` 是 `reserved`，并且连续 1 s 没变（预约行写在请求发出前，等这 1 s 是为了确定请求已经发出） | 同一条预约仍是 `reserved`，模型 step 数没变 |
| ② `response_committed` | 已有证据，且某个模型请求在途 | harness 先持有 `LOCK TABLE opspilot_tool_charges IN EXCLUSIVE MODE`。`commit_step` 不碰这张表，执行器派发前的 `charge_tool` 要碰，所以 worker 会停在这两者之间。kill 条件：worker 后端在这张表上等锁，并且最后一个 step 带工具调用、`tool_results` 为 0、该 step 的 operation 没有任何 charge 行 | 同一个 step 仍处于这个状态；先结束 worker 后端，再释放锁 |
| ③ `tool_in_flight` | 已有证据时，代理的 `remaining` 设为 1 | 代理已经卡住 1 次请求，并且本 epoch 恰好一条未结算的 charge 行，对应 `metrics_range_query` | 同一条 dispatch 仍未结算 |

## 结果

判据编号对应 U7。每格的数字取自对应 summary 的 `verdicts.criteria`。

| 判据 | ① 模型请求在途 | ② 响应已提交、工具未派发 | ③ 工具在途（默认并行 2） | ③ 补充（并行 1） |
|---|---|---|---|---|
| Run / 事故 | `ea71b863…` / `9b833598…` | `68ede1ac…` / `eb052afa…` | `ce40bf5d…` / `06bfdebd…` | `29fe15e2…` / `e7802238…` |
| kill 时点 | 第 2 轮模型请求在途，已有 1 个模型 step、2 条证据 | 第 2 轮 step 已提交，计划 `metrics_range_query` 和 `traces_search`，都未派发 | 第 3 轮 `t0` 被代理卡住 1.1 s 后 kill | 第 2 轮 `t2` 被卡住后 kill |
| 断点成立 | 通过 | 通过 | 通过 | 通过 |
| 重发模型请求 ≤ 1 | **1**（预期 1）：17 次预约 = 16 个模型 step + 1 条在途 | 0：10 次预约 = 10 个 step | 0：15 = 15 | 0：11 = 11 |
| 重派工具查询 ≤ 1（只限在途那条） | 0 | 0；两个待派工具在 epoch 2 各派 1 次 | **失败：2 条**。`t0` 在途；`t1` 已在 epoch 1 结算（1.03 s，证据行已写但 `committed=false`），但 #309 按序号顺序提交结果，`t1` 的提交排在卡住的 `t0` 后面。kill 后两条都重派 | 1（只有在途的 `t2`） |
| kill 前已提交的证据保留，并可被结论引用 | 2/2 保留，2/2 被引用 | 2/2 保留，2/2 被引用 | 3/3 保留，3/3 被引用 | 5/5 保留，内容哈希不变；3 条已采纳的都被引用，2 条是工具错误记录（`status=error`、`adopted=false`） |
| 未结算费用保持占用 | 被杀的预约到结束时仍是 `reserved`，`budget_reserved=1`，按 360 s 上界计入 | kill 时没有未结算项 | 被杀的 dispatch 到结束时仍是 `seconds=NULL`，按 30 s 占用，`tool_operations_used=34` 等于 charge 行数 | 同左，30 s，22 = 22 |
| 新 epoch 在租约到期 + 一个轮询周期内开始 | 到期后 0.87 s（上界 2.2 s：轮询 2 s + 采样 0.2 s） | 1.82 s | 0.96 s | 0.14 s |
| Run 结局 | `completed`，已发布 | `completed`，已发布 | `completed`，已发布 | `completed`，已发布 |
| **整体** | **通过** | **通过** | **失败** | 通过（补充） |

「可被结论引用」只按已采纳的证据判：工具错误记录不论有没有被 kill 都不会进入结论的 `evidence_ids`。在本库四个 Run 里，已提交证据是否被引用与 `adopted` 完全一致（已采纳 26+15+31+18 条全部被引用，未采纳 2+2+0+3 条都没被引用）。这是本次执行者对判据的读法，如果要按「全部已提交证据」判，补充轮会失败。

PG 日志里只有两条 `57P01 FATAL`，都是 harness 在 ② 中用 `pg_terminate_backend` 结束的 worker 后端；没有 42501，web 和 worker 也没有其他 ERROR。

## 断点 ③ 的失败

- 现象：同一轮两条并行查询，`t0` 卡住，`t1` 先完成并结算。按 #309 的合同 R-5，工具结果在主线程按序号提交，所以 `t1` 的结果在内存里等 `t0`。进程被杀时 `t1` 的证据行 `committed=false`、step 的 `tool_results=0`，新 epoch 只能把这一轮当作整轮未完成，两条都重新派发
- 上界：一次 kill 最多重派 `OPSPILOT_TOOL_PARALLELISM` 条，默认 2。并行 1 的补充轮符合 U7 的 ≤ 1
- 没有改产品代码，也没有放宽判据。待决选项已报给 lead：A 把 U7 的上界改成 `tool_parallelism`（合同变更，用户门）；B 产品修复，让已结算的兄弟结果先落库（改 #309 R-5，用户门）；C 记为 FAIL，另立 issue

## harness 修正与重判

两处都是 harness 读数的问题，不涉及产品行为。重判只用运行时冻结的 `pre/post/final` 快照重算，`summary.json` 的 `rejudged[]` 里记了时间、commit、原因和重判前的结论：

1. ① 第一次判为失败，原因是把已发布的 `conclusion` 记录行（`kind=conclusion`，不对应模型请求）算成了一次已回答的请求，于是重发数读成 0。修正后读数为 1，加了回归测试
2. 补充轮第一次判为失败，原因是把工具错误记录也要求被结论引用。改为只要求已采纳的证据被引用，未采纳的只要求保留且内容不变。旧快照里没有 `adopted` 列，所以从同一个库的 evidence 行回填（证据行提交后不再改写），`rejudged[].adopted_backfilled_from_db=true`

另外在 ③ 失败后，为了把失败原因写清楚，增加了 `settled_uncommitted_at_kill` 字段，只报告，不改通过逻辑。

## Trace

project `opspilot-lab-f2-kill-resume-live-20261023`（longlived）。每个 Run 只能读回 worker B 那次尝试的根 run。被杀进程的根 span 没有结束，也没有导出，所以 epoch 1 没有根 run；epoch 1 是否有子 span 被导出，未确认。

- ①：[epoch 2 根 run](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/8f326138-25a7-40bb-a096-cd68cce3539c/r/00000000-0000-0000-a487-91f31f233707?poll=true)
- ②：[epoch 2 根 run](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/8f326138-25a7-40bb-a096-cd68cce3539c/r/00000000-0000-0000-a392-664153a31e88?poll=true)
- ③：[epoch 2 根 run](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/8f326138-25a7-40bb-a096-cd68cce3539c/r/00000000-0000-0000-ffa7-26fd5e0c7aa6?poll=true)
- ③ 补充：[epoch 2 根 run](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/8f326138-25a7-40bb-a096-cd68cce3539c/r/00000000-0000-0000-054f-48c04c221069?poll=true)

## 费用

DeepSeek 余额（CNY），每轮前后各读一次：① 98.77 → 98.58，② 98.50 → 98.31，③ 98.31 → 97.97，补充 97.88 → 97.74。四轮合计 0.86 CNY；从第一次读数到最后一次是 98.77 → 97.74，差 1.03 CNY。轮次之间余额也在下降（98.58 → 98.50，97.97 → 97.88），说明同一账户有其他调用方，所以这些数都是上界。

## #242 压缩取证（U8-A）：没做，产品路径无法强制压缩

- web 建 Run 走 `opspilot/investigation/inputs.py:164` 的默认值 `limits=M1_FROZEN_LIMITS`，web 和 worker 都没有覆盖 `context_tokens` 的环境变量
- 压缩阈值是 (1_000_000 − 65_536) × 0.95 ≈ 888k 估算 token（`opspilot/investigation/loop.py:1009`，`context.py:181`）。本次四个 Run 只有 10–17 个模型 step，四个 Run 都没有发生压缩（没有 `kind=compaction` 的 step）
- 唯一不改代码的旁路是：在 worker claim 之前，harness 用 owner 连接改写 `opspilot_runs.input` 里的 `limits`。这样就绕过了 web 入口，性质接近开发开关，按 lead 的指示没有做，已上报。#242 的压缩项仍然开放

## 实验环境释放与清理

- `fault restore` 已校验（`1ee5c0258e3e`）；`occupation release` 后 `show` 为 `null`；`tmp/lab-ledgers/` 下没有 `.active-*` 和 `fault-active.json`
- harness 启动的 worker 都是核对 PID 后停止的：A 用 SIGKILL，B 用 SIGTERM，B 都以返回码 0 退出。web（PID 15484，核对命令行后）用 SIGTERM 停止。8096 和 19092 不再监听；自建 PG 已停止。没有用 pkill/killall，也没有碰其他进程

## 凭据

证据里没有 token、口令或 API key。harness 只把 DeepSeek、LangSmith 和 Prometheus 凭据读进进程环境，ledger 只记录环境变量名。gitleaks 扫描结果见提交记录。
