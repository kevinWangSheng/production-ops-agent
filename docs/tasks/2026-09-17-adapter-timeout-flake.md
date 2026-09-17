# SyntheticAdapter 物理超时测试偶发失败

- 状态：已完成
- 更新日期：2026-09-17
- 依据：调度者派发的修复任务；线索来自
  `/private/tmp/claude-501/-Users-shenghuikevin-dev-AI-production-ops-agent/a7d3abb1-8221-4812-bafb-316b8b11e6aa/scratchpad/reports/seam-32.md`
  「make check」节
- 工作区：`/Users/shenghuikevin/dev/AI/production-ops-agent-flake2`，分支
  `fix/adapter-timeout-flake`（自 `origin/main`，调度者已建）

## 目标与范围

修复 `tests/test_m0_adapters.py::test_physical_timeout_retains_unknown_and_no_tools`
在全量 `make check` 高负载下偶发失败（单独重跑通常过）。范围限定在定位真实根因并
修复，不削弱「物理超时必须 `retain_unknown` 且不执行任何工具」这条断言，不改
`scripts/m0/adapters.py` 生产逻辑（除非证据表明它本身有 bug）。

## 前提与完成条件

- 前提：worktree 由调度者创建，起点为 `origin/main`，接手时工作区干净。
- 完成条件：判断根因属于「测试对时序的假设不成立」还是「适配器在超时后仍发送」；
  修复后循环验证 0 失败；`make check` 结论行记录；全新上下文只读子代理独立审查；
  PR 开启并等待 CI。

## 执行进展与证据

### 根因判定：测试对时序的假设不成立（不是适配器超时后仍发送）

**重要更正**：调度者转述的失败方向（"期望 `['reserve','unknown']` 实得
`['reserve','send','unknown']`"）与实际断言方向相反。测试源码
（`tests/test_m0_adapters.py:333`）是
`assert [e[0] for e in events] == ["reserve", "send", "unknown"]`——pytest 失败
输出里等号左边永远是"实际算出的值"，右边是硬编码的期望值。实测复现的失败信息是
`assert ['reserve', 'unknown'] == ['reserve', 'send', 'unknown']`：**"send" 从
实际记录里缺失了，不是多出来的。** 已用独立子代理复核此判断（见下方「独立审查」）。

1. **定位**：`scripts/m0/adapters.py:112-130`——`SyntheticAdapter.round` 在
   `async with asyncio.timeout(seconds):` 作用域刚一进入就置 `sent = True`
   （第 130 行，紧接着才 `await client.chat.completions.create(...)`）。而
   mock transport 的 `handler` 只有在 `create()` 内部真正把控制权交给
   transport 时才会执行第一行 `events.append(("send",))`。测试用
   `timeout=0.01`（10 毫秒）——这段"进入 timeout 作用域"到"handler 拿到调度、
   记录 send"之间要经过 OpenAI SDK 与 httpx2 内部若干层 `await`。
2. **实测证实是一次性冷启动成本，而非适配器逻辑问题**：用独立脚本
   （`scratchpad/profile_cold.py`）对同一调用链分段计时，全新进程下：
   `httpx2.AsyncClient`/`AsyncOpenAI` 构造均 <1ms，但**`client.chat.completions.create()`
   本身首次调用耗时 190–294ms（无额外压力）、420–743ms（16-worker CPU 压力
   下）**——这是 OpenAI SDK 请求/响应模型的 pydantic-core 校验器 schema 在进程
   内首次使用时才构建（惰性、构建后按类缓存，构建时是纯 CPU 计算，会与其它
   进程抢时间片），与 mock handler、与适配器本身的超时/取消逻辑都无关。
   `remaining_seconds()` 用的 `clock=lambda: NOW` 是冻结的业务时钟，只影响算出
   的秒数（恒为 0.01），`asyncio.timeout(seconds)` 内部用的是真实 wall-clock，
   不受这个 mock 影响。
