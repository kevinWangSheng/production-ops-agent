# PG 首阶段 tool_choice 兼容修复

日期：2026-09-10。仅离线修复，0 新模型请求。

父执行者已取得真实 HTTP 400：`Thinking mode does not support this tool_choice`，安全诊断见 [原失败诊断](round-02-pg-first-diagnostic.json)。原 Run `eb6996d4-ed44-447e-b33f-0a60f3efc323`、first_started 记录、完整受限 raw、3.44064 CNY unknown 均保留；本修复不改原输入或重新使用原 Run。

- 首次离线候选曾改 `tool_choice: auto`，未执行；依据父补充、执行者直接核查的[官方 V4 thinking 集成文档](https://api-docs.deepseek.com/quick_start/agent_integrations/oh_my_pi/)，最终完全省略 `tool_choice`，复用原 protocol-v1 fixture user prompt。该专用文档设置 `supportsToolChoice: false` 并说明 thinking 模式拒绝该参数；不能依据一般 API schema 假定显式 auto 可用。保持 explicit deepseek-v4-flash、thinking/high、同工具 schema；响应仍须 exact read_fixture/target，并完整提交后才消费工具结果。
- HTTP 非 200 先分类 API 拒绝，不误报响应 model 不匹配；原始响应先保存。已知 400 映射固定 `THINKING_TOOL_CHOICE_UNSUPPORTED`，其他非200为 `PROVIDER_HTTP_REJECTED`；安全 stdout 带 code/http_status，拒绝正文不导出。
- 新输入的实际复验必须由父在统一全轮账本新分配下创建独立新 Run/记录；本执行者没有调用或新建真实 Run。

验证命令：`/Users/shenghuikevin/dev/AI/production-ops-agent/.venv/bin/python -m pytest tests/test_m0_pg_live_probe.py -q`，输出 `7 passed in 1.32s`；Ruff check/format 已执行。新增 400 回归检查受限 raw 保留、固定原因、未模型采纳；旧身份拒绝等回归仍通过。

文件 hash：
- `scripts/m0_pg_live_probe.py` `fc6cc9607a7e3fe68973ab31a5760073b99b979a8638c57b28122d6d69fdee10`
- `tests/test_m0_pg_live_probe.py` `e28a93bc900eb6e23c30085faa1898e403d200035fcbb8afaa03efffc0c07625`

## 同源 content 兼容与最新验证

同一官方文档要求工具调用轮次的 assistant.content 非 null。driver 只在第二请求的 wire 构造阶段深拷贝已重建完整消息组，把 assistant+tool_calls 的 null content 转为 `""`；原 PG response/raw、tool-call IDs、reasoning_content 全部保留不改。记录转换布尔与 `v4-non-null-assistant-content-v1`，适配器版本更新；不在第一实际成功后再改代码。

null 哨兵离线测试确认除 wire content 外完全相等、原消息不变、私有字段不变。最新定向命令同上，`8 passed in 1.31s`，Ruff PASS；0 新模型请求。以前7项/auto候选为历史，不能作为最终省略参数版本的实际证明。

最新冻结文件 hash：
- `scripts/m0_pg_live_probe.py` `6d86a7dc64462709b33936b0d3eac0cb9ea5dba05795f1853a2937f57cc1fac6`
- `tests/test_m0_pg_live_probe.py` `587265a8b5556940b8961fadfe6ae00be77bfe17be9a424c7f3c2f154c1a7243`
