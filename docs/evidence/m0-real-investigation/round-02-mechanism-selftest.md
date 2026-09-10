# M0-02 机制实现者自测与真实调用接口交接

日期：2026-09-10。状态：**实现者自测完成，独立复验进行中；不代表产品实施入口或主动故障调查通过。**

工作区 `/Users/shenghuikevin/dev/AI/production-ops-agent-m0-01`；没有执行真实模型调用或 trace 上传，没有起停 PG，没有读取 `.env` 或实际私有推理正文。PG 由父执行者启动并确认原实验目录；本执行者仅安装新增 `m0_v3_*` 表、创建随机隔离实验记录。旧实验记录不更新、不删除；全局历史保全核查由父统一完成。

## 原命令与结果

使用既有主仓库 `.venv`，没有 setup/install：

```sh
M0_STEP_POSTGRES=1 /Users/shenghuikevin/dev/AI/production-ops-agent/.venv/bin/python -m pytest tests/integration/test_m0_step_store_postgres.py tests/test_m0_outcomes_v3.py tests/test_m0_outcomes.py tests/test_m0_budget.py -q
```

最终本轮实现者输出：`104 passed in 5.90s`，其中 12 项真实 PG 机制、4 项 v3、其余为原 v2 与预算回归。PG 用例使用实际 spawn 子进程、`os._exit(17)` 中断和新进程恢复；没有用线程替代跨进程重建。

```sh
M0_B_POSTGRES=1 /Users/shenghuikevin/dev/AI/production-ops-agent/.venv/bin/python -m pytest tests/integration/test_m0_budget_postgres.py -k 'not database_restart' -q
```

输出：`9 passed, 1 deselected in 1.85s`。有意不执行重启 PG 用例，遵守父为唯一环境生命周期操作者。预算变更仅提取可共享业务事务的 helper，沿用原账本。

定向 Ruff check/format 已执行；最后 check 为 `All checks passed!`。此处是实现者证据，独立审查结果另由审查者提供。

## 保留失败与修复

1. 第一轮真实 PG 为 `1 failed, 4 passed in 2.90s`：`test_actual_process_cut_before_response_retains_reservation` 的临时 `CTX.Queue()` 在 macOS spawn 重建前被回收，子进程 `FileNotFoundError` 来自 `multiprocessing.synchronize.SemLock._rebuild`，未到业务断点。保留 Queue 引用后同组 `5 passed in 3.05s`。
2. 独立审查复现：预算事务期间 lease 过期仍发起请求。修复为预算提交后保持共享 session advisory lock，在实际 starter 前再次用数据库时钟完整检查 fence/lease/deadline；拒绝不退预留。模型、工具均有 `pg_sleep(0.2)` 越过 lease 的回归。
3. 独立审查复现：取消后不兼容 claim 可覆盖 cancelled 为 blocked。修复为先检查可领取状态/期限/已有 lease，再处理版本兼容；保留取消状态回归。
4. 独立审查复现：工具没有 dispatch 也可提交。`commit_tool` 必需 attempt_id，并绑定操作、generation/owner/epoch 和最新物理尝试；缺失、错操作和旧尝试拒绝。模型响应同时补 request_id 相同约束，避免同逻辑步骤重试时混用旧响应。
5. 独立审查复现：初始 view 未经过动态 checker 校验。新增初始 raw/投影/scope 检查，并在 `investigator_input()` 出口拒绝无效初始证据。

## 给父执行者的受限 PG 接口

真实 HTTP 与全轮文件账本由父统一操作。本模块不提供 HTTP client。共享文件账本固定为环境 worktree 的 `tmp/m0-environment/m0-02-request-ledger.json` 与同名 `.lock`；phase `pg`，额度依据 round-02 authorization/profile，不能重新初始化总账。旧 24 CNY 未核账保留。

`PostgresBudget` 现有表明确 `synthetic=true`；下面的 PG 预留是有界机制镜像，真实费用/次数权威仍由父共享 wrapper 执行。不要将镜像称为新的真实全轮总账或修改旧表 CHECK。父须同时通过两个限制；跨 PG 与文件系统不承诺原子事务，失败时保守保留预留。

```python
from datetime import datetime
from uuid import uuid4
from scripts.m0.budget import PostgresBudget
from scripts.m0.contracts import RunContext, RequestIdentity
from scripts.m0.step_store import StepStore

ledger = PostgresBudget(EXISTING_LAB_DSN)
store = StepStore(ledger)
store.install()  # 仅 additive m0_v3_*；不安装或启动服务器
run = RunContext(EXPERIMENT_UUID, RUN_UUID, "deepseek", ABSOLUTE_DEADLINE)
# 父显式创建新实验身份；微 CNY 数值只是机制镜像，不替代共享真实总账。
ledger.initialize(run.experiment_id, 3_500_000, run.deadline)
subject = store.accept(run, "round02-pg-intake", BUSINESS_INPUT, VERSIONS)
fence = store.claim(subject, run, uuid4(), VERSIONS, lease_seconds=LEASE_SECONDS)
request_id = uuid4()
step, handle = store.dispatch(
    fence,
    "fixture",
    0,
    EXACT_INPUT_SNAPSHOT,
    request_id,
    3_440_640,
    4,
    START_PHYSICAL_REQUEST_AND_RETURN_HANDLE,
)
# starter 返回前必须已经实际发起；不能返回一个尚未执行的协程或排队任务。
assistant, usage = WAIT_HANDLE_WITH_BOUNDED_SUPERVISOR(handle)
assert store.commit_response(fence, step, assistant, request_id=request_id)
# response/private 只能程序流转，严禁 print/log/trace/交给审查者读取。
attempt_id = uuid4()
store.dispatch_tool(fence, step, 0, attempt_id, TOOL_LIMIT, START_FIXED_READ)
assert store.commit_tool(fence, step, 0, TOOL_MESSAGE, attempt_id=attempt_id)
# 完整 usage 经父校验后可按微 CNY 上界 settle；缺失则 retain_unknown。
ledger.settle(RequestIdentity(run, request_id), VERIFIED_USAGE_UPPER_MICROCNY)
```

