# Flash JSON 模式修复独立审查

日期：2026-09-09。审查者为未参与实现的新上下文 Agent。范围仅本轮 normal-1 诊断、最终请求的 JSON 模式修复及相关回归；不覆盖后续真实工作负载调查、完整 M0 或产品验收。

结论：此范围内未发现阻断问题。真实修复前失败、修复后单次成功、严格拒绝围栏的离线回归均有证据。仅据本轮观测确认问题，不推断此前 Flash 失败原因，也不把一次成功视为非确定性可靠性证明。

## 依据与被审版本

- 阅读本 worktree 的 AGENTS.md、完整 SPEC.md、ROADMAP.md、本轮 [合同](contract.md)，以及 M0 计划实验合同/模型兼容条款、C3 模型适配与资源/数据出口/预算条款。
- worktree：`/Users/shenghuikevin/dev/AI/production-ops-agent-m0-01`。
- 审查 `git diff -- scripts/m0/live.py tests/test_m0_live.py`。实现仅新增最终请求 `response_format={"type":"json_object"}` 三行；解析、字段集合、目标和证据校验未改。初轮工具调用请求没有新增该字段。
- 运行代码摘要（`live.code_digest()`）：`7d8cd410de941b1b6c294a744e7976211ac3ec38078937579beffba7cbc48c0c`；本次重新计算与 started-2、result-2 的 outbox 摘要一致。此摘要覆盖既有 digest 定义中的源码/锁文件/fixture，不覆盖测试和诊断 wrapper。
- 单独复算诊断 wrapper `tmp/m0-real-investigation/run.py` SHA-256：`38f023e480257b78a3739b8a4c1f73714e3f5acf0e8e6d566b11fd19b7164660`，与 started-1/2 相同。wrapper 只增加最终 content 的本地观察，不改变请求或返回内容。审查未执行 wrapper、未读取 .env/旧 approval 或 provider 私有响应。

## 已核查原始证据

原始文件均在本 worktree 的 `tmp/m0-real-investigation/`，未将任意模型正文复制到此报告。

| 运行 | 诊断与业务结果 | 请求与状态 |
| --- | --- | --- |
| result-1 / final-diagnostic-1 | 最终 content 为 JSON Markdown 围栏；直接 JSON 解析失败，围栏内部严格等于固定 target/evidence_id 合同；`LIVE_FINAL_JSON_INVALID` | 2 次模型，无 trace 上传；2 CNY 未核账预留 |
| result-2 / final-diagnostic-2 | 最终 content 为裸 JSON 且严格匹配；`LIVE_PROTOCOL_COMPLETED` | 2 次模型、1 次 trace 上传、2 次回读；`TRACE_VERIFIED`；2 CNY 未核账预留 |

- 第一轮 experiment ID：`011db117-4eb6-47ac-aab1-c092c2d970b6`；第二轮：`60eef401-f408-4e4d-bf57-aa5a32a3a41b`。两轮均 reported_model=`deepseek-v4-flash`；不能证明不可变后端权重。
- result-2 记录总耗时 10.7923765 秒；这是原始运行记录，非审查者重新调用测量。
- 审查者独立运行专属 PG 身份核查，随后通过 `default_transaction_read_only=on` 对这两个 experiment ID 查询 `m0_live_once` 与 `m0_live_diagnostics`。归一化 deadline 时区表示后，两轮原始业务行和诊断行均与对应 result 工件完全一致。未修改数据库、未启停服务、未访问模型或远程 trace。
- `final-diagnostic-1/2.json` 和 `result-1/2.json` 权限均为 0600。两轮原始失败/成功工件均保留。未独立比对全部历史数据库行，不能据本报告宣称全部历史行均已复验。

## 确定性检查

独立运行：`.venv/bin/python -m pytest tests/test_m0_live.py -q`，结果 **75 passed in 1.83s**。

新增测试捕获真实构造的 HTTP 请求 body，断言仅第二次模型请求设置 JSON 模式；即使模拟返回本轮已观察的正确 JSON 围栏，仍得到 `LIVE_FINAL_JSON_INVALID`，且没有 trace-post。现有合同检查仍通过。此测试验证协议边界，不读取模型思维链。

另核查实现者保留的 `json-mode-red-valid.txt`：失败点明确为第二次请求缺少 response_format，能够发现缺少修复；这是已检查的历史红测工件，并非审查者重新回退代码执行。其他红测搭建错误不计入有效回归证据。未在本审查中运行全量 make check。

## 权限、出口与费用边界

- 修复不增加工具、外部 endpoint、重试或权限；固定工具仍校验 read_fixture/精确目标，结果仍必须引用固定证据。
- 认证仅用于相应客户端请求 header；trace 继续通过既有白名单 DTO，最终模型正文和 reasoning_content 均未加入 DTO。provider 私有协议字段只由既有 continuation 在同 provider/Run 内续接。
- wrapper 只检查最终 content，不读取 reasoning_content。只有解析后严格匹配已知合成合同的裸 JSON 或其围栏形式才会保存原 content；其余仅保存结构标志。文件用 O_EXCL/0600 创建，既有诊断不覆盖。
- 既有 Wire 保留每个 slot 单次、两次模型请求、16 KiB 请求/128 KiB 响应、180 秒模型/10 秒其他 HTTP 和绝对截止控制。上述修复没有削弱限制。
- 本轮两例合计 4 次模型和 1 次 trace 上传；已核查的两行合计保留 4 CNY 未核账。实际费用仍未知，不能据 token 数或成功状态释放预留。全局累计额度由主 Agent 账本管理；本报告不认证后续环境实验额度或供应商账单。

## 结论边界

已支持的结论是：本轮失败观测为 Markdown 围栏破坏最终严格 JSON 合同；增加最终请求 JSON 模式后，单次真实 Flash 固定 fixture 链路完成且 PG 记录 TRACE_VERIFIED。审查者依据本地原始工件与 PG 复核该记录，没有独立远程下载 trace。未验证真实事故诊断质量、完整恢复/取消矩阵、长期稳定性、soak 或产品功能通过；SPEC 实施门槛及 feature passes 不因本报告改变。
