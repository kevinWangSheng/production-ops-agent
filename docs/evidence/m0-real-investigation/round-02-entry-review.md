# M0-02 首流程实施入口独立审查

日期：2026-09-10 UTC。审查者：fresh-context `m0_entry_review`，未参与实现。结论：**目前不能打开 M1-01；报告收束与必要持久机制已有实证，报告质量仍未闭合；下次候选验收包现已冻结，尚未执行。** 不将所有 M0 后续事项提前变成首片前提，也不以有限核心定位成功代替整份报告证据真实性。

## 审查边界

已读当前 AGENTS、完整 SPEC、ROADMAP、C3 §5–8/12–13、M0 计划及 first-vertical-investigation 计划；核查 v3 outcomes/bridge、StepStore/SQL/budget、PG driver、环境 harness/report_contract/trace_view 与定向测试。主 worktree 为 `production-ops-agent-m0-01`，环境为 `production-ops-agent-m0-environment`；候选存在未提交变更，本审查不更改实现、验收、Git index 或原业务记录。

没有调用模型、trace 或业务后端，没有启动/停止环境，没有读取 `.env`、private-protocol 或真实 provider reasoning。PG 仅读取原实验安全元数据；定向机制测试另建随机实验 ID，保留原记录。共享宿主/worktree 不是 OS 隔离证据。此前审查用于定位，以下结果另经原工件和测试核验。

## 入口条件逐项判断

| 条件 | 当前证据与判断 |
| --- | --- |
| 正常及可诊断故障真实主动报告 | **协议收束、有限核心结论成立；完整质量未通过。** normal01 非 JSON 最终正文，normal02 原报告存在，fault01 4 HTTP/19 工具、normal03 3 HTTP/12 工具有实际 JSON 最终报告。fault01 定位 checkout→payment Charge 失败方向有据；normal03 支持限定窗口未观察到 checkout 失败。不能回改旧失败。 |
| 动态证据、raw/view、实际交付 | **必要合同及本轮业务回放成立。** 4 个主动 Run 共 62 个 manifest 的 raw/view 文件 hash 全部匹配；15 个 delivered-business messages 采用运行器定义的 canonical 序列化重算 hash 全部匹配。固定旧投影重放 fault01 与 normal03 的 bridge/v3 无 violations，19/19 与12/12 引用实际交付。此为合同一致性，不是语义正确性。 |
| 精确关注主体/授权/来源 | **合同具备，真实执行权限证据不完整。** v3 分开 Compose、Kubernetes、integration unknown 身份；bridge 从固定 registry 映射关注主体，混合来源保留自己的身份。主动 bridge 的 Action 明确 authorized/executed unknown，不能因此声称全部实际权限验证通过。实际 Holmes 的 OS 文件/网络/注入器隔离尚未证明，container probe 不能替代；这是一项明确的运行环境限制，不单独要求在有限首片编码前建设隔离平台。 |
| 最终人工控制与步骤重建 | **首片机制前提有相称的分层证据。** 定向真实 PG 测试覆盖提交断点、部分工具重建、取消发起竞争、owner/epoch/lease、迟到响应、未知预算、版本阻塞、发布幂等及孤儿 transport。真实模型/PG 两步 fixture 已跨进程重建并发布。主动 Holmes Run 没有执行 PG 恢复或人工取消；这两条实验必须分别描述，不称完整主动调查恢复已通过。 |
| 兼容性及预算 | **本轮已测配置可复现；新候选效果未知。** 明确 Flash 请求/响应别名、thinking/high、最终 JSON/no-tools；固定 tool_choice 的400原失败保留。20请求用尽，0 trace；账本 hash 与 final-usage 一致。32768输出、512KiB请求、360s/1800s、工具30s/累计240s是本轮开发配置，不是SLA。18份usage峰值上界1.438848 CNY、2份未知6.88128 CNY及旧24保留；不是发票实付。 |
| 首片验收与环境冻结 | **文档缺项已关闭，候选执行未完成。** 审查后父新增并由本审查完整复核 [冻结开发包](../../testing/first-investigation-v3-2026-09-10.md)：正常/故障各2 Run、每窗300秒、必要机制每例1次、所有报告无未处理P1/P2、非退化及先固定候选hash。配置写明32768/512KiB、时间/费用/清理和现有OTel环境；未来供应商调用须新授权。它适用于下一候选，不回改本轮FAIL，不补成已执行的验收。 |

## 直接核见的质量问题

1. fault01 claim13 写“20 spans of349”，其 e8/e9 实际 `sampled_spans` 长度均14、total349、omitted335。normal03 claim6及 summary 同样写20/248，实际 e11/e12 均14/248、omitted234。报告把 cap 当实际显示数；它改变证据覆盖范围，不只是格式差异。
2. fault01 claim8/15 把全窗口 cart 日志及同一失败 trace 的 cart HTTP200作为排除依据；原 claim 所引 e6/e7 不支持该同 trace HTTP200关联，且仅显示部分日志。normal03 claim5 把没有返回的 accounting ERROR 序列称为0；claim7 把一条 trace 可见的 gRPC 段扩到另一条。这些逐项原view分析见 fault-outcome-review 与 normal03-outcome-review，原报告文本本审查已直接复核。
3. fault01 summary 将投影不提供错误细节泛化为 permitted telemetry 无触发信息；保留 raw 中已有 payment 错误细节。最新 trace_view 增加诊断字段和实际覆盖计数，metric view 明确 missing series 不等于0，离线49测试通过；**没有新的模型报告证明这些误判已消失**。新的更详细 trace view 会减少可见 spans，必须把此取舍纳入下一次冻结版本，不能用新view解释成旧模型当时看到了它。

