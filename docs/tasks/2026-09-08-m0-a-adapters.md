# M0 A：模型协议与 trace 适配

- 状态：待接口独立审查后开始；日期：2026-09-08。
- 批次与授权：[索引](2026-09-08-m0-batch.md)，本地可逆 M0 实施测试及 PR；不合并、不联网实验。
- 依据：SPEC、C3 §5/7/11–13、M0 §1–7；F1/F2/F7/F8/F14 相关机制前提，不更改 steps/passes。
- 工作区：待协调者从共同基线创建 `production-ops-agent-m0-a` / `chore/m0-a-adapters`。

## 目标、接口与归属

可注入 transport 的协议适配；完整流式响应后才执行工具；正常/断流/错误参数/工具失败/配对/Run 和私有字段边界；白名单 trace 上传回读替身。复用旧排演，若需修改 scripts/m0/protocol.py 先与协调者约定。禁止另建预算系统。

专属文件：scripts/m0/adapters.py、scripts/m0/trace_adapter.py、tests/test_m0_adapters.py、tests/test_m0_trace.py、tests/fixtures/m0/adapters/；本任务记录及 docs/evidence/m0-a/。共享接口遵守[合同](2026-09-08-m0-shared-contract.md)。依赖锁、CLI/config、ROADMAP、批次状态由协调者修改；需依赖时报告具体版本理由，不自行更新锁。PR base 为 chore/m0-batch-baseline，依赖基线未合并变更，不能算本 PR 独有。

## 实验前提、步骤与完成条件

执行前核查 Git、完整 SPEC 与上述依据；不得读取真实 .env，合成资料不包含业务或保留集。先记录所选版本、实验命令/输入/预期及失败处置，再实施与运行。用合成 SDK transport 运行正常与故障路径，记录预算协议调用顺序；trace 写/读失败和污染/错误归属必须可见；live 仍拒绝。所有失败保留、修复后重测；环境不具备单列证据不足。自测后由全新上下文独立审查，处理发现后提交，协调者推送/建 PR 并检查 CI。

## 资源、进展与交接

A/C 仅短时 Python 测试及公开资料查询；B 独占本批唯一重型环境，不干扰其他项目。真实模型和 trace 次数必须为 0，付费总额未授权，不复制凭据。独立数据库不增加真实授权额度。
当前未执行；下一步接口审查后实现，写回实际命令、版本/hash、结果及缺项，保留专属进程/数据位置；完成时分别标注本地、独立审查、PR/CI、合并、真实实验、M0 退出与产品验收。
