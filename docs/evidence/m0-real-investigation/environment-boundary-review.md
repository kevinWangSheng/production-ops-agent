# 环境与 Holmes 实验边界独立审查

2026-09-09；新上下文独立审查者，未参与实现/环境运行。范围是专属环境 worktree 中的 Holmes 执行器、read_proxy、prepare.py、实验合同，以及主 worktree 的 PG 控制探针/首片候选计划。未读取真实 .env 或私有 reasoning、未调用模型/trace、未操作部署/故障/数据库。本报告不认证真实调查效果或操作系统网络隔离。

## 当前结论与发现处置

**本报告所列已重现问题完成针对性修复复核；限定此开发实验接口范围，没有剩余已定位阻断问题。** gzip 最初待修状态作为过程保留，最终复验见末节。实际网络隔离、绝对工具清理、真实调查效果仍未由本审查认证。验证层次如下：

1. **累计请求账本可能并发覆盖/丢失预留。** 原执行器 read→write 无锁，write_text 非原子。已增加整 Run 持有 allocation flock，以及 pending 文件 fsync→replace→目录 fsync；坏 JSON 读取失败时停止。静态复核已确认，未做真实进程崩溃/断电测试。该文件是本次单 allocation 工程账本，不代表产品 PG 权限/预算架构已替代。
2. **响应读取缺少实时体积边界。** 原模型 response.read 无上限、工具读完才判大小。新增 bounded_send 流读取、模型 2 MB/工具 1 MB、finally close。独立抽取当前函数并用真实 httpx 合成流验证：超 2 MB 拒绝且关闭；不调用网络。
3. **新增 bounded_send 重建 gzip 响应重复解压（待修）。** iter_bytes 已解码，重建 httpx.Response 时仍带原 Content-Encoding，导致 DecodingError。独立 gzip 合成流在当前实现稳定重现，响应流有关闭。建议移除重建响应的 Content-Encoding/原 Content-Length 或等效正确保持原始编码，并重跑合成 gzip/大小边界。发现已直接交给执行者。
4. **ANSWER_END 被误当完整成功。** 上游 `holmes/core/tool_calling_llm.py:1320` 附近无 tool_calls 即 ANSWER_END，即使 finish_reason=length。执行器已检查 stop/非空 content，返回 investigation_returned 与 pending_independent_evidence_check；其余 incomplete。静态复核通过；调查质量仍须独立业务证据。
5. **Prometheus 瞬时 lookback 可能越过声明 start。** 原代理只限显式 range，未约束默认 lookback。已见 prepare 设置 --query.lookback-delta=5m，代理要求窗口至少300秒且显式 range 不超过窗口、拒绝 offset/@。独立合成检查确认短窗口拒绝且未调用 backend。实际运行配置是否更新须由环境运行工件核对；源码更改不代表容器已经重新加载。Prometheus 官方说明瞬时选择器读取 lookback 内最近样本：[Querying basics](https://prometheus.io/docs/prometheus/latest/querying/basics/#staleness)。
6. **后端 redirect 可越出固定 BASES。** 已用禁用环境代理的 opener 和 NoRedirect 替换默认 urlopen；独立合成检查 redirect_request 返回 None。尚未由审查者运行真实 HTTP 301/302 拒绝链路；环境执行者另负责其原始证据。

## 独立执行的检查

使用环境的 Holmes venv，仅抽取执行器 nested functions/导入不启动的代理 Handler，以合成 httpx Stream、假 backend、禁实际发送的原函数替身检查：

- 第5次 Run 模型请求、第13次 allocation 模型请求均在 transport 前拒绝；前4次登记占用，失败/重试继续受物理 send 计数。
- 2 MB 超限流拒绝并 close；gzip 重复解码失败已重现并报告。
- 错 integration→403，POST→405，短窗口→400，offset→400，错误 service→400；这些拒绝后 backend 发送次数为0。初次测试夹具把 do_POST 的 None 当返回值导致 TypeError，已改为捕获 reply；该测试搭建错误不计实现缺陷。
- 拒 redirect handler 返回 None。上述是离线函数边界测试，不是实际鉴权、完整进程崩溃恢复或网络隔离测试。

## 私有字段、费用和权限边界

- 当前同步 `DefaultLLM.completion` → LiteLLM → httpx.Client.send 由 wrapper 计数；SDK 参数 max_retries/num_retries=0，wrapper 对每个实际 send 另有4/12上限。未把 max_steps 当 HTTP 次数。只允许固定 DeepSeek host/path 和固定 localhost GET 工具入口；不宣称对全Python网络库或不可信宿主代码构成沙箱。
- 合同12 CNY/12 HTTP，每请求1 CNY占用、64 KiB请求、8192输出；费用字段只作峰值cache-miss估算，不是账单。调用前必须保留占用，低估算不得自动提高12次上限。上游独立 traces/callbacks 关闭；没有另行上传 trace 的执行分支。
- 上游 ANSWER_END 包含整个 messages/prompt，其中可能有 reasoning；执行器只摘最终业务 content，未序列化整体 event。已核查上游富 trace 分支受 HOLMES_LANGFUSE_ATTRIBUTES 控制、日志受 logging.disable 控制；这是受检路径源码证据，不是全依赖运行时出口证明。
- 工具20秒、代理10秒是客户端/socket IO timeout，不能单凭该参数声明绝对wall-clock硬超时或不合作子任务的有界清理。Run SIGALRM 在主线程执行，也不等于工具线程各自具有取消硬边界。报告/合同应准确保留这项未证实能力；不以当前正常查询时延认证产品 F2/F7。
- 代理 metrics 查询授权范围是整个专属 OTel integration；service 列表是配置，不能证明实际遥测或身份映射。loopback后端、显式工具列表与移除collector Docker socket/hostfs减少暴露面，但未证明宿主 Holmes 进程无法直接访问后端。容器内部网络测试若不覆盖实际 Holmes 执行位置，不能据此声明端到端隔离。
- prepare.py 保留数据卷、仅为实验工程配置；未给模型 Docker/socket/exec/写API。OpenSearch固定 `_search` 的POST是后端查询，不等于授权任意POST写入。只凭代理405不能认证后端本身无写权限。

## 控制探针和首片计划复核

已读 `scripts/m0_control_probe.py`、control-probe-contract.md、control-reconstruction-check.json 与 `docs/plans/first-vertical-investigation-2026-09-09.md`。探针仅验证最终业务快照及generation/epoch/status/lease SQL条件、PG重启保留；没有独立owner/Run校验、逐ModelStep/ToolOperation断点重建或取消后零发送。reconstruct对schema作ready/blocked选择不证明真实provider协议恢复。主任务已在合同明确4主体相对预记2主体的差异、owner/Run原定判据未完成、原始源码/结果保留；此澄清符合证据边界。

首片计划保持“候选、实施入口未打开”，列出真实身份、动态可见证据/最终人控版本、步骤恢复三处接缝；将完整UI/发布观察/72h soak留实施及后续验收。其文字没有把本探针或环境准备自动改成通过。仍须取得对应真实来源/权限、恢复/人控证据并冻结验收，方可另记录入口决定。

## 当前被审文件快照

环境工作区：`/Users/shenghuikevin/dev/AI/production-ops-agent-m0-environment`。以下摘要只覆盖本报告初次写入时当前文件；后续修复须补复验记录。

- `scripts/m0_environment/holmes_baseline.py` SHA-256：`1b4e755c642a7fd55132c52ec2d4e645bf5e564c97b142fe333e693f714ce76f`。
- `scripts/m0_environment/read_proxy.py` SHA-256：`50a872233b469c370f99de434931b8f2f7aec6f1a4620de72d91974cb2a853ab`。
- `scripts/m0_environment/prepare.py` SHA-256：`2003fcb00fcda7fdcf8db011d550c2856fa0bca009f68d77c9a1e8fc4eff7f53`。

## 最终修复复验

再次读取执行器并独立执行真实 httpx 的合成 gzip/超限流检查：gzip 正确解码为原 JSON、重建响应不含 Content-Encoding；超2MB仍拒绝；两条流均 close；0实际网络。原 gzip 重复解码发现关闭。当前源码SHA-256：`24f2bd94812c0ed974aa251aa8940093a619f8153aca3d112f4fe538195c35ac`。

同时静态核查请求前改为检查 run_stop（单Run与总截止较早值）、响应model精确匹配、finish_reason判据与合同将20秒称为客户端IO超时。不能据这些检查宣称全工具绝对清理或实际网络沙箱通过。后续执行结果与环境权限原始证据仍由独立业务/环境核查判定。

## 环境采集保全追加复核

`capture.py`和`observe_window.py`仅将输出目录mkdir改为exist_ok=False，位于发起query之前。已读取history-overwrite-denial.json：重复normal-ready/fault-01退出1，原记录SHA保持；控制流与工件相符。审查者未重跑或操作环境。此修改避免误用标签覆盖历史，不声明任意文件系统故障下的完整事务保证。