上述是实际报告缺陷及入口证据缺项，不宣称发现新的 StepStore 安全实现缺陷。`check_outcome` 与 `validate_report` 校验身份、引用及结构，没有实现通用自然语言因果评分；通过这两个函数不能关闭以上质量发现。bridge 的默认 generation0只对应未发生人工事件的主动实验，不证明真实人控水位集成已完成。

## 独立执行与原记录核验

- 主 worktree：`M0_STEP_POSTGRES=1 /Users/shenghuikevin/dev/AI/production-ops-agent/.venv/bin/python -m pytest tests/test_m0_outcomes_v3.py tests/test_m0_holmes_bridge.py tests/test_m0_pg_live_probe.py tests/integration/test_m0_step_store_postgres.py -q`：**40 passed，7.74s**。
- 环境 worktree：同 Python `-m pytest tests/test_m0_holmes_round02.py tests/test_m0_trace_view.py -q`：**49 passed，1.52s**。仅本地假端点/fixture与子进程机制；无真实供应商或业务后端请求。
- 直接 Python 调用 `load_packet` + `check_outcome`，使用旧完整投影快照 `22a96b87cf6575c8246abeb7edbed5101e1c6951fcc62b5e8243b6e589c6efd8.py.txt`：fault01/normal03 均 `[]`。normal02需自己的旧版本/依赖组合，误用上述快照得到 `PROJECTION_DEPENDENCY_MISSING`，本审查没有把该参数错误称为运行缺陷或算作normal02重放通过。最初使用紧凑JSON重算delivery不匹配，改用已读运行器的默认空格canonical定义后全部匹配；原文件无修改。
- 原 PG 使用 READ ONLY 事务核查：`SHOW data_directory` 精确为主实验 `tmp/m0-b/postgres`；subject `0f388fcf-ab06-46fb-acd5-ec8c226efdb6` 为 completed/generation0/epoch2/published；round0/1 均有完整响应及hash，tool_calls分别1/0，工具结果success且captured_at保留；审计按序 model_response、tool_result、model_response、publish均 accepted；受限 wire 表仅查询HTTP状态与长度，200/812bytes和200/829bytes，没有读取正文。

审查源码 SHA256：

| 文件 | SHA256 |
| --- | --- |
| scripts/m0/outcomes_v3.py | aa42ba5594648e7408a85e4ef80182ec7705d76fd011c0111f69ad43cddc68ee |
| scripts/m0/holmes_bridge.py | e1c9bca7351dddfa79ecff268c1cbe2257f994021b65b420e1fe2dcc62b03389 |
| scripts/m0/step_store.py | 71bf4c2452ee44e73c9c7b438296d2e6451fd722329975d1d542b69324f2e02b |
| scripts/m0/step_store.sql | e3cba8e242aedc329dac605736b52b5e3f31b9225293a7fcbadf4830ba73ddaf |
| scripts/m0/budget.py | 0eee8b60ab91d21df7fe5f97a4116bb6c74a0afbccf4407857effb9e3262408b |
| scripts/m0_pg_live_probe.py | 6d86a7dc64462709b33936b0d3eac0cb9ea5dba05795f1853a2937f57cc1fac6 |
| scripts/m0_pg_private_transport.py | 50cd86e99941b6ad076ad77f7e369429f1ac5b6baf95de4df820fd6c2823fcb8 |
| 原全轮 ledger | 97a0fd3816a4f8996987c4934409181a42b4f9479c92356dc91eecfa9ff9dfcc |
| feature_list.json（未修改） | f0945ab400eabc8e05ced64c8328498d325fd566052fb2d14c4606299ea3fcd3 |

## 剩余有界任务与入口决定

先完成本轮归档，把成功协议/有限定位和报告质量失败分开，更新旧计划状态。父新建的首片冻结包已经独立复核，明确身份/交付/控制、预算、正常与故障各2次、逐事实准确性及unknown质量、非退化和资源环境条件，关闭了原先缺少可执行质量阈值的文档问题。实际候选执行前还须固定完整版本/hash，并取得其所属新预算/期限。已见案例仅作开发回归；完整保留集和72小时soak仍在后续阶段。

冻结首片的可信工具/网关权限和运行环境：已有proxy权限探针与实际Holmes进程隔离证据分开。M0计划要求真实拒绝证据，C3要求持续运行测试时控制侧与目标隔离；它们不自动要求在M1编码前建设新OS平台。当前实际Holmes文件/网络/工程注入器隔离未证须保留为限制，后续对应运行边界须实测，不能只依赖Python monkeypatch或模型自律，也不能以container probe替代。缺口的处理阶段与适用环境应明确，不暗中宣称安全验收已过。

新付费授权后按冻结包复验正常与故障主动报告，由独立审查核验全部关键事实，保留失败分母；尤其计数、缺系列、同trace关联、投影缺口不得继续冒充已知事实。仅在证据满足后，记录入口决定与日期并更新 SPEC/ROADMAP 开 M1-01。本审查不要求先完成完整 UI、独立恢复观察、全故障矩阵或soak，也不把缺少主动Holmes+PG完整产品集成单独当作必须先造完产品的门槛。

**当前最终判断：首流程前提推进显著，但实施入口仍未清除；所有 feature passes 保持原状。** 此审查不是用户批准、PR合并授权或产品验收通过。
