# #428（#243 PR-2）产品模式 trace outbox 真实证据（OB-R13，2026-11-02）

类别：集成运行。本机 colima `m1-kind` VM，Compose 项目 `opspilot-393`，`fixture` 工具档，真实 DeepSeek，真实 LangSmith SaaS（项目 `opspilot-lab-428`）。不是生产证明，不是 soak。冻结摘要见 [summary.json](summary.json)。合同：#428 Agent Brief、合同 r3「#243 产品模式 trace outbox」节（Q14–Q17、OB-R3–R13）、运行计划与 lead 裁决（仓库外 `~/.agent/handoffs/m3-batch4-2026-10-31/run-plan-428.md`）。

原始记录不入库（ADR-0006），在仓库外 `~/.cache/opspilot-428-run/`（`timeline.log`、`kill-tries.log`、`metrics-*.txt`、`payloads-*.json`）。不含 key、口令或令牌值。worker_env 只按路径交给回读脚本，在其进程内读 LangSmith 设置，没有打印、复制或 grep（K2）。

## 本目录文件

| 文件 | 内容 |
|---|---|
| `summary.json` | 冻结摘要：构建、四个 Run、两次故障注入、两个未确认项结论、载荷抽查、成本估算、收尾 |
| `rows-A.json`、`rows-B.json`、`rows-C1.json`、`rows-C.json` | 各 Run 的 outbox 行（kind、idempotency_key、state、attempts；无载荷） |
| `readback-*.json` | `scripts/outbox_trace_check.py` 对各 Run 的 LangSmith 回读（只有 ID、计数、固定码、链接） |
| `payloads-A.json` | Run A 八行的完整载荷样本（只有 Q14 白名单字段） |

## 构建与开关

- HEAD `9077506a`，`install.sh upgrade`（`OPSPILOT_TRACE=outbox`）构建 `opspilot:local`，镜像 `sha256:3295a902…`，替换 4f 留下的 #386 分支镜像。三个运行时 `opspilot_build_info` revision 均为该 HEAD。
- worker `/metrics`：`opspilot_trace_export_enabled{reason="ENABLED"} 1`。
- 开关打开 4 秒内，PR-1 以来已写但从未投递的 22 行（3 个旧 Run）一次投完（07:07:32Z）。

## 开窗时的事故与目标选择

`checkout-prod` 有两个开放事故：`606d9341`（lifecycle open，Run completed，2026-10-31 13:14Z）、`d67c3cd0`（lifecycle open，Run waiting_human，2026-11-01 08:22Z），都不是 4f 留下的。新告警会关联进开放事故而不开 Run，按计划 §2 与 lead 裁决改用演练目标 `checkout-428a…f`，没有关闭这两个事故。演练目标不在 fixture 工具登记表里，**所有 Run 都没有成功的工具调用**：每个 Run 两次 `metrics_range_query` 被拒（`denied` / `TARGET_NOT_REGISTERED`），以 `INCOMPLETE_INVESTIGATION` 交接为 `waiting_human`。`target_identities.json` 临时加了 6 条，结束后恢复原字节（sha256 `1714ab7b…`）。

## 验收步骤

