# PR16 控制代次修复独立复验

日期：2026-09-10。结论：**P1 comment3975793064 本地独立复验通过，无新增阻断发现。** 仅覆盖当前5文件修复；不代替最新远端CI/Code/Security复审，不授权合并，也不改变实验质量FAIL或产品实施门槛。

## 依据与检查

独立通过GitHub只读API核对[原P1](https://github.com/kevinWangSheng/production-ops-agent/pull/16#discussion_r3975793064)，审查基线58e10b1feced67b840f265f49124b251ad85560e与本工作区HEAD一致。问题确为new_run递增generation，而v3控制事件不能表示该转换，导致连续历史校验失败。

逐项读实际diff与所有m0_v3_subject写入口：generation递增只有StepStore.control(cancel/correct)及new_run；两者都先持同一主体advisory事务锁，主体更新与accepted audit在同一事务提交。claim/renew/publish不增加generation；旧m0_control_probe为独立历史schema，不是v3路径。未发现其他代次递增入口遗漏。

ControlAction新枚举只增加new_run；连续generation、current Run和旧fence拒绝规则未放宽。control_snapshot在同一主体锁/事务读取current_run/generation与accepted控制audit，复用相同ControlAction枚举，按sequence排序；因此不会把仅含人工payload的m0_v3_control误当完整历史。输出只含current_run/final_generation及generation/action/at，不包含人控payload、模型响应、证据或私有协议。

## 实际验证

独立运行：

```sh
M0_STEP_POSTGRES=1 .venv/bin/python -m pytest tests/test_m0_outcomes_v3.py tests/test_m0_outcomes.py tests/integration/test_m0_step_store_postgres.py -q
```

结果：**104 passed in 7.31s**。实际PG随机隔离实验覆盖直接new_run、cancel→new_run、correct→new_run，均从control_snapshot构建IncidentScenario→IncidentOutcome并通过；独立旧Run和旧generation迟到发布仍拒绝，accepted非控制audit与rejected控制audit不混入事件历史，控制payload哨兵未出现在公开快照。

这些最高入口用例使用新Run尚未查询的running/incomplete Outcome，验证控制历史衔接；不是新一轮付费调查报告或产品端到端验收。测试均使用随机实验记录，无drop旧schema/旧数据覆盖，无模型/trace请求，无.env或实际private读取，无PG起停。

另直接比较当前IncidentScenario.model_json_schema()与tracked Scenario.v3 schema JSON：完全相同。git diff确认v2 outcomes.py和原v2 fixture没有改动；原v2测试包含在104项回归中。

## 稳定快照

作者冻结hash与独立当前SHA256全部匹配：
- `scripts/m0/outcomes_v3.py` `d895a165f728f88146e213453d3fa6a77f8f8f80c9edb5bb603fa8f2fd031c09`
- `scripts/m0/step_store.py` `817230d1c4bef33859a6447a1f84629e0dcaf705888884eef4d5be38823492d4`
- `tests/test_m0_outcomes_v3.py` `950a2bc28210d0424b51c20ebc3601641a123293b1ff23fb4bb76dbcd84f1077`
- `tests/integration/test_m0_step_store_postgres.py` `fb3866f12757eeb8c840ee989177ad545c2824ccfdc983777cca5203585a7fc7`
- `docs/evidence/m0-real-investigation/IncidentScenario.v3.schema.json` `5e3e19ec2c43975552139d74fe72e5250c6f5c4410e97345bce8ca24e2ff7854`

审查者只新增本记录，未改实现、未提交或推送。PG检查已完成，可由父执行者按原安排停止专属PG；远端Security仍由父等待闭环。