3. **决定性验证**：`scratchpad/repro_adapter.py`——同一进程内先做 1 次"热身"
   调用（宽松 timeout=2.0，走到底），再跑 100 次原始 `timeout=0.01` 的调用，
   在观测到 `uptime` load average 高达 36 的压力下，**100/100 全部通过**；
   不做热身、直接跑 50/100 次原始调用，稳定出现 1 次 `['reserve','unknown']`
   （约 1-2% 失败率，无压力时已可复现，非必须极端负载）。
4. **排除"适配器超时后仍发送"**：全部观测到的失败签名只有
   `['reserve','unknown']`（send 缺失）与 `['reserve','send','unknown']`
   （正常），从未出现过 send 之后还有额外事件、或 send 重复、或
   `settle`/未取消完成的迹象；`scripts/m0/adapters.py` 里 `sent` 标记与
   `except` 分支的判定逻辑本身未改动也无需改动。
5. **"单独重跑必过"的真实含义已核实并不等于"隔离运行天然安全"**：用
   `pytest -k` 单独反复跑这一个测试（`scratchpad/loop_isolated.sh`，每次都是
   全新进程、必然"冷"），在本机真实压力叠加合成压力（`uptime` load average
   12–22）下**连续 40 次全部失败**，签名与线索报告完全一致
   （`assert ['reserve', 'unknown'] == ['reserve', 'send', 'unknown']`）。这
   说明"单独重跑通常过"只是因为历史上手动复测时机器负载已经降下来，不是隔离
   运行本身有免疫力；同一进程内跑整个 `tests/test_m0_adapters.py`
   文件（该文件内此测试之前已有十余个测试成功调用过
   `adapter.round`/`create()`，顺带把 pydantic-core schema 缓存热身）在同样
   16-worker 压力下连续 5 次全部通过——这从另一个角度印证了"冷/热"是决定性
   变量，而依赖"文件里恰好有更早的测试顺便热身"是脆弱的隐性假设，不是稳健
   的测试设计。

### 修复（仅 `tests/test_m0_adapters.py`，未改 `scripts/m0/adapters.py`）

在测试函数开头显式加一次热身调用（复用文件已有的 `setup()`/`execute()`
辅助函数，构造一套完全独立的 `adapter`/`events`/`budget`/`run`/`history`，
用默认 `timeout=5` 走一次正常、立即返回的完整往返），把一次性 schema 构建成本
挤到真正的紧凑超时窗口之外；同时把该测试自身的 `timeout` 从 `0.01` 提到
`0.1`（仍是 mock handler `asyncio.sleep(1)` 的 1/10，继续满足"取消发生在响应
仍然挂起时"的语义），作为对残余的一般性 OS 调度抖动的纵深防御。断言
`["reserve", "send", "unknown"]` 原样保留，未做任何削弱。

### 循环与压力验证

- **红态确认**（`git apply -R` 无损切回原始代码）：`scratchpad/loop_isolated.sh 40`
  在 16-worker CPU 压力（`uptime` load average 12–22）下，**40/40 全部失败**，
  失败信息与线索报告完全一致。
- **绿态确认**（`git apply` 换回修复）：同样 40 次孤立冷进程 + 同等压力，
  **40/40 全部通过**。
- 按任务标准的朴素 30 次循环（无额外压力）：`pass=30 fail=0 total=30`。
- 整个测试文件：`pytest tests/test_m0_adapters.py` → `29 passed`（隔离单测通过
  之外，确认未影响同文件其它 28 个用例）。
- `make check` 连续两次，结论行：
  `1050 passed, 75 skipped, 2 xfailed`（第一次 30.33s，第二次 26.77s）；
  `ruff check`："All checks passed!"；`ruff format --check`："410 files
  already formatted"；`mypy`："Success: no issues found in 13 source files"。

## 独立审查

全新上下文只读子代理（general-purpose，未参与本次实现/调查）审查结论：
**APPROVE**，无阻断问题。逐条：

1. 隔离性：`setup()`（行 106-125）每次调用都全新构造
   `adapter/run/history/fixture/events/bodies`；`execute(*setup()[:4])`
   只按位置传参，不绑定任何变量，热身调用与真实断言之间没有共享可变对象——
   真实测试的 `events = []` 是热身返回之后另起的新列表。
