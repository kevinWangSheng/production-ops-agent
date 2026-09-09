# 首流程所需 PostgreSQL 控制与恢复探针合同

2026-09-09，执行前冻结。依据C3 §6–9/13、ADR-0003、M0 §3及F2/F7/F12。它是一次有界设计机制实验，不实现产品调度/恢复服务，不宣称完整F2通过。

问题：已提交的证据与版本能否经真实PG停止/启动保持；人工取消提交后，旧执行者能否覆盖新状态；所有权/epoch/租约或版本不符时能否拒绝采纳？

独立schema `m0_vertical_probe_20260909` 位于本任务原专属PG55431，创建时拒绝同名schema（不覆盖旧数据）。使用真实成功Flash实验的可公开最终JSON、outbox/运行代码digest与固定工具来源作为输入快照；不读取/保存provider reasoning，不宣称已验证跨进程私有协议续传。

步骤：落盘2个独立probe主体和证据，快照记录model alias/prompt fixture/source digest/protocol version/control_generation/owner_epoch/租约。记录正常CAS提交成功；另个主体人工cancel事务递增generation并提交，随后旧generation尝试提交最终候选必须0行更新。新代但旧epoch、过期租约同样拒绝。停止/重启专属PG，由新连接重建快照，核对证据内容/hash、取消状态与拒绝的迟到结果历史仍存在。给不兼容schema版本重建返回blocked(INCOMPATIBLE_STATE)，不删除或悄悄换版本。全部旧live行与诊断行哈希应保持。

验收是SQL返回行数/持久内容/实际pid变化，非模型自述；任意断言失败保留原schema/输出，不删重试。只执行此专属schema的DDL/SQL，不向产品Agent授予SQL能力。实验停止/启动前确认归属并协调其他只读审查连接；未杀其他进程。0新增模型/trace/费用，沿用本轮绝对截止。未知失败明确记录；无自动后台服务。实现时明确probe粒度不能替代完整worker中断/重复输入/租约竞争矩阵。

执行后边界说明：实际创建4个主体（normal/cancel/epoch/expired），不是上文预记的2个，原过程文本保留以便审计。SQL已覆盖generation/epoch/status/lease；**未设置独立owner或Run字段，因此owner/Run不符拒绝这一原定判据尚未完成**。只重启PG并重建最终业务快照，没有逐ModelStep/ToolOperation提交断点、当前许可重验证或后续发送拒绝证明。初版“incompatible_reconstruction”输出只来自schema比较；后续显式reconstruct函数对实际持久快照返回ready/blocked的检查另存control-reconstruction-check.json，仍不证明真实provider协议续传。原始源码hash对应control-probe-original.py.txt，原始结果不改写。该探针补齐子证据，不单独打开实施门槛。
