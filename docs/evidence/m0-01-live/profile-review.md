# M0-01 模型批准绑定与 PR 审查闭环：独立复核

2026-09-09；独立 Agent 使用全新上下文审查，未参与实现。基线提交 `782fcd278ab39b0dae7e61f5482f0a5dc791da71`，范围为其上的 `AGENTS.md`、`scripts/m0/live.py`、`tests/test_m0_live.py` 未提交变更。依据为 SPEC 当前实施门槛、排除项、证据、人工控制、费用及交付约束，C3 §5/12，以及原 normal-1 方案。缺失的 ignored 会话进度文件不作为阻塞。

## 结论

本轮范围内未发现阻塞缺陷。结论仅覆盖本地模型批准拒绝、响应身份校验与 PR 流程规则；不证明固定后端权重、不批准新的真实调用，不表示 PR 远程审查闭环或产品验收已完成。

- v2 批准合同精确绑定请求模型、可接受响应模型、thinking、effort 和 `reported_alias` 范围。缺项、未知范围、`fixed_weights` 或固定 0813 响应要求均在账本 claim 和外部请求前拒绝；旧 v1 合同不能因代码升级取得新权限。
- 每轮响应模型必须与批准值严格一致。首轮不匹配时，工具和第二轮请求均停止；第二轮不匹配时业务失败，不上传 trace。失败仍持久保存有限状态和安全用量字段，预算不自动释放。
- `reported_alias` 只能约束服务报告的别名，无法检测服务继续报告相同别名但更换后端的情形。固定权重要求因此被拒绝而非降级；接受别名范围需要真实的新批准，不能重解释既有批准。本审查没有查询供应商，亦不独立确认供应商当前路由或不可变版本能力。
- AGENTS 新条款要求已触发审查返回、处置发现并核对提交覆盖；实质更新要求复审。它没有新增自动合并权，也没有把 CI、旧提交审查或 PR 就绪等同于合并/收尾，符合现有授权与交接要求。

## 本地验证

执行 `.venv/bin/python -m pytest tests/test_m0_live.py -q`：初次 **35 passed in 0.80s**；主 Agent 补充旧 v1 拒绝和第二轮模型不匹配回归后，重新审查测试差异并执行同一命令：**38 passed in 0.75s**（最终覆盖版本）。

另执行独立 Python 离线探针，复用测试合成合同和 MemoryLedger，以 `no_network()` 和 MockTransport 执行真实 `execute` 路径：

1. v1 合同分别保留和移除 profile，均抛出 ConfigError，`ledger.claimed == False`。
2. 第一轮响应改为 `deepseek-v4-pro-0813`：一个模型请求、零工具执行、业务 failed、`LIVE_MODEL_PROFILE_MISMATCH`、无 trace POST。
3. 仅第二轮响应改为该固定版本：两个模型请求、一次先前获准工具执行、业务 failed、同一受控错误码、无 trace POST。
4. 两轮出站请求的 model 和 effort 与批准 profile 一致；上述两种失败都保存 failed 业务结果。

探针输出：`PASS: legacy v1 rejects with/without profile; first and second response mismatches halt before further tool/model/trace; request profile bound`。探针为本轮额外验证；旧合同和第二轮不匹配已由主 Agent 纳入持久回归并经本审查复验。未读取 `.env` 或真实批准文件，未访问外部服务、执行 live CLI、触碰 PostgreSQL。

## 审查覆盖指纹

以下 SHA-256 固定上述检查所见版本；后续实质变更应复核受影响项。

- `AGENTS.md`：`a35146876d7b15f09f872915aa68f2701f902687cf0d69349a57b840a4c8dcb9`
- `scripts/m0/live.py`：`27dfee6bd8fad3f8d434e737dcb16ff9b2bbc31953a247fa7bba45b6d1f8f5db`
- `tests/test_m0_live.py`：`5fdfe91aace4f0e3667da69d9cc1b05da3e9d4ce3eb3004efe5c56eec9282ae7`

主 Agent 同时维护的其他文档不在本次独立结论范围；远程机器人结果和最新 CI 尚须主流程等待及核查。
