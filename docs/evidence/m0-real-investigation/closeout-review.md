# 本轮证据与环境收尾独立复核

日期：2026-09-09。受检主工作区 `/Users/shenghuikevin/dev/AI/production-ops-agent-m0-01`，环境分支最终 `ce5e2a2` 已整合为 `ed41def`；本次另覆盖当前 SPEC/C3/首片候选文档未提交修订。审查者不执行模型、trace、目标查询、数据库/虚机启停或通用全量测试，不读取 .env、provider reasoning 或账户余额全文。

**结论：已核查的收尾算术、保全工件和当前停机状态相符；未发现此收尾范围内的阻断问题。** 这不改变 [真实调查失败结论](investigation-outcome-review.md)：两次主动故障调查和最后业务接续均未交付故障最终报告，SPEC门槛仍未打开。PR可以作为证据/修复变更进入审核，不能把PR检查或审查成功说成M0/产品功能通过；远程PR/CI/机器人审查闭环未在本报告认证。

## 费用、次数与时长

独立读取环境原 `holmes-request-ledger.json`、本轮Flash公开原始结果与 `final-usage.json`，用Decimal重算：

- 模型HTTP：Flash4 + Holmes16 = 20；Holmes工具59；本轮trace上传1。到模型请求上限，不能再发起模型请求。
- 输入170078、输出46839，总计216917tokens，全部与原usage之和一致。
- 全部输入按cache-miss峰值3、输出9 CNY/M估算为0.931785 CNY，空闲减半为0.4658925；Holmes按已记录cache-hit/miss分别0.05/1.5、输出4.5 CNY/M，Flash未知cache按miss计算，合计0.3834861 CNY。这是核对合同费率假设下的算术，不是重新认证供应商实际账单/费率适用性。
- final-usage所记余额减少0.39是账户聚合观察，不是各Run可归属账单。本审查未读取或独立重取账户余额；小额差值、显示精度、入账延迟与并发用量不能被当作每案真实费用证明。
- 本轮20 CNY未核账预留 + 旧4 CNY未核账预留 = 24 CNY；预留不是实花金额。旧4是此前未结账历史，并未挤入或扩大本轮20 CNY授权。
- 六个Holmes Run的首模型请求到末响应时间跨度逐项由ledger起止时间重算，与final-usage一致；包含中间工具，不能称完整进程wall time或SLA。

## 导出与停机的实际核查

环境原工作区保留 `tmp/m0-environment/jaeger-final-export`。对公开manifest的19份gzip逐一解压，核对raw SHA256、raw字节数和trace条数；合并traceID恰为2792，压缩文件总9765809bytes（约9.31MiB），各源结果均小于25000上限。导出scope明确为当时Jaeger保留的一小时、逐服务查询；非事务快照，不证明此前没有驱逐或全部历史完整。

已读 `export_traces.py`，固定localhost只读Jaeger端点；输出目录exist_ok=False避免同目录重跑覆盖，每源读取至64MiB+1后超限停止并保留部分工件。该脚本为已授权工程保全，不是模型工具/产品权限；有界读取并不等于任意不合作源的绝对wall-time取消保证。受检源码SHA-256 `d882c6a4258621bfd6cf9bcfa201c4a9809e031f1aa4992923a23e31d76247e3`。主工作区与原环境9个Python环境脚本逐字一致。

`stopped-resources.json`原快照解析为26容器全部exited、6数据卷保留；个别退出码非0，不据此声称所有服务均优雅退出或产品恢复机制通过。随后本审查实际执行只读 `colima list`，default和m0-otel均Stopped；`lsof`在55431、18080、18081、19090、16686、19200未见TCP监听。没有启动已停环境来验证。

另核对 `postgres-cleanup.json` 与当前文件/进程：主任务PG数据目录存在，postmaster.pid不存在，55431未见监听；系统PostgreSQL PID4391仍存在且进程名为postgres。未删除数据，未访问/修改系统PG。

还原和恢复窗口原始数据已在调查审查报告逐项核对：原配置字节恢复，8条checkout200日志与8条无可见错误trace的ID集合一致、5分钟错误增量0、付款恢复。仅是有流量的本次工程还原观察，不是F6/Kubernetes HealthProfile认证。

## 最终文档边界

SPEC仅修正“完全没有实现/部署证据”的过期泛称，明确无产品实施或生产部署证据，Feature implementation gate仍not cleared。C3新增单次Flash固定协议链路的实际状态，同时保留完整协议/恢复矩阵和产品验收未完成；不把Holmes失败写为调查成功。首片候选计划已明确本轮4HTTP/128KiB/8192等只是**尚未通过故障报告链路、不得作为最终冻结值**的校准起点；保留真实目标/依赖授权、动态可见视图、持久步骤恢复与工具wall-time缺口。

实际模型/环境失败、不同窗口、不同输入上限/投影以及最后新Run业务接续均保留原记录；不得重写为匹配条件评测或跨进程provider私有协议恢复。任何后续M0实验须先明确相应合同和可用授权，当前20模型HTTP已耗尽。