第一进程退出后，第二进程等待原 lease 自然到期，使用相同 subject/run 与新 owner `claim`，不创建新的 Run。调用 `rebuild(fence, step)`：

- `retry_model`：无完整已提交响应，输入快照保留，重试必须新 physical request ID，未知预留不减。
- `pending_tools`：返回待完成操作 ordinal/call；已提交结果不重采。
- `ready`：返回受限 `input` 与完整配对 `messages`，仅同 provider/Run 续传。第二请求输入由已提交快照的初始消息加这些完整组构成；不读取/打印私有正文。

第二请求使用新 logical round 与 request ID，完整最终 JSON 响应提交后，先做外部结果合同检查，再 `publish(fence, parsed_candidate, step=final_step)`。publish 要求对应已提交的 JSON final、当前 fence 与幂等候选；它本身不判断因果正确性。只输出 `summary(subject)` 的安全元数据及经白名单处理的业务判据。

**最初接口交接时的 transport 风险：**线程握手超时后若线程仍可能发起请求，释放 PG 锁会破坏取消顺序；必须确保超时传输已终止、已回收。单纯 `Thread.start()` 或 `create_task()` 不是 actual initiation。该风险随后由父明确选用可终止子进程，授权本执行者准备下述脚本；没有执行付费调用。

## 后续授权的真实 PG driver 准备

新增 `scripts/m0_pg_live_probe.py` 与 `scripts/m0_pg_private_transport.py`。独立审查发现父硬退出后的子传输迟发窗口，父随后明确授权小型锁移交改动；核心新增 prepare_request/send_guard，不建设平台。每 stage 需父显式 `--execute`，父仍负责外层 supervisor 与 PG 生命周期。脚本内部：

- phase `pg` 复用 round02 全轮 `.lock`/Budget；PG 镜像与文件账本非原子，失败只保守占用。
- 独立 transport 进程固定官方 URL、HTTP/1.1、无 retry、无重定向、无环境代理；仅程序从主 `.env` 取认证 key。
- 父 prepare_request 只持久预留；实际 transport 子进程自行获取同 PG session lock、核当前 fence/实际 body 快照 hash、原子消费一次性 send grant。子进程在 headers/body.started 再核 lease/deadline，body.complete 后释放锁并通过专用 pass_fds 管道发信号。父最多等 10 秒；没有信号先 terminate/kill/wait 回收子进程才报错。父 SIGKILL 不会把子进程变成无锁发送者。
- response 通过私有 stdout pipe，大小限制 2 MiB，响应内容从不作为日志打印；实际隐藏推理仅程序保存/恢复。
- first stage 固定一次 read_fixture 完整响应与工具结果提交，退出；second stage 新 PID 等原 60 秒 lease 自然过期，从 PG 重建配对消息并调用 JSON final。响应等待期间每 10 秒条件续租；每阶段/Run/绝对 deadline 均保持。
- 第二阶段的最终验证仅精确 synthetic target/evidence/status，不声明主动故障调查成功。
- response model 从父批准的 round02.PROFILE reported_models 双项集合与 provider metadata hash 读取，无其他默认；请求仍固定 v4-flash。完整 bounded wire body 在解释前保存到受限 PG，错误模型拒绝且保留；代码/profile/hash在两stage之间核对。
- 全轮文件锁的 reserve/finish 都有期限；拿不到结算锁保留 unknown，不重置费用。PG镜像子额度按父后续分配为3.5 CNY/4 HTTP，实际全轮权威仍在统一文件账本。

离线验证命令：

```sh
/Users/shenghuikevin/dev/AI/production-ops-agent/.venv/bin/python -m pytest tests/test_m0_pg_live_probe.py -q
```

输出 `3 passed in 0.77s`：实际子进程发送事件/私有 pipe、无发送事件时先回收子进程、无效 body 的 worker 退出且零 stdout/stderr（在读取配置之前）。没有真实 HTTP，也没有使用凭据。此 driver 待独立审查后由父执行：

```sh
/Users/shenghuikevin/dev/AI/production-ops-agent/.venv/bin/python -m scripts.m0_pg_live_probe first --record <父指定的独立临时记录路径> --execute
/Users/shenghuikevin/dev/AI/production-ops-agent/.venv/bin/python -m scripts.m0_pg_live_probe second --record <同一记录路径> --execute
```

后续扩展回归输出 `112 passed in 7.69s`（整组含6项driver离线、14项真实PG及v3/v2/预算）：新增实际父SIGKILL、取消事务提交、孤立子传输放行的交错，独立本地HTTP监听确认零请求且预留保留；one-use grant与实际bodyhash绑定；错误response model原始响应保全；finish文件锁deadline。该批第一次运行有一个测试替换笔误 `str(..., fence=...)` 导致TypeError（16 passed/1 failed），修正测试后完整通过；没有产生外部请求。

## 覆盖边界

本批证明的对象是版本化 DTO、真实 PG 下的有界步骤/预算/控制机制；尚不证明实际 DeepSeek 跨进程续传、主动故障因果结论、完整 Observer、真实数据库进程重启、完整升级部署或产品验收。真实组合、全轮账本、环境保全、最终独立复验与入口决定由父统筹。
