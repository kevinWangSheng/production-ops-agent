# PR16 第二组 P1：报告交付与 Run 子限额

基线：`1462e2d`。日期：2026-09-10。对应 CodeReview comments `3975937306`、`3975937313`。本记录是实现者修复与自测，独立复验另由父统筹。未提交/推送，未读取.env或provider私有内容，0模型/trace调用，未起停环境。父已有两份状态文档变更未触碰，旧真实账本、质量FAIL、快照及M1门槛不变。

**单位边界：**下面PG测试中的10/40/50/100是新建随机隔离替身实验的整数记账单位/预留，绝非本轮实际CNY或真实模型调用。全轮20请求测试使用pytest临时目录的独立离线账本；没有读取或修改真实授权总账。

## 3975937306：零证据完成结果缺少真实报告绑定

原checker只在遍历view/引用时核对report step/request，因此completed partial/inconclusive且claims/evidence为空时，无delivery或错ID也可得到无违例。新增16项最高seam用例先复现：2项合法空view通过，其余14项缺失/错Run/错step/错request/旧generation/未提交/重复交付均未得到应有的报告绑定拒绝。

修复独立于view数量计算matching delivery，统一用于报告采纳条件和可见证据集合。按父明确的相称规则：`execution == completed`或`assessment_status == completed`任一宣称完成，必须恰好1条匹配current Run、report_step_id、report_request_id、最终control_generation且response_committed的delivery。合法空view不被误拒。两维均不宣称完成的blocked/waiting_human/cancelled/budget_exhausted且incomplete handoff允许0条，不强制调用模型；未另加状态互斥。

回归覆盖两维分别/组合宣称完成及其合法空view、零delivery拒绝。旧v2不修改；已解决的new_run控制枚举和连续水位约束保留。

## 3975937313：错误地把 Run 子限额用于整个 experiment

核对[冻结首流程包](../../testing/first-investigation-v3-2026-09-10.md)：每Run4个物理模型请求/20工具；所属授权累计费用、次数和绝对deadline另行优先。原dispatch/dispatch_tool按experiment统计，却把结果与max_requests/max_queries（Run子限额）比较，导致显式new_run仍被旧Run耗尽的子额度拒绝。

只把子限额计数键改为不可变`ModelStep.run_id`，不使用会变的subject.current_run，不删除历史计数或预留。保留实验锁、PostgresBudget累计费用/unknown和绝对期限；实际全轮20HTTP仍由现有round02 Budget共享授权guard执行，本修复不另建总账。

真实PG回归（父启动原专属PG，本执行者只增随机记录）：原Run用满4个模型替身请求/20次工具替身尝试，同Run换owner/epoch仍被子限额拒绝；新Run恢复自己的子额度，允许1个模型替身请求与1次工具尝试。实验累计仍为5/21条历史，unknown=10、其他reserved=40，共50个**替身账本单位**。新Run还有本地次数余量时，追加60单位仍被100单位实验总额度拒绝；延长到原experiment deadline之外仍拒绝，原绝对deadline不变。修复前该用例在新Run模型入口实际失败`REQUEST_LIMIT`（1 failed）；独立审查者另已复现工具入口`QUERY_LIMIT`。修复后两入口及全部保留约束均通过。

另用全新的临时文件账本模拟5个Run各4次请求并逐次重载；第21次新Run请求仍被现有全轮guard拒绝。模拟usage为零只为隔离次数判据，非实际费用测量；原previous-allocation占用标记保留，未触及真实总账。

## 实际验证

红阶段命令：

```sh
/Users/shenghuikevin/dev/AI/production-ops-agent/.venv/bin/python -m pytest tests/test_m0_outcomes_v3.py -k completed_report_requires -q
M0_STEP_POSTGRES=1 /Users/shenghuikevin/dev/AI/production-ops-agent/.venv/bin/python -m pytest tests/integration/test_m0_step_store_postgres.py -k new_run_has_own_limits -q
```

第一条14失败/2通过，第二条1失败（REQUEST_LIMIT）。修复后v3全30项通过；同PG新用例1 passed/17 deselected，临时总账跨Run用例1 passed/9 deselected。最终相称组合：

```sh
M0_STEP_POSTGRES=1 /Users/shenghuikevin/dev/AI/production-ops-agent/.venv/bin/python -m pytest tests/test_m0_outcomes_v3.py tests/test_m0_outcomes.py tests/test_m0_holmes_bridge.py tests/integration/test_m0_step_store_postgres.py tests/test_m0_pg_live_probe.py -q
```

输出`152 passed in 8.77s`；定向Ruff `All checks passed!`，`git diff --check`退出0。PG由父唯一负责关闭。上述是合成合同/真实PG机制及离线总账验证，不是重新运行真实模型或改变既有报告质量。

稳定文件SHA-256：
- `scripts/m0/outcomes_v3.py` `ab5fbe76ff01575c5ec2250417c38c29c4e577461eb9909c8312baf7cbaf3a8d`
- `scripts/m0/step_store.py` `3e0ea6a3f56f63e982b608e52c73007089c42e4ebafdd9c2e5a3aa9b41e6e255`
- `tests/test_m0_outcomes_v3.py` `b12708112a3bf600a941044f44765dd44e5c841d475cfcd05743f7a150ca69ed`
- `tests/integration/test_m0_step_store_postgres.py` `04f8e9bff8fee23ca8809961f0705615a74e486a00857f4817f984e7fdae2271`
- `tests/test_m0_pg_live_probe.py` `89ff36628074fa0d920c4a8aa65f8c5910489c24ab74cdab1088a1910ebf7de8`
