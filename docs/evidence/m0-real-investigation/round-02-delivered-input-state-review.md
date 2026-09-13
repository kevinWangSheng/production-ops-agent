# PR16 完整交付输入与状态一致性独立审查

日期：2026-09-10。审查者为全新上下文 `delivered_input_state_review`，未参与实现，仅写本记录。工作区 `/Users/shenghuikevin/dev/AI/production-ops-agent-m0-01`；起点 `65d3624d6c58b0d2f3647e27485e30d3bb653781`。依据 AGENTS、SPEC、当前严格 v4 验收包和实际源码。原报告质量 FAIL、20 次真实模型额度耗尽、M1 门槛关闭均不变。

## 设计审查

已在修改关键代码前核查 `report_contract.prepare_wire/final_payload` 与 `holmes_baseline` 的实际调用：wire 阶段处理 JSON 副本，当前 context 不回写 Holmes 历史。合法 user 序列为原始实际输入、本次 context，final_phase 才追加固定最终指令。assistant/tool 历史属于实际 wire；已持久业务投影本来仅包含 user/tool，不能为审查而把 assistant 塞入该投影或扩展其合同。

批准新增可信发送记录 `final_phase` 与固定最终指令 hash；缺旧元数据保持 unknown，不从消息内容反推。完整核对 user 对象、内容及顺序，涵盖 reportless；固定指令须同时匹配真实纯函数结果，不能任意 hash 自签。历史 v3 入口不增加这些要求。

状态要求为 `assessment_status=completed` 必须对应可信与输出的 `execution=completed`，涵盖 partial/inconclusive。经核 SPEC 和既有 v3 incomplete 条件，不从模型自述改写可信执行状态，也不强制反向等价：有界执行完成但报告 incomplete/inconclusive、具备 gaps/handoff 仍是合法表达。

## 独立复现与验证

修改前独立 Python 调用 fixture、bridge 和 public checker，确认干净 fixture 返回 `[]`，追加 user 指令并更新业务消息 hash 后仍 `[]`；completed assessment 的 partial/inconclusive 分别搭配 failed/blocked 共四例也返回 `[]`。这两项是实际可重现绕过。

修改后实际执行：

- `.venv/bin/python -m pytest tests/test_m0_outcomes_v4.py tests/test_m0_holmes_bridge_v4.py -q`：137 passed，3.82s。
- `PYTHONPATH=. .venv/bin/python /tmp/opspilot-delivered-input-independent.py`：独立 public DTO 与实际 `python -m scripts.m0.holmes_bridge` 子进程 CLI 矩阵通过，退出 0。
- 使用现成 Holmes 解释器重跑 `tests/fixtures/m0_environment/strict_runtime_probe.py`：四个离线 probe 通过。有效/错误目标案例各模拟四次请求；两个无来源 initial 案例在零请求前拒绝；全部 `real_http=0`。
- 另直接重复调用真实 `prepare_wire`：带 assistant/tool 历史的同一原始输入，非 final 和 final 各执行三次，分别恒为二/三个 user，原始输入不变且历史保持。

CLI 正例：合法提前报告（final_phase=false）和最终阶段报告（true）均退出 0、无 violation。负例：前后额外 user、重复原输入、重复/旧 context、content block、额外字段、篡改指令、缺 phase、缺指令 hash 全部退出 1。malformed report 保留原文且报告对象为 null；该 reportless 输入追加 user 后仍增加 `UNEXPECTED_USER_MESSAGE`，不因解析失败跳过。

状态矩阵：supported/partial/inconclusive 搭配 completed 均通过；分别搭配 failed、blocked、running、budget_exhausted 共十二例全部拒绝并含 `ASSESSMENT_EXECUTION_MISMATCH`。completed execution + incomplete/inconclusive/gaps/handoff 正例通过。

初次独立脚本运行有测试环境/脚本错误，已保留说明：项目 `.venv` 不含 dotenv，改用已有 Holmes venv 后通过，未安装依赖；独立临时脚本需 `PYTHONPATH=.`；错误地把 assistant 加入仅 user/tool 业务投影被既有校验拒绝，已改在真实 wire probe 验证历史；reportless 断言最初寻找 INPUT 字样，实际正确 violation 为 `UNEXPECTED_USER_MESSAGE`，修正断言后全矩阵通过。这些不计实现缺陷或额外真实模型实验。

运行 Holmes probe 的完整命令：

```sh
HOLMES_TEST_UPSTREAM=/Users/shenghuikevin/dev/AI/production-ops-agent-m0-environment/tmp/m0-environment/holmesgpt-5e983c17f30e93099c7d775167266d4cd1d586c4 /Users/shenghuikevin/dev/AI/production-ops-agent-m0-environment/tmp/m0-environment/holmes-venv/bin/python tests/fixtures/m0_environment/strict_runtime_probe.py
```

仅使用预置合成替身；未读取真实私有 provider 响应、凭据或 .env，未连接网络/模型/PG、未启停服务。

## 工件版本与边界

独立检查的 SHA256：

- `scripts/m0/outcomes_v4.py`：`ae3cda21b274bc61c17cfb28abbe406a5edebffe0680f61cf3f4d8a3ef3e0b69`
- `scripts/m0/holmes_bridge.py`：`e303cbadef1f4805b9a11695134bec2ffc44dc1219640e1d9cc7a978055100b6`
- `scripts/m0_environment/holmes_baseline.py`：`24ece20b3d5ed671b86cb652475eb1c56064bb9efc15d48af11227f48271adee`
- `tests/fixtures/m0_environment/strict_runtime_probe.py`：`c17beecfc6346f2fa13bbaf6b17a6cb3fcc977d91316b38dd842f30525a49320`
- 独立临时矩阵脚本：`77c6caa89118c648f69514dd4579643309c07fb04214d4168b1cc36ad5cdcb03`
- `/tmp/opspilot-independent-delivery-matrix.log`：`80fe03b1f767d9e365ce824ac263816032aa7e316f628e5b2bc2206a086fc637`
- `/tmp/opspilot-independent-wire-probe.log`：`76a627f4ee19f8b3dfd70c07c66dd8f3a7a02f50398835f3098f0f97b171fa17`

上述实现接缝的离线独立验证通过，无剩余本范围 P1/P2。schema 导出一致性与最终源码未变化核对在下方续记；PR 最新 CI/外部审查、合并授权由主执行者处理。本记录不是历史报告重新认证，不代表新真实模型效果、M0 退出或产品验收。

冻结后续记：独立 Python 将两个 canonical v4 schema 的 JSON 与对应 `model_json_schema()` 逐项比较，均一致。Scenario SHA256 为 `b8b28f6fe59072388a128b70e97d21af0d2ea68c80bc97cd0d6a6ff9c10ca3c6`；Outcome 为 `84ed7977a33b503abd13ea2591ab85dbdba8584038ce0ddc438bb5a00864ad8b`。再次计算三份实现源码 hash 与上述受测版本完全一致；`git diff --name-only` 确认旧 v2/v3 schema 和历史 source-snapshots 未修改。当前独立审查已完成，无本范围待处理项；后续实质代码变更须复核受影响部分。
