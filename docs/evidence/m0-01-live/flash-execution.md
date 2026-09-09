# Flash 单次真实实验：最终内容合同失败

2026-09-09；[冻结合同](flash-contract.md)、[运行版本](flash-version.json)、[实际脱敏结果](flash-execution-result.json)。本次结果 **未通过完整链路**，不是 Flash normal 成功，也不改变任何产品验收。

## 实际结果

运行基线 `55539bc3262260840c2f6a92201c1478c8443255`，源码/锁/fixture 未修改时执行；只有合同/状态文档为 dirty。CPython 3.12.13 与锁定依赖闭包实际核对通过。运行 14:05:09.466543–14:05:13.652583 UTC，耗时 4.185888 秒，CLI 退出 1。

两次模型请求，响应报告均为 `deepseek-v4-flash`。PostgreSQL attempts 为 project/model-1/model-2，usage 有两条，business=failed、business_code=LIVE_PROTOCOL_FAILED、trace=pending、trace_code=TRACE_NOT_ATTEMPTED；业务、outbox 和诊断在新连接回读一致。没有 trace POST/GET。首轮工具参数校验、固定 read_fixture 和同 Run 消息配对续接已通过，否则不会进入第二次模型请求。

第二条 usage 仅在最终响应结构校验通过后记录；两条报告模型与批准 alias 一致。因此按运行源码控制流可定位为最终 `json.loads(content)` 或精确 target/evidence_id 对比失败。这是基于持久记录和固定源码的定位，不是对原响应正文的直接检查。原入口没有保存该正文，进程已退出，不能恢复其中的具体格式/字段，也不能从 39 个输出 tokens 推断内容。未执行上传，所以 LangSmith 没有本次 Run 可供进一步回读；读取旧 Pro trace不能回答新 Flash 内容问题。

## 已确认的诊断缺口与修复

原入口把 JSON 无法解析、非预期结构、target 错配及 evidence_id 错配全部折叠为 LIVE_PROTOCOL_FAILED。新增四个固定失败分类，经既有同事务 diagnostics 路径持久化；不记录任意响应正文、未知字段或私有 reasoning，不改变成功判据、请求内容、重试或上传权限。

离线构造 8 种第二轮内容（含 Markdown 围栏、非 JSON、非对象、缺少/多出字段、目标/证据错配及秘密哨兵），通过完整 execute 入口复现分类丢失：修复前 8 failed；修复后完整 make check 264 passed/17 默认 PG skip。围栏只是覆盖用例，**不声称真实 Flash 本次输出了围栏**。原始失败日志保存在本 worktree ignored 的 `tmp/m0-01-flash-live/diagnostic-red.txt`，修复后输出在 `check-after.txt`。

此修复只解决今后无法区分失败原因的问题，未证明本次真实响应能通过，也不追溯修改本次数据库错误码。未增加 response_format、不宽松抽取 JSON、不修改 fixture 或验收条件。后续如执行新实验，需要新的身份/批准/hash并明确追加请求范围，不能用原已消耗两次请求的批准自动重跑。

## 次数与费用

前置 4 次只读 GET：模型目录、DeepSeek 余额、LangSmith 项目和 settings 均 200；live 为 1 项目 GET + 2 模型 POST；收尾 1 余额 GET。共 8 次 API HTTP 请求，其中模型 2、上传 0、trace 回读 0；无重试。

输入 976、输出 118、总计 1094 tokens。按当日官方峰值、输入全部 cache miss 估算 0.003990 CNY；实际运行处于北京时间 22:05 空闲时段，按该时段全部 cache miss 估算 0.001995 CNY。未保存缓存细分，不能当实际扣费。账户余额前后显示差额 0.00，属于聚合/显示精度及可能延迟，不表示免费或完成对账；可归属实际模型费用仍 unknown。trace 未上传，本次上传费用 0。

旧 Pro 2.00 CNY + 本次 Flash 2.00 CNY = 两个真实实验合计 4.00 CNY 未核账占用，均不释放。数据库另有合成测试账本，不能将全表 reserved 求和当真实支出。

## 前提与保全

API 核对 default project/tenant/name 匹配，模型目录含显式 Flash，CNY 余额足够。浏览器扩展读取超时后使用原生 Chrome UI：当前 Developer Free、1/5000、Add card to remove trace limit；default 项目 retention 14d、No automations found、Evaluators 表无数据行。没有新增 workspace/project、支付方式、套餐或自动化。

专属 PostgreSQL 17.9 在旧 M0-01 worktree 的 `tmp/m0-b/postgres`，55431/m0_budget。运行前已有业务/诊断表可查询，无 live 安装/迁移。前置显式 PG 测试 4 passed；这些既有测试会在专属实例创建随机合成行和缺表测试 schema，未将其误记为真实实验或产品 migration。运行前快照中的 22 条行逐一哈希一致，含旧 Pro 实验；原 Pro 批准及证据未覆盖。

私有批准、API 元数据响应、CLI、余额及旧行快照仅在 `tmp/m0-01-flash-live/`，0600/0700；主 `.env` 只更新本轮非秘密预算/期限，key 不复制。收尾服务停止、独立审查及 PR/CI 状态在任务记录接续；未完成的完整链路需新的执行边界决定。
