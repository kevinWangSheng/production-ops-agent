# 严格报告 runtime 独立复验

2026-09-10，新上下文独立审查者接续已通过的有界设计，直接读取并执行候选，未参与 runtime 实现。仅本地合成传输；无真实模型、后端、trace、PG 或 Colima 操作，未读取真实私有字段、凭据或 `.env`。

## 固定范围

- `scripts/m0_environment/holmes_baseline.py` SHA-256：`89e4a26d5c997f734cf6a0386733fb3e1528fc12bc82a7623ecb85d117328d85`
- `scripts/m0_environment/report_contract.py` SHA-256：`f5d0adf01c48b0ad404f3737ee9d9d81598f38c583f1f15f2c9b3b125267546e`
- 依赖 Holmes checkout：`5e983c17f30e93099c7d775167266d4cd1d586c4`，只读借用环境 worktree 已有 `holmes-venv/bin/python`。
- checker/bridge 仍在联合收尾。本复验不代替最终统一快照/schema/严格 bridge 审查。

## 亲自执行与核查

运行命令：

```sh
HOLMES_TEST_UPSTREAM=/Users/shenghuikevin/dev/AI/production-ops-agent-m0-environment/tmp/m0-environment/holmesgpt-5e983c17f30e93099c7d775167266d4cd1d586c4 /Users/shenghuikevin/dev/AI/production-ops-agent-m0-environment/tmp/m0-environment/holmes-venv/bin/python tests/fixtures/m0_environment/strict_runtime_probe.py
.venv/bin/python -m pytest tests/test_m0_runtime_strict.py tests/test_m0_trace_view.py -q
```

结果：探针4组成功退出；10项定向测试通过。探针运行真实固定 Holmes 循环与 `httpx.Request`，仅替换 pipe transport 和 dotenv 值为合成测试值。断言实际 Content-Length、请求字节 hash、完整 business/context hash、同 physical request 的 catalog/policy/per-view refs；合成 private 协议续传且不进业务记录；合法显式 target、错误 target 拒绝；有/无独立初始 timing；越 scope 初始输入在任何模型请求前拒绝。模型计数为合成响应次数，真实 HTTP 为0。

另独立扩展探针：在内存载入同探针，将可信 policy revision 增至530000字符，经真实 wrapper 的 `prepare_wire → envelope_check → dispatch` 路径运行，确认返回 `model request bytes denied`、model_http_requests=0、tool_queries=0。没有修改实现或仓库测试。首个扩展脚本错误地期待 `model request envelope denied`，断言失败；实际程序已经按 bytes 拒绝。修正测试期待原错误码后复跑，保留原失败说明，不能将首个脚本失败归为产品缺陷。

源码核查确认目录在真正出站请求中追加后才运行原字节及token估算限制；完整响应后才记 collection_completed_at；dispatch/response为客户端边界，原 observed_at 仍为操作开始。source_timing取实际可见日志事件或 span 起止，未把 Prom 求值时间、HTTP 返回或旧 observed_at 填作 source/capture 证明。initial sidecar按view hash绑定且事件时间必须与可见字段一致；没有凭question自述补时间。默认v2及显式legacy保持分派。

## 结论与局限

固定两份 runtime 候选在上述离线范围内通过独立复验，未发现本范围未处理的实质问题。runtime 的 `investigation_returned` 明确只是候选报告返回，时间适用性/输出权威/完整质量仍由v4最高接缝与独立全文审查承担。

这不是新版真实模型调用、真实后端兼容性、初始raw完整来源、自然语言报告质量或产品验收通过。旧真实报告质量FAIL、已用20请求与SPEC门槛不变。最终交付必须覆盖联合稳定源码、schema、旧报告legacy保真及strict拒绝边界。
