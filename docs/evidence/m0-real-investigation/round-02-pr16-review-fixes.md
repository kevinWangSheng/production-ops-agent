# PR16 审查发现修复

本记录只记录审查修复和实际验证，不改变旧报告质量FAIL、预算历史、运行快照或M1实施门槛。未执行模型/trace调用；实现者初次交付未提交或推送，后续Git状态以任务记录与PR为准。

## CodeReview P1：new_run控制代次无法通过v3合同

来源：PR16，审查提交`58e10b1`，comment `3975793064`。实现者于2026-09-10复现并修复；独立复验仍由父统筹。

**原因与范围。** StepStore的全部generation递增入口是`control(cancel/correct)`和`new_run`，两者均在主体更新的同一事务写accepted audit。v3 `ControlEvent.action`此前仅允许cancel/correct，无法表示new_run；若省略该事件，连续水位检查又会拒绝合法结果。原v2不修改，不放宽连续generation、current Run或迟到采纳条件。

**修改。** v3 `ControlAction`增加new_run，并同步Scenario.v3 schema。新增只读`StepStore.control_snapshot(subject)`：在同一主体控制锁/事务下读当前Run、最终generation和accepted控制转换；事件枚举复用v3类型，只返回generation/action/at，不返回人控payload、模型响应、证据或private。它从完整committed audit读取，而不是把只保存人工payload的m0_v3_control表误当全部代次历史。旧summary和业务写路径保持。

**红→绿。** 先新增直接new_run、cancel→new_run、correct→new_run三条最高IncidentScenario→IncidentOutcome用例。修复前命令`python -m pytest tests/test_m0_outcomes_v3.py -k new_run_generations -q`实际输出`3 failed, 4 deselected in 0.11s`，均为new_run不在Literal枚举的ValidationError；修复后同命令`3 passed, 4 deselected in 0.08s`。用例同时保留旧Run、旧generation以及缺失转换的拒绝断言。

**真实PG跨层。** PG由父明确启动并验证原目录，本执行者没有起停。新增3项真实PG用例实际调用control/new_run，再以安全快照构建v3 Scenario/Outcome；测试分别拒绝旧current_run和旧generation迟到发布，并检查accepted非控制事件（claim/model_response/publish）及rejected控制事件均不混入公开历史。额外审计行由测试有意注入随机隔离实验，用于检查过滤，不代表调用模型。公开快照不含测试人控payload。只增随机实验记录，无旧数据删除/修改。

定向真实PG命令（既有主.venv）：

```sh
M0_STEP_POSTGRES=1 /Users/shenghuikevin/dev/AI/production-ops-agent/.venv/bin/python -m pytest tests/integration/test_m0_step_store_postgres.py -k current_control_snapshot -q
```

输出`3 passed, 14 deselected in 0.83s`。随后相称回归：

```sh
M0_STEP_POSTGRES=1 /Users/shenghuikevin/dev/AI/production-ops-agent/.venv/bin/python -m pytest tests/test_m0_outcomes_v3.py tests/test_m0_outcomes.py tests/integration/test_m0_step_store_postgres.py -q
```

输出`104 passed in 7.63s`；定向Ruff为`All checks passed!`，`git diff --check`退出0。这里是实现者验证，不代替最新提交独立复验、CI或仍pending的Security审查。父负责PG停止和PR统筹；本修复不新建产品平台、修改原真实运行快照或打开M1。

稳定文件SHA-256：
- `scripts/m0/outcomes_v3.py` `d895a165f728f88146e213453d3fa6a77f8f8f80c9edb5bb603fa8f2fd031c09`
- `scripts/m0/step_store.py` `817230d1c4bef33859a6447a1f84629e0dcaf705888884eef4d5be38823492d4`
- `tests/test_m0_outcomes_v3.py` `950a2bc28210d0424b51c20ebc3601641a123293b1ff23fb4bb76dbcd84f1077`
- `tests/integration/test_m0_step_store_postgres.py` `fb3866f12757eeb8c840ee989177ad545c2824ccfdc983777cca5203585a7fc7`
- `docs/evidence/m0-real-investigation/IncidentScenario.v3.schema.json` `5e3e19ec2c43975552139d74fe72e5250c6f5c4410e97345bce8ca24e2ff7854`

父于独立复验后停止专属PG；这次为本P1必要真实跨层回归临时重启，模型/trace新增0，原20HTTP及所有unknown占用不变。初次提交58e10b1的CI checks/m0-postgres均SUCCESS（run34439757538），Code Review本P1本地已修并独立复验；Security Review仍待返回，待同轮发现齐后批量推送复审。

完整本地开发检查：`make check`退出0，374 passed/34 PG opt-in skipped（14.37s），原输出`tmp/m002-pr16-control-full-check.txt`保留；104项真实PG组合回归如上另行执行。2026-09-10T05:36Z，Security首请求约27分钟仍无GitHub运行确认或结果，统筹补发一次同head请求5613717230；未将未知状态标为通过。
