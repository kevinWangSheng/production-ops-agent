# #527 有界真实 Run：模型请求在途时杀 worker，接管后遗留预留结算为 unknown

- 日期：2026-11-22 19:45–19:53Z；分支 `fix/527-orphan-reservations`，修复提交 `682f14f5`
- 合同：[B3 预审](../../tasks/2026-11-22-next-batches-b3-prereview.md) B3-R1、R2、R5
- 类别：有界真实运行。真实 DeepSeek、真实 LangSmith（`OPSPILOT_TRACE=lab`），工具是 fixture profile 的 Prometheus 回放，真实 `kill -9`。不是生产证明
- 冻结：[summary.json](summary.json)（`scripts/run_freeze.py`），[reservations-psql.txt](reservations-psql.txt)。原始 ledger 在被忽略的 `tmp/lab-ledgers/527-orphan-reservations/20261122T194526Z/`，不入库，sha256：events.json `24c64049…`、worker-a.log `601c8af0…`、worker-b.log `7ecd0e33…`、web.log `b66c69ff…`
- trace（接管后 epoch 2 的根 run）：https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/5df3f14f-a2a2-492c-9d63-27747d60ec96/r/00000000-0000-0000-1e5b-8020c713f2a3 。epoch 1 进程被 SIGKILL，它的根 run 没有读回（`read_back` 只找到 1 个根）

## 与 B3-R5 的偏差

B3-R5 写的是本地 Compose 加 `docker kill` worker 容器。这里改用本机进程：临时 PG 17（127.0.0.1:55527，库 `live527`，迁移到 `0028_runbook_imports`，运行角色 `opspilot_web_lab`/`opspilot_worker_lab`），`python -m opspilot.web serve` 加两个 `python -m opspilot.worker_main`（worker 用 worker 角色连库）。原因是本机 swap 接近用满，kind 实验没在运行，不起 Compose 栈。两种做法都是对 worker 进程发 SIGKILL，claim 走的都是同一条 worker 角色路径。

## 步骤（驱动脚本在仓库外的 scratchpad 里）

1. 起 web 和 worker A，经 `POST /intake/ui` 提交一个事故（目标 `checkout-prod`）
2. 用 owner 连接每 0.1 s 读行。kill 条件：已有 1 个以上 step，恰好一条预留处于 `reserved`，并且连续 1 s 不变。kill 前核对 PID 的父进程是驱动、命令行含 `-m opspilot.worker_main`，然后 SIGKILL
3. 起 worker B，等租约（420 s）过期后它 claim 出 epoch 2，Run 跑完

## 结果

| 时点 | Run 列 reserved / spent / unknown | 被杀那条预留 `88120813…` |
|---|---|---|
| kill 时（19:45:30Z，第 2 次模型请求在途） | 1 / 1 / 0 | `reserved`，`seconds=NULL` |
| kill 后 1 s | 1 / 1 / 0 | `reserved` |
| 接管（19:52:31Z，epoch 2） | 1 / 1 / 1：遗留预留移到 unknown，第 3 次请求的新预留记为 reserved | `unknown`，`seconds=360`（= `reserved_seconds`） |
| Run `completed`（19:52:47Z） | 0 / 2 / 1，总数 3 = 3 条预留行 | `unknown`，`seconds=360` |

结束时该 Run 没有一条预留还是 `reserved`（`still_reserved = 0`，见 psql 结果）。worker B 和 web 的日志里没有 ERROR。

## 费用

按 token × 官方单价（deepseek-flash 每百万 token 命中 $0.003、未命中 $0.15、输出 $0.60，https://api-docs.deepseek.com/quick_start/pricing）。summary 的 `tokens` 只含 2 个有用量记录的轮次：命中 4608、未命中 480、输出 3451，约 $0.0022。被杀的那次请求没有用量记录，未计入，按同量级估计再加约 $0.001。合计约 $0.003。