| 步骤 | 结果 | 出处 |
|---|---|---|
| Compose 有界真实 Run，`OPSPILOT_TRACE=outbox`，冻结摘要 + trace 链接 | 通过。Run A `f18affc0`（事故 `79255a70`），[LangSmith trace](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/1a1a92bb-976f-4dd7-8a68-283c492b3b61/r/00000000-0000-0000-cd90-119757d3b947?poll=true) | `readback-A.json` |
| 在 LangSmith 按 `incident_id` 与 version 筛到 token、工具错误码、状态 | 通过。按 incident 筛到 9 个 run，加 `image_revision` 筛到 6 个（model_step 与 tool_result 带镜像字段；run_state 行与根 span 来自 0024 触发器，不带镜像字段，属 PR-1 已知形状）。token 合计与载荷逐项一致（7051/2032）；`tool_reason` = `TARGET_NOT_REGISTERED` ×2；`run_state` = running、waiting_human | `readback-A.json`、`payloads-A.json` |
| 故障注入①：LangSmith 不可达 | 通过。worker 带 `compose.drill-trace-egress.yaml`（`api.smith.langchain.com:127.0.0.1`）重建，约 57 秒。Run B `38012555` 终态、交接原因、预算 3/100、工具用量与 Run A 相同；期间 pending 升到 8、attempts 到 4、`last_delivery_age` 为 `+Inf`、丢弃 0，日志只有 `error=URLError`。去掉 override 重建后 35 秒内 8 行全部补投（attempts 4–5），丢弃 0 | `rows-B.json`、`readback-B.json`、summary `fault_1` |
| 故障注入②：投递中途重启 worker | 第 3 次命中。Run `7da57ca3` 的 `run_state:…:1` 被领取、在途时 SIGKILL worker；重启后租约到期重领重发（attempts 2）。LangSmith 上该 span **只有一条**。第一次 POST 是否在被杀前到达 LangSmith 无法从我方观察（`read_run` 不返回接收时间），因此结论与「平台去重」或「第一次没送到」都相容。被杀的 Run 在原租约到期（07:24:39Z）后由新 worker 续跑，07:24:55Z 停在 `waiting_human` | `rows-C.json`、`readback-C.json`、summary `fault_2` |
| 载荷抽查无内容字段 | 通过。24 行载荷键全部在 Q14 白名单内；`reasoning`/`query` 字样只来自白名单键 `reasoning_tokens` 和工具名 `metrics_range_query`。四次回读的内容扫描全为 0（reasoning_content、x-api-key、authorization、bearer、gen_ai.prompt/completion、promql、key 字面值、非空 inputs/outputs、白名单外 metadata） | `payloads-A.json`、`readback-*.json` |
| 两个未确认项 | 根 span 晚到：**可接受**（Run A 子 span 07:09:28–32Z 投递，根随停下状态行 07:09:43Z 投递，LangSmith 上 8 个子 run 全部挂在根下）。重复 span：**部分回答**，见上一行 | summary `unconfirmed_items` |
| 成本（Q17） | 估算已记，**余额差待用户提供**。4 个真实 Run、12 次模型请求；LangSmith 平台估算合计 $0.0086241（A 0.00227685、B 0.0020328、C1 0.00210495、C 0.0022095）。估算，权威为余额差 | summary `cost` |

## 故障②的三次尝试

| 时间（UTC） | 尝试 | 结果 |
|---|---|---|
| 07:14:19 | 1，Run `7c451da1`（事故 `e8086303`） | 等待循环已看到在途行，但辅助脚本把 `until` 循环的退出码 1 误读为未命中，没有发 kill。该 Run 正常结束，8 行一次投完；回读 `readback-C1.json` 通过 |
| 07:16:51 | 2 | 目标键 `d` 尚未加入 identities，告警建了 `TARGET_UNRESOLVED` 交接事故 `ff2d3a9b`，没有 Run、没有模型调用 |
| 07:17:39 | 3，Run `7da57ca3`（事故 `6e54ab61`） | 命中，见上表 |

## 回读脚本在真实数据上的两处修正

首次真实回读退出码为 1（`CONTENT_PRESENT`、`METADATA_OUTSIDE_ALLOWLIST`）。逐项查看后都是 LangSmith 自己加的数据：每个 run 的 `OTEL_TRACE_ID`/`OTEL_SPAN_ID` 元数据（`scripts/lab_evidence.py` 也按它筛选），以及 llm run 上由 `gen_ai.usage.*` 生成的 `usage_metadata` 输出（只有 token 数）。分类器据此收窄并补了测试（其余 inputs/outputs 仍算内容），重跑后四次回读退出码 0、无失败码。同次实测确认 LangSmith 的 `trace_id` 等于根 run id，run id 是 OTLP span id 补零成的 UUID（32 个推导 ID 全部找到）。

## 偏离与局限

- 没有成功的工具调用（见上）；fixture 工具档，不是 otel-demo。
- 故障②的「是否去重」只能部分回答；按 lead 裁决不加确定性探针，重复安全的结论依据 PG 并发测试。
- kind lab Prometheus 的健康检查返回 401（auth 代理，健康脚本没有读者凭据），本次 fixture 工具档不依赖它；Jaeger 正常，15 个演示服务在线。
- LangSmith 项目的 14 天保留与 DeepSeek 价格条目是平台配置，本次没有读取；成本估算非空说明价格条目存在。
- `install.sh upgrade` 把 `OPSPILOT_TRACE=outbox` 记进了该安装的 `install.env`，之后同一安装再 `upgrade` 会保持 outbox 模式，除非显式传 `OPSPILOT_TRACE=off`。
- 收尾：web、worker、observer 已停，postgres、数据卷与密钥目录保留；worker `extra_hosts` 为空。
- 未做：独立审查本证据；全量 `make check`/pytest（窗口内按规则不跑）。F8 `passes` 保持 false。
