# M0-02 实际 Holmes 安全业务记录到 v3 的离线桥接

日期：2026-09-10。状态：实现者自测通过，待独立最终复验和新的实际 closed-report 工件；不是完整产品验收。实现位于 `scripts/m0/holmes_bridge.py` 与 `outcomes_v3.py`，没有网络、模型调用或 private-protocol 文件读取。

## 当前接缝

- 消费实际 Run 的 configuration、deployment-registry、input-business、observations、逐 ToolOperation raw/model-view/manifest、delivered-business、最终安全 response-business/result-business。
- 从 observations 登记顶层 operation ID，再匹配固定 `tool_call_metadata={...}{view}` 结构。metadata tool name/call ID 与实际 tool message、登记 operation/view 必须对应；不递归搜寻遥测中的 evidence_id。重复 ID/call、尾随歧义、未登记 view 拒绝。
- 完整 raw 文件 UTF-8 bytes hash、manifest canonical raw hash、canonical view hash、view 文件 bytes hash分别核对，不把不同序列化的 hash 混用。
- 以传入的已冻结源码 SHA-256 加载环境 worktree 中三个纯函数 `bind_identity/trace_projection/log_projection`，按 registry/context hash重放；不执行 Holmes harness main，不另抄一套 trace/log投影。保存的模型 view 必须等于重放结果。source hash标识本次重放实现，不能单凭它断言整个历史运行器版本已经证明。
- `business_projection_hash`校验实际 user/tool消息投影；`full_wire_hash`保存可信 gateway记录的完整请求哈希，桥接不读取含私有协议的完整 wire来重算。两者明确分开，不向 OpenAI payload添加自定义顶层 evidence_views。
- `report_request_id`唯一绑定同 Run、同步骤的最后物理请求与已收到完整最终响应。不同重试不并集可见证据；原始 Scenario只含原始请求/关注主体，initial_views为空。
- 只接受实际最后响应的 `m0-report-v1` JSON。incomplete要求inconclusive+gaps，supported要求引用事实；引用必须在这一次请求实际交付view集合中。非JSON/DSML、未知引用或响应关联不成立拒绝，不把旧失败改为通过。

## 身份和动作的证据边界

ComposeTarget保留不可变container/image、config_revision和registry映射。无可靠实例映射的metrics等使用IntegrationTarget，service_identity为unknown并保留可观察service标签；不能通过目标相等比较来满足Compose实例事实。

一条ToolOperation只生成一条动作记录。`attempted/authorized/executed`均为三态：已有proxy HTTP响应可证明attempted=True；没有响应只知道operation已登记，attempted=None。当前记录不含独立后端执行/权限决定，authorized/executed留None；不从HTTP200推断全部后端行为，也不从timeout推断没有请求。这一保真合同不替代实际权限探针。

closed report当前没有claim.target，模型自然语言里的实例归属仍须独立质量审查。只有显式scope target的claim才能执行Compose/Integration确定性目标匹配；不能宣称所有自然语言实例事实已经自动验证。

Holmes baseline没有PG主体控制日志。bridge的final_generation/controls是可信调用方提供的外部参数，baseline无控制时为0；不从模型报告取值，也不把该默认值称为同一真实Holmes Run已集成PG最终控制水位。PG控制机制证据属于独立步骤恢复实验。

## 验证

```sh
/Users/shenghuikevin/dev/AI/production-ops-agent/.venv/bin/python -m pytest tests/test_m0_pg_live_probe.py tests/test_m0_outcomes_v3.py tests/test_m0_holmes_bridge.py tests/test_m0_outcomes.py -q
```

加入动作三态后的实现者结果为 `101 passed in 1.46s`（包含当时7项driver测试）；其后driver新增null wire兼容单独8项通过。桥接测试覆盖实际结构、最后physical attempt、raw bytes/registry/source篡改、DSML/未知引用拒绝、遥测伪ID、重复tool消息、Integration无法证明实例事实以及HTTP响应/timeout动作三态。定向Ruff通过，文件hash见 `round-02-bridge-driver-fix-hashes.json`。未为测试读取任何实际provider私有内容。

对既存 `m002-normal-01` 只读核查曾确认16份实际工具工件可按交付结构重建；接入严格closed report后结果明确是 `FINAL_REPORT_SCHEMA_INVALID`。其pure DSML失败不回填passed、不改旧工件。新的normal/fault实际closed报告尚待父执行者提供，再由独立Agent验证。

离线命令（必须由已固定运行记录提供投影源码hash）：

```sh
/Users/shenghuikevin/dev/AI/production-ops-agent/.venv/bin/python -m scripts.m0.holmes_bridge --run-dir <实际Run目录> --projection-source-sha256 <已冻结源码SHA256> --output <新结果文件>
```

输出仅安全合同摘要/拒绝码；输出文件使用exclusive create保留历史。`contract_consistent=true`只表示记录、引用与版本化合同一致，不证明因果质量、完整恢复或产品功能通过。

## 实际 normal02 固定错误消息与版本重放修复

独立实际检查发现失败工具消息有固定 `Tool execution failed:` 加两个实际换行前缀，来自 pinned Holmes `holmes/core/models.py:98`。parser只接受此固定前缀，并要求顶层view在trusted operation登记且确有error或非200状态；不扫描任意错误文字后的JSON、不接受嵌套伪ID。实际安全e10消息已作为 `tests/fixtures/m0/holmes-error-tool-message.json` 回归fixture。

新增 `--projection-source-file`/`projection_source_path`，由可信调用方明确指定该Run冻结源码，仍强制SHA一致，不从遥测记录接收代码路径。原normal02使用精确`7fd7326644b26fe00d0a8bad25aab4b865dfbd453bb5a7b4fa491eda5539a676`不可变源码重放；当前新日志v3不能重签旧view。Artifact/EvidenceView保留manifest projection_revision，新metrics-v1与logs-v3调用各自冻结纯函数并核版本。

该批定向输出 `17 passed in 0.11s`。中间1个合成timeout测试仍使用缺少固定ERROR前缀的旧fixture，正确被新检查拒绝；将合成fixture对齐真实上游格式后全部通过。当前hash在 `round-02-bridge-prefix-fix-hashes.json`。

normal02实际已成功构建15份raw/15份最后交付view的packet，绑定`m002-normal-02:http:11`且initial_views=0，见 `round-02-normal02-bridge-result.json`。checker仍保留`FAILED_EVIDENCE_AS_FACT`：该报告把“三个查询被拒/data withheld”作为fact引用失败工具记录；当前v3沿用的笼统fact须ok限制也拒绝这种工具失败事实。此处未为结果放宽规则，交独立审查区分合同限制与报告质量。normal02已知独立质量失败不因消息解析修复而成为通过。
