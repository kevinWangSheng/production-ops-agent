# M0-02 normal03 独立控制窗与报告核查

状态：控制窗前提通过；主动调查完成并有 JSON 报告，核心有限结论有据；整份报告质量未通过（3 项 P2）。此结论不认证 M1 入口或产品健康。

## 边界与依据

fresh-context 独立评审，读取 AGENTS、SPEC、C3、round-02-contract.md 的最后 17 已用/normal03 仅 3 HTTP 合同。仅离线读取业务工件和工程恢复记录；未读取 .env、provider reasoning/private protocol，未发送模型、trace 或后端请求，未操作环境。共享宿主/worktree 不代表 OS 隔离或盲测。

环境目录：`/Users/shenghuikevin/dev/AI/production-ops-agent-m0-environment`。以下环境相对路径以此为根。

## 控制窗核查

- `docs/evidence/m0-real-environment/m002-normal-03-observation.json` 的 7 份 raw 全部重新计算 SHA256 和 bytes，与记录一致。
- `tmp/m0-environment/engineer-only/m002-fault-01/fault-timeline.jsonl` 记录 03:42:26Z restore；当前 `opentelemetry-demo-63649.../src/flagd/demo.flagd.json` 与 `flags-original.json` 原始字节相同，SHA256 `1ee5c0258e3e13609ecec75979cee286ab00f442d288112e39e3db8433d176c6`。
- 新窗 `1789011776..1789012076` 为还原后的 300 秒。payment transactions 的 5m increase ≈4.9996458584；checkout→PaymentService Charge 的 code0 increase=5、code2=0。是 Prometheus 外推增量，不能写成精确订单计数。
- checkout/payment trace 查询各返回同一组 5 个 trace，去重共 248 spans，其中 checkout 66/payment 10。逐 span error tag 检查无 true；不是 10 笔 payment 交易或全系统无错误证明。
- span metrics 同窗 checkout/payment ERROR increase=0，但 recommendation/ad 各 ERROR increase=1.25。无注入不等于全系统 healthy。
- logs frontend-proxy 总 hits=128，返回 20；报告必须区分 backend 命中、返回和实际模型可见行。
- 初始 question/scope 仅请求、身份、时间窗、来源 catalog 与通用语义要求，无最终观测值。checkout container/image/config 与 registry 对应项完全一致。registry 为先前捕获，未进行新 live docker inspect。
- 上游目录为解包目录，无 .git。`source-downloads.json` 保存固定 commit `63649d6d6a59de88fb421b88c3c3a6185b6d21ad` 的 codeload URL；本次独立重算 `otel.tar.gz` SHA256=`4bdfa245df27f224ff77c18eb29a2846a71528160a75a347448d1d67e1f3e9eb`，与下载记录一致。`m002-environment-resume.json` 另记启动前 freeze_images --check-only 通过；本评审没有重做 live image 校验。在解包目录执行 git rev-parse 得到的是上层工程 HEAD，不能当上游 commit 核验结果。

## 报告质量

实际 Run：`tmp/m0-environment/holmes-runs/m002-normal-03/`。3 个 HTTP ordinal 18/19/20 均 `response_received`/200，12 个工具查询，最终 stop，schema `m0-report-v1`、assessment completed/conclusion partial。最终请求 `m002-normal-03-http-20`。

独立重算全部 12 manifests 的 raw/view 文件 SHA256 及 canonical SHA256 均匹配；最后 delivered-business 的 messages hash 匹配，12 条实际 tool content 去掉 metadata 后逐个 JSON 与注册 view 完全相等；全部 claim evidence_ids 为完整 ID 且在该实际请求的交付清单中。没有以未发送 view 代替实际可见证据。

### 发现

1. **P2，claim 6、12 及 summary/gaps：把 trace 上限当实际显示数量。** e11/e12 `sampled_spans` 各实际 14，且是同一组 14 spans；`total_span_count=248`、`omitted_span_count=234`。报告反复说显示 20，20 是 selection cap/source query limit。应为 14/248，不是 20/248；不能把两个重叠 view 累加。
2. **P2，claim 5：缺少 accounting ERROR 序列被写成 ERROR=0。** e2 只有 accounting STATUS_CODE_UNSET=5，没有 accounting STATUS_CODE_ERROR。其他列举的零错误值、recommendation/ad 各 1.25、currency OK 13.75 均有相符序列。应删除 accounting 零值断言或标明没有返回该错误序列。
3. **P2，claim 7：把一条 trace 可见的完整路径扩展到另一条 trace。** e11/e12 中 trace `ef94f6ad35ad2a47fecf672989d8ad6f` 可见 frontend `grpc.oteldemo.CheckoutService/PlaceOrder` status0；trace `0486232d8e10ce9cb3fb014b79ef8ae0` 可见 load-generator/proxy/frontend HTTP200 链，但对应 gRPC span 没有显示。该 gRPC span 在 raw 中存在（span `d682e284237fca97`），这不允许报告称它是 displayed sampled span。应分别限定两条 trace 的可见路径。

### 逐 claim 核验（序号从 1 开始）

- 1–4：e2/e4/e8/e9 的 checkout span-kind、RPC client/server 窗口 increase 数值吻合。已限定外推增量而非唯一订单数；`rate query` 的叫法不精确，但所引 PromQL 明确是 increase，未用原始累计量冒充窗内事件。
- 5：如 P2-2，其余服务数值吻合。
- 6：5 traces/248 total、checkout66/max66424μs/payment10、聚合 visible-error-tag=0 均吻合；显示计数错误如 P2-1。
- 7：trace/parent 链在 ef94... 可见，048... 的 gRPC 段不可见，如 P2-3。
- 8–9：cart 显示20/总54、均 Information；proxy 显示18/总128，其中16个200、2个308，未见5xx，吻合。没有把未显示记录状态当已读。
- 10：EmptyCartAsync 时间和 trace_id `0486232d8e10ce9cb3fb014b79ef8ae0`、cart/proxy GET /api/cart 的 `78e17a0d6a3b4ab0e49f5e2499f609b9` 精确匹配；报告谨慎未声称可见 checkout span 归属，符合显示边界。
- 11：e10 聚合返回 payment≈4.9996458584，仅 service_name label，描述当前结果无状态拆分成立；没有把它泛化成原始来源永远无标签。
- 12：缺测/省略方向正确，trace 数量错误同 P2-1。
- 13–15：核心假设和排除假设显式限定 observed/visible evidence，不认证健康或恢复。正常窗内 checkout 的零错误 increase、实际成功 RPC 流量和部分 HTTP200 路径支持“未观察到 checkout 失败”的有限判断；无法证明完整无失败。
- 16：recommendation/ad span error increase 与 ad EventStream grpc code4≈1.24999 均有来源，并未当作 checkout 根因，作为另行人工跟进合理。

其他限制：无 SLO/基线、无 checkout 日志接入、返回 trace 总量/抽样之外未知均已披露；建议新增日志只能改善未来观测，不能追溯补出此历史窗不存在的日志。5m increase 已匹配300秒窗，外推与 scrape 边界不等于查询了不同窗。报告没有执行修复或发布门禁。

### 判定

主动协议/最终报告存在、核心正常窗结论有据、严格逐 claim 质量未通过。原始模型报告保持不改写；以上是独立评审，不替模型补写成功，也不授权新增 HTTP。最后额度已用完的判断由全轮账本负责，本评审只核见本 Run ordinal18–20。
