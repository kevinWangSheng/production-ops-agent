# C1–C5 代码批次全新上下文独立复审

日期：2026-09-11  
受检 worktree：`/Users/shenghuikevin/dev/AI/production-ops-agent-m0-01`  
受检分支/HEAD：`chore/m0-02-convergence` / `b691a1d7622bcaa750a8bc214d590b9992509433`  
复审者：全新上下文独立 Agent（未参与 C1–C5 实现）

本记录只复审 C1–C5 五个提交及其测试/evidence，不修改生产代码、
`feature_list.json`/passes、SPEC gate 或历史运行证据。开始时 worktree 已有
协调者未提交的 B3/first-investigation-v4 文档修改，予以保留。未读取凭据，
未运行模型、trace、OTel/Holmes 服务或 M0-OTel；仅运行离线定向测试和 Ruff。

## 依据与范围

- `SPEC.md` 的证据真实性、只读工具、绝对 query deadline、超时/预算/取消、
  人工控制与验证分层要求；实现门槛仍为 not cleared。
- `docs/testing/first-investigation-v4-2026-09-10.md` 的初始 raw/view/hash
  绑定、来源更早 deadline 优先、完整协议和失败保留合同。
- C1 `c801c6f`、C2 `0205e63`、C3 `5de2a8c`、C4 `b733a0c`、C5 `46ac8d8`，
  以及汇总测试提交 `b691a1d`。

## 代码核对

### C1 — imported raw evidence 的 hash 分支

`outcomes_v4.py:625-652` 将候选副本与已验证 view 按 evidence id 绑定；只有
候选对象本身不等于完整 trusted view 时，才尝试对 compact、带换行和
`indent=2 + newline` 三种 JSON 字节表示计算 SHA-256，并与 trusted
`raw_hash` 比较。任意 hash 不匹配仍返回
`INITIAL_INPUT_REASSEMBLY_MISMATCH`。这条依据与 `initial_evidence.py` 中
`raw_file_sha256`/`Artifact.raw_hash` 的来源一致，未发现放宽为“同 id 即信任”。

已有正例覆盖完整 trusted business view append；负例覆盖 tampered raw
record 与 tampered view。注意：C1 专项正例没有真正把“与原 raw 文件字节匹配、
但不是 view 形状”的候选送入 `raw_hash_matches` 分支，因而该新分支本身仍是
静态核对，未取得直接回归证据。这是测试覆盖缺口（P2），不是本次静态检查发现
的 provenance 放宽。

### C2 — lock 后 deadline 重检

`holmes_baseline.py:747-759` 在取得 `tool_io_lock` 后调用
`tool_remaining()`；该函数 (`:102-116`) 重新读取时钟，把
`scope_deadline`、单工具/累计工具预算、Run 截止时间取最小值，并在来源期限
不超过 4 秒时返回明确的 `query authorization deadline reached`。因此先通过
pre-lock 检查、再等待锁跨过来源 deadline 的路径会在 GET 发出前被拒绝，未跨过
时该来源期限也会传播给 child wall timeout。

专项测试覆盖了 monkeypatch 时钟跨过 deadline 和“来源剩余 <4 秒”分类；但没有
直接让真实 `tool_io_lock` 持有者阻塞另一个调用并观察 GET 未发出。该缺口与实现
边界分开记录，不把定向函数测试称作锁竞争运行时证明。

另一个可观察边界：当 `run_stop`（而非来源 deadline）只剩 ≤4 秒时，
`tool_remaining()` 仍抛出 `TimeoutError("tool total deadline")`。当前代码没有
分别分类 Run deadline；这是现有错误语义的残余问题，见“发现”。

### C3 — known boundary codes

`KNOWN_BOUNDARY_CODES` 已从异常处理局部变量提升为模块常量，且新增
`query authorization deadline reached`；对应回归断言存在。异常处理仍只把常量
中出现的文本放入 `boundary_error_codes`，不会把任意 provider 异常原文导出。

### C4 — bool query deadline

