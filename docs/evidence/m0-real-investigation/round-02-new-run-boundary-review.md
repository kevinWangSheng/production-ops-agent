# PR16 new_run 版本与控制边界独立复验

日期：2026-09-10 UTC。独立审查者 `/root/new_run_boundary_review` 未参与实现，以新上下文接收原始发现与合同；只编辑本记录，不修改实现、测试或 Git index。

## 范围与结论

依据当前 SPEC（M1 gate 仍为 not cleared）、C3 §5/7、ADR-0003、M0 恢复工作包及首流程 v4 冻结合同，复核远程发现 `3978285916`、`3978285917`：新 Run 不得在缺失版本身份时取得执行权；最后 accepted new_run 不得沿用旧 cancel/correct 的最终状态。

两项问题均现场复现，冻结修复后本有界复验 PASS，无本范围未处理 P1/P2。结论只覆盖离线合同及专用真实 PostgreSQL 合成机制；没有模型、供应商、trace 或生产调用，没有读取历史私有协议字段或凭据。共享宿主和独立上下文不证明 OS 隔离。原真实报告质量 FAIL、M0 未完成、M1 gate 关闭与 feature passes 均不变。

工作区：`/Users/shenghuikevin/dev/AI/production-ops-agent-m0-01`，分支 `chore/m0-02-convergence`。接手基线已推进到 `0e44dc7fcff41017b8f82ea8ddeb610d65a0b777`；本次实际覆盖下面列明的工作树 SHA-256，而非只凭旧提交号。其他作者的初始时钟/输出文件变更未改动。

## 修复前独立证据

- 在 root 已启动并核实身份的专用 PG（`127.0.0.1:55431 / m0_budget / m0_lab`）新建随机 experiment `e3f7b267-35f5-4476-ae53-d531a7294672`，用合成业务输入调用 `accept(..., {'state':'v3'})`，随后 `new_run(..., {})` 返回 generation 1，`claim(..., {})` 返回 epoch 1。证明空版本可持久化并获得执行权。所有原行保留。
- `.venv/bin/python -m pytest -q tests/test_m0_outcomes_v4.py -k latest_new_run`：**2 failed / 6 passed / 37 deselected**；cancelled 与 waiting_human 两个反例均返回空违规列表。
- `M0_STEP_POSTGRES=1 .venv/bin/python -m pytest -q tests/integration/test_m0_step_store_postgres.py -k empty_versions`：**1 failed / 26 deselected**，失败原因为 `DID NOT RAISE BudgetError`。

作者编辑期间另一次组合测试与文件保存交错，未作为冻结版本的回归结论；最终重新核对源码 hash 后运行下列检查。

## 冻结修复后的独立检查

命令与实测结果：

```text
M0_STEP_POSTGRES=1 .venv/bin/python -m pytest -q tests/integration/test_m0_step_store_postgres.py -k 'empty_versions or fencing_intake_versions or current_control_snapshot or own_limits or control_clears_active_final'
8 passed, 19 deselected in 1.02s

.venv/bin/python -m pytest -q tests/test_m0_step_store_versions.py tests/test_m0_outcomes_v4.py
70 passed in 0.44s

shasum -a 256 scripts/m0/step_store.py scripts/m0/outcomes_v4.py tests/test_m0_step_store_versions.py tests/test_m0_outcomes_v4.py tests/integration/test_m0_step_store_postgres.py
```

核查内容：

- accept/new_run/claim 共用事务前校验：空 map、None、list、非字符串或空 key/value 均 INVALID_INPUT。非空具体版本集合仍由调用合同确定，本修复不声称验证版本字符串所代表的软件真实性。
- 真实 PG 拒绝空版本后，subject summary、预算 snapshot 不变，新 run/run_input 行及 accepted new_run audit 均不存在；同一 fresh Run 随后使用合法版本可接受并领取，旧 fence 发布仍拒绝。测试以新随机 experiment/subject 操作，保留历史行。
- 合法但不同的版本仍触发 INCOMPATIBLE_STATE 与 blocked，原累计预算不被新 Run 重置；人工控制清除当前 final 而历史报告保留；控制快照排除 rejected/non-control audit。
- v4 末次 new_run 拒绝 cancelled/waiting_human，同时允许 running/paused/blocked/failed/budget_exhausted/completed；new_run 后实际 cancel/correct 仍接受对应状态；历史 cancel/correct 后显式 new_run 可以完成。未永久封禁历史上曾取消的主体。

源码与测试 hash：

| 文件 | SHA-256 |
| --- | --- |
| scripts/m0/step_store.py | a807d26c229df4f9e220e533bea94d9fadd6f59893b636555a57fe2b1362b35d |
| scripts/m0/outcomes_v4.py | b480812add96056e246520bffd3c88093e263265b47feb67ce0fcff420265d24 |
| tests/test_m0_step_store_versions.py | 1c267b32b0958a43a27d0399dd1f031bea01a54f04cf3998b6cb65ef41a0c511 |
| tests/test_m0_outcomes_v4.py | c9a00009e2c47528b386b3c2743df74eca1015b20942a0d02c16dc8cd5f84a48 |
| tests/integration/test_m0_step_store_postgres.py | 3a9ee28d431ad55895ef7c5ebb6c8f78c6aa43ad7fe2e98509524d3867c1d788 |

## 交接边界

PG 的启动、身份核查及停止由 root 统一负责，本审查者未执行生命周期命令。已通知 root 所有 PG 检查结束，可以停止；停止结果由 root 后续核实。本记录不替代最新 PR CI、远程复审或用户合并授权，不认证完整产品状态机或真实报告质量。