2. 热身命中同一条代码路径：`setup()` 的 handler 立即返回完整 SSE 流，
   `execute()` 用默认 `timeout=5` 跑 `adapter.round(...)`，会真正走到
   `scripts/m0/adapters.py:131` 的 `client.chat.completions.create()`——
   该文件本次改动为空（`git diff scripts/m0/adapters.py` 为空），确认热身
   命中的是同一处生产代码。
3. 断言语义未削弱：`sent=True` 仍在进入 `asyncio.timeout` 作用域后、
   `create()` 之前设置（`adapters.py:129-131` 未改）；`remaining_seconds()`
   在冻结时钟下把 `timeout` 原样传给 `asyncio.timeout`；新值 0.1s 相对
   handler 1s 的 sleep 仍有 10 倍余量（原 0.01s 是 100 倍），"取消发生在响应
   仍挂起"的语义不变，没有引入新的提前退出路径。
4. 新增失败面与现有约定一致：热身调用是裸 `execute(...)`（不接
   `pytest.raises`），如果正常往返本身出问题会在热身处而非断言处报错——这与
   文件里其它裸 `execute(...)` 调用（如 150-151、203、206、233、280、357、
   371 行）暴露失败的方式一致，不是新模式。
5. 生产代码确认未改：`scripts/m0/adapters.py` diff 为空，`sent`/`retain_unknown`/
   `settle` 判定逻辑逐行核对未变。
6. 风格：全文件其它地方都把 `setup()` 的六元组解包给具名变量，只有这里用
   `execute(*setup()[:4])` 的 slice-and-splat；审查方认为对"不需要断言、
   即用即弃"的热身场景是合理写法，注释已说明 pydantic-core 惰性 schema 构建
   的动机；改动最小、范围聚焦，无无关重构。
7. 独立运行 `ruff check tests/test_m0_adapters.py` 通过；核实
   `pyproject.toml` 的 `[tool.mypy] files = ["opspilot"]` 确认 `tests/` 本不在
   项目 mypy 检查范围内，项目实际配置的 `mypy`（无参数）调用
   "Success: no issues found in 13 source files"；强行对该测试文件单独跑
   mypy 会报 152 个改动前就存在、遍布全文件的 `no-untyped-def` 类提示（整个
   文件历来没有类型注解），与本次 2 行 diff 无关也非本次引入；独立复跑
   `pytest tests/test_m0_adapters.py` → `29 passed`。

无 AGENTS.md 范围/权限问题：纯测试文件改动，未涉及生产/数据库/凭据/网络边界。

## 下一步与交接

- 提交 `23ea89c`（单一改动：`tests/test_m0_adapters.py` + 本任务记录），已推送
  任务分支 `fix/adapter-timeout-flake`，已开 PR
  [#38](https://github.com/kevinWangSheng/production-ops-agent/pull/38)。
- CI 两项 checks（`checks`、`m0-postgres`）均 `SUCCESS`。
- Codex 机器人 code review 在提交 `23ea89c` 上留了 1 条 inline thread（P2）：
  指出当时任务记录的「下一步与交接」还写着"待独立审查结论补充/待开 PR"，与
  文件头部已标「已完成」矛盾。**采纳**——该问题在后续提交 `bcf0b47`
  （更新独立审查结论、PR 号、CI 状态）里已经修复，已在线程回复具体修复提交号
  并 `resolveReviewThread`。分支保护要求 `required_conversation_resolution`，
  解决前 `mergeStateStatus` 为 `BLOCKED`，解决后恢复 `CLEAN`。
- 当前：`mergeStateStatus: CLEAN`、`mergeable: MERGEABLE`，CI 全绿，无未处理
  review thread。
- 状态：**PR 已就绪，待用户审核合并**。合并授权、合并动作及合并后清理留给
  用户按默认流程处理，本任务未合并、未删除分支。
- ROADMAP 无需更新（工程维护，不改变任何功能门槛/验收状态）。
