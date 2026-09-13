# M0-02 新真实PG组合独立结果核验

日期2026-09-10。结论：**本次synthetic fixture +真实DeepSeek/PG跨进程协议组合通过；不代表主动调查或全部恢复机制通过。** 审查者0模型/trace/余额调用，无环境启停，未将实际reasoning或完整provider wirebody读入上下文。

## 实际业务与请求

Run 5822fb34-c343-4085-aca5-6337ff2ad40d，experiment 10e0b870-4db4-4fda-b2fd-991dc68e3368，subject 0f388fcf-ab06-46fb-acd5-ec8c226efdb6。record与阶段stdout分别为first_committed和completed；first PID39801、second44306，实际PG最终state=completed/generation0/epoch2，current_run一致，final为精确fixture observed JSON。

独立实际数据库回读：两个model request分别epoch1/2、均settled；PG保守上界mirror为1623和1428微CNY。对应全轮文件账本ordinal12/13、phase pg、request_id分别05d69bd5-aa5c-441b-807f-47cd333e354f与181b7fee-54f1-4e16-8d8b-cbdd43754165，HTTP200。输入/输出tokens331/70和272/68；本轮费用上界0.001623+0.001428=0.003051 CNY，仅峰值全miss上界，不是实际账单已核。两次reported_models均deepseek-flash，出站explicit v4-flash。

PG受限wire表仅查看status/code/bytes：两份CAPTURED/200，812与829bytes，未导出body。两步response reasoning字段类型均string；第一步一个tool_call，第二步零tool_call。

## 重建与协议保留

在数据库端比较、仅返回布尔：第二步初始message与第一步输入一致=true；第二步完整assistant message与第一步已提交response一致=true；第二步tool message与已提交ToolOperation.result一致=true；reasoning字段相等=true。未返回任何private正文。结合已审driver第二PID读取PG rebuild路径，证明本次第二请求输入来自同Run已提交消息，不是仅重新手工构造相似内容。

ToolOperation实际success，plan call id与result tool_call_id均call_00_AtLQMQTwF2ZB5tm74zeR3584；固定业务fixture evidence_id=m0-evidence-a、target=m0-target-a、observed_at=2026-09-08T00:01:00Z、来源synthetic/read_fixture/v1，与原fixture一致。record wire_assistant_content_normalized=false，本次实际无null→空串改写；归一化支持仅另有离线证据，不冒称实际触发。

## 历史保全

独立逐表执行row_to_json(t)::text，Python字符串sorted后以换行join（无尾换行）UTF8 SHA256。round-02-pg-before.json所列7旧表、总53行全部行数/hash一致：29旧live、15diagnostics、5attempt、4subject、3空表。新机制实验表未混入旧表校验。未输出旧表正文，未修改旧PG400/unknown或其他历史记录。

## 边界

这是预先指定fixture工具与精确最终JSON的两阶段真实模型/PG组合，非主动故障调查、非故障报告质量证明、非真实worker中途崩溃恢复（本次是阶段间正常退出）；父SIGKILL/取消的安全性有另列确定性PG测试。旧首PG400和3.44064unknown继续保留，不能因新Run成功释放。

证据：tmp/m002-pg-live-v2.json、tmp/m002-pg-v2-first.stdout、tmp/m002-pg-v2-second.stdout、环境tmp/m0-environment/m0-02-request-ledger.json，以及本次数据库只读布尔/状态/哈希核验。record自身版本hash逐项核对结果见下。

- `/Users/shenghuikevin/dev/AI/production-ops-agent-m0-01/scripts/m0_pg_live_probe.py` 当前执行源码hash匹配：`True`
- `/Users/shenghuikevin/dev/AI/production-ops-agent-m0-01/scripts/m0_pg_private_transport.py` 当前执行源码hash匹配：`True`
- `/Users/shenghuikevin/dev/AI/production-ops-agent-m0-01/scripts/m0/step_store.py` 当前执行源码hash匹配：`True`
- `/Users/shenghuikevin/dev/AI/production-ops-agent-m0-01/scripts/m0/step_store.sql` 当前执行源码hash匹配：`True`
- `/Users/shenghuikevin/dev/AI/production-ops-agent-m0-01/scripts/m0/budget.py` 当前执行源码hash匹配：`True`
- `/Users/shenghuikevin/dev/AI/production-ops-agent-m0-environment/scripts/m0_environment/round02.py` 当前执行源码hash匹配：`True`