`effective_query_deadline()` (`holmes_baseline.py:85-93`) 先显式排除 bool，再
验证 int/float 和 finite，最后取 profile ceiling；`True` 的负例通过，避免 Python
`bool` 作为 `int` 静默成为 1 秒授权。现有测试未单独覆盖 `False`，但它同样被
显式拒绝，静态路径一致。

### C5 — tool-call id

`step_store.py:26-38` 在遍历前要求 assistant 为 dict、`tool_calls` 为 list，
且每项为 dict、`id` 为非空 string；不满足即 `TOOL_CALLS_INVALID`。后续
`protocol.continuation()` 继续校验 function/name/arguments、id 唯一性和结果
配对。新增 5 个 malformed container/id 负例与 1 个合法 identity-preserving
正例均通过；未发现把缺失/数值 id 送入后续索引的路径。

## 独立可复现检查

在上述 worktree 执行，均未触发模型、trace、OTel/Holmes、数据库或外部网络：

```text
./.venv/bin/python -m pytest tests/test_m0_holmes_bridge_v4.py \
  tests/test_m0_holmes_round02.py tests/test_m0_step_store_protocol.py
160 passed in 7.12s

./.venv/bin/ruff check scripts/m0_environment/holmes_baseline.py \
  scripts/m0/outcomes_v4.py scripts/m0/step_store.py \
  tests/test_m0_holmes_bridge_v4.py tests/test_m0_holmes_round02.py \
  tests/test_m0_step_store_protocol.py
All checks passed!

./.venv/bin/ruff format --check ...
6 files already formatted
```

仓库内汇总 evidence `round-03-batch-fix-final-check-green.txt` 另记录全量离线
套件 730 passed / 44 skipped；本次没有重复运行该较大套件。其早先红色 Ruff
输出和后续 green 输出均保留，不能用 green 覆盖历史失败。

## 发现与处置建议

### P2 — tool total / Run deadline 未被 known code 正确分类

`tool_remaining()` 在 `remaining <= 4` 且 `scope_remaining > 4` 时统一抛出
`tool total deadline` (`holmes_baseline.py:113-116`)；当实际较早边界是
`run_stop` 时也走同一分支。更直接地，`KNOWN_BOUNDARY_CODES` (`:72-82`)
根本不包含 `tool total deadline`，所以 `main()` 的
`boundary_error_codes` (`:1344-1346`) 对该失败返回空列表。这样会丢失一个已
发生的边界分类；建议后续按真正耗尽的 dimension 返回稳定 code，并把所有允许
公开的稳定 code 与回归测试成对维护。该问题不放宽权限或发出请求，但会损坏
失败可观察性/审计分类，当前不能称 C3 boundary-code 完整闭合。

独立只读调用的结果（`scope_deadline=1000, run_stop=103, now=100`，及
`tool_elapsed=PROFILE.tool_total_seconds`）均为
`TimeoutError: tool total deadline`，而该字符串在当前 `KNOWN_BOUNDARY_CODES`
中的匹配结果为 `[]`；这不是根据模型或外部服务推断。

### P2 — C1 raw-hash 分支缺直接正例；C2 缺锁竞争回归

两项实现的静态控制流与现有定向测试均通过，但：

1. 没有正例证明合法 raw 文件字节可通过 C1 新增 hash 分支，也没有覆盖三种
   序列化候选中恰好命中的实际归档格式；
2. 没有确定性测试实际持锁/释放锁的跨 deadline 并发路径。

在补齐前，这些是覆盖限制而非 M0/产品通过证据；不得将现有测试写成真实锁竞争
或 raw 导入运行时证明。

## 结论

C1/C2/C4/C5 的受检实现与既有约束相容，定向 160 项测试及 Ruff 通过；C3 的
query-deadline code 已被加入但 boundary taxonomy 仍有上述遗漏。复审**未发现
P1**，发现两项 P2（一个可观察分类缺陷、两个专项覆盖缺口）。本记录不改变
历史失败、M0 实施门槛、feature passes 或任何费用/权限授权；在修复分类并补足
相称回归后，仍须重新独立复验受影响范围。
