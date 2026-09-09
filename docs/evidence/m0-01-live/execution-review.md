# normal-1 首次真实结果独立审查

2026-09-09；独立 Agent 以新上下文核查。结论：**两轮模型业务链完成有证据支持；trace 未完成严格读回验证，首条 normal-1 整体未通过。** 本审查不开放 M0 或产品实施门槛。

## 范围与验证

读取 AGENTS、SPEC、ROADMAP、M0 计划、C3 数据流/预算合同、本目录 plan.md/execution.md、冻结执行代码及 protocol.py。未读取 `.env`、批准文件或凭据；未发外部 HTTP、模型/trace 请求，未重跑实验、启停服务或修改实现。

- 当前 HEAD 为 `2dbdc0ffbc126931b340b99efce57ec63bb0ba2b`。按代码中的路径及拼接算法独立重算 code digest，得到 `eac1968a6cd9215b8095b04c05b4f13b04d04bf7821bf3aad4adaa37f6ec9e12`，与账本 outbox 一致。
- 读取允许的 `tmp/m0-01-live/execution-cli.json`、`ledger-result.json`。CLI 记录运行 14.415 秒、退出 1、business=completed、trace=unknown、模型请求计数 2。
- 使用 psycopg 连接专属本地 PostgreSQL 55431，设置 `default_transaction_read_only=on`，仅按快照 experiment_id 查询 `m0_live_once` 的 business/outbox/usage/trace_status/cost_state/reserved_cny/attempts；返回一行，逐字段与脱敏快照一致。没有选择 approval_hash 或其他行。
- 对公开 `execution-result.json` 执行确定性比对：ledger 与允许的原始快照完全一致，CLI 结果及时间/退出码一致，条件费用算术通过。
- 本审查没有独立访问账号页面、授权原文、账单或供应商日志。执行前账号/费率复核与人工授权描述来自执行记录，不把它们写成审查者独立外部验证。

## 能确认的业务链与计数边界

`live.py:execute` 仅在两个响应均通过校验后设置 completed：首轮只有一个有效 read_fixture 调用，固定 target 匹配；`protocol.py:continuation` 验证工具 ID 配对及私有续接字段存在，按同 provider/Run 在内存续传；第二轮 finish_reason=stop、assistant 角色、无工具调用，最终 JSON 严格匹配固定 target/evidence_id。结合冻结摘要、两条 usage 及 PostgreSQL completed，可支持本次合成业务协议链完成。没有读取或保留原始模型正文，不能进行独立正文重放，也不据此宣称真实事故调查效果、断点恢复或全部 M0 兼容矩阵通过。

两响应 reported_model 均为 `deepseek-v4-pro`，仅证明响应报告此别名，不证明后端为独立固定的 0813 权重版本。

账本依次登记 project、model-1、model-2、trace-post、trace-read-1、trace-read-2 六个槽位。`Wire.request` 在 HTTP 发送前提交槽位，故六个槽位是已登记尝试，不能一概称为六个实际出站请求或六个响应。business completed 支持两次模型响应已收到并验证。当前调用路径无自动重试，槽位不得重复；证据范围不覆盖供应商内部执行次数或该进程以外的账号活动。

## Trace 的精确证据边界

1. **POST 2xx 是控制流推断。** 只有 trace-post 正常返回后才进入 trace-read-1；其返回路径要求读完整响应且 HTTP 状态为 2xx。因此现有后续槽位支持上传收到 2xx 的推断，但没有持久化原始状态码/响应，不能描述为直接观测到具体 200/202，也不证明平台已持久存储完整 DTO。
2. **第一次 404 无法确证。** 进入第二次读取说明第一次返回 None。代码既会把 404 映射为 None，也会对 2xx 的 JSON `null` 返回 None。不能据此确诊平台可见性延迟。
3. **第二次为何结束未知。** 非匹配的 2xx 对象会退出循环；非 2xx/404、JSON/对象格式错误、体积上限、超时、取消或本地错误也可能结束路径。槽位提交早于发送，因此最后槽位本身也不证明第二次请求已成功出站或收到响应。未保存安全失败码/字段比较结果，不能确定是 extra、DTO、HTTP 还是其他原因。没有 trace-read-3 不足以填补这些信息。
4. **业务持久记录与 trace 状态分离得到本次结果支持。** 当前业务/outbox 保留 completed，trace_status=unknown；不能据此称 trace 验证通过，也不能把平台失败的具体类别补造出来。CLI 退出 1 与完整实验判据一致。

## 费用与后续边界

两次 usage 合计输入 973、输出 97、总计 1070 tokens。按执行记录的输入 9 元/M、输出 27 元/M、全部输入 cache-miss 假设，`973×9/1,000,000 + 97×27/1,000,000 = 0.011376 CNY`，算术正确；这只是条件模型费用估算，不是已核账的实际总费用，不覆盖未知平台计费或供应商账单调整。

PostgreSQL 保持 `reserved_cny=2.00`、`cost_state=unreconciled`，CLI actual_cost_cny=null；支持全额未核账占用保留，不能声称实际消费 2 元、确认总账单低于 2 元或释放剩余预算。

原始结果缺少回读状态及安全错误分类，是阻止 trace 根因诊断的证据缺口。建议在后续独立工作项中先增加离线可测的固定分类，再按新有界合同只读诊断 trace；不重跑模型、不借原 one-shot 预算重发上传。本报告形成时执行记录已修正第一次 404 与全部槽位出站的过强表述，无剩余需要把本次结果改称通过的依据。
