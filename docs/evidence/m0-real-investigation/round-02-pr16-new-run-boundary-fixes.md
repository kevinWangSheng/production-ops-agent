# PR16 new_run版本与状态边界修复

对应`78f7af0` review的P1 comments `3978285916/3978285917`。有界修复仅涉及StepStore、v4 checker和对应测试；未修改bridge/schema/预算实现，未提交。PG由root核实并启动；作者仅操作随机隔离实验，未起停环境、访问旧真实行或调用模型/网络。

## 3978285916：版本前提必须先于事务

原accept只检查versions truthiness，而new_run完全缺失相同前提、claim只比较相等，故new_run({})之后claim({})可执行未版本化Run。共享离线pretransaction矩阵还暴露非mapping、空key/value与非string value未拒的同组路径。

新增实际PG测试先红：

```sh
M0_STEP_POSTGRES=1 .venv/bin/python -m pytest tests/integration/test_m0_step_store_postgres.py -k empty_versions -q --tb=short
```

`1 failed, 26 deselected in 0.67s`，DID NOT RAISE BudgetError。该红例只写入自己的随机实验，没有修改或清理历史行。

修复共享_validate_versions：要求非空dict、非空string key/value；accept/new_run/claim均在进入事务之前检查并拒INVALID_INPUT。仅验证版本向量形状和非空性，不增加中央版本目录；具体兼容版本集合仍由调用方和既有版本不匹配blocked机制负责。

PG绿例证明拒绝后summary、预算快照不变，没有写入新m0_runs/m0_v3_run_input、没有accepted new_run audit或代次推进；随后同fresh Run合法版本可正常new_run/claim，旧fence仍不能publish。没有回写或迁移历史版本记录。

## 3978285917：最后new_run不能残留旧人控状态

当前StepStore的new_run明确进入running；只有后续cancel/correct才分别进入cancelled/waiting_human。v4原来只校最后cancel/correct，遗漏最后new_run后的这两种不可能残留状态。现在仅在最后accepted控制为new_run时排除cancelled/waiting_human，返回CONTROL_STATE_MISMATCH；不限制正常runtime进度及blocked/failed/budget_exhausted/completed等结果。

新增正反例覆盖两种残留状态、正常runtime状态，以及new_run→cancel/correct和cancel/correct→new_run完成两种顺序，避免让旧控制永久阻止新Run完成。

## 作者证据与冻结

```sh
.venv/bin/python -m pytest tests/test_m0_step_store_versions.py tests/test_m0_outcomes_v4.py -k 'version_precondition or latest_new_run' -q --tb=short
.venv/bin/python -m pytest tests/test_m0_step_store_versions.py tests/test_m0_outcomes.py tests/test_m0_outcomes_v3.py tests/test_m0_outcomes_v4.py -q
M0_STEP_POSTGRES=1 .venv/bin/python -m pytest tests/integration/test_m0_step_store_postgres.py -q --tb=short
.venv/bin/ruff check scripts/m0/step_store.py scripts/m0/outcomes_v4.py tests/test_m0_step_store_versions.py tests/test_m0_outcomes_v4.py tests/integration/test_m0_step_store_postgres.py
```

初次离线红20 fail/9 pass（18个无效版本进入transaction、2个非法state误过）；修后29个定向通过。最终180项离线通过（0.76s）、真实PG全27项通过（7.80s），ruff通过。PG组包含跨进程/fence、new_run预算与unknown继承、累计约束和最终指针历史保留回归；其中金额均隔离替身账本单位，不是真实CNY或实际HTTP。

稳定SHA256已交独立new_run_boundary_review复验，作者不自称独立通过：

- scripts/m0/step_store.py：`a807d26c229df4f9e220e533bea94d9fadd6f59893b636555a57fe2b1362b35d`
- scripts/m0/outcomes_v4.py：`b480812add96056e246520bffd3c88093e263265b47feb67ce0fcff420265d24`
- tests/test_m0_step_store_versions.py：`1c267b32b0958a43a27d0399dd1f031bea01a54f04cf3998b6cb65ef41a0c511`
- tests/test_m0_outcomes_v4.py：`c9a00009e2c47528b386b3c2743df74eca1015b20942a0d02c16dc8cd5f84a48`
- tests/integration/test_m0_step_store_postgres.py：`3a9ee28d431ad55895ef7c5ebb6c8f78c6aa43ad7fe2e98509524d3867c1d788`

root负责PG停止、整体fullcheck、source快照与PR。当前修复不新增真实模型验证，不改变历史qualityFAIL或M1门槛。
