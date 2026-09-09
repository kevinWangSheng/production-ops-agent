# M0-01 首条真实链路：normal-1（已批准并执行一次）

当前状态：用户已确认原范围/2CNY/期限，首条已执行，模型completed但trace unknown，整体未通过；见[执行记录](execution.md)。以下为运行前冻结的方案与当时前提，不授权再次运行。

2026-09-09；依据 SPEC 实施门槛、C3 §5/11/12/13、M0 §1/2/6/7及原 M0-01 任务。本轮仅实现并离线验证实验入口；无付费调用、无 trace 上传，不改变产品验收。

## 有界合同

- 单一合成 normal-1，使用 protocol-v1 的 messages/tools/evidence；最多两次非流式 DeepSeek 请求，thinking/high、deepseek-v4-pro。单次输出最多4096 tokens、发送 JSON 最多16384 UTF-8 bytes；只允许一个 read_fixture 调用和固定目标。首轮完整响应在同一进程内验证后才执行假工具，私有 reasoning 仅内存同 Run 续传。
- LangSmith 现有默认 workspace，现有项目：先无 workspace override 地读取指定项目，核对项目 UUID、name、tenant UUID；不创建 workspace/project。项目不存在或身份不符即停止。US区域与现有default项目已通过登录页面核对；一次只读项目GET确认现有key可访问默认workspace及项目，未创建资源。DeepSeek key存在，模型有效性未验证。
- 首次预算建议总额 **CNY 2**，一次性持久占用全部额度；最多1次项目 GET、2次模型 POST、1次 trace POST、3次 trace GET（仅404可继续读），无自动重试、重定向、代理或自动 tracing。登录账号实际显示Developer Free、0/5000 traces，default无evaluator行且明确无automations；正式执行前复核额度/规则未变，不开通付费套餐。
- 单模型请求180秒、其他HTTP请求10秒、整个进程有效执行期限最多420秒，同时受审批绝对截止时间约束。建议审批截止 **2026-09-11T06:59:59Z**（洛杉矶9月10日23:59:59）；若审批时已过期，重新给出具体期限，不静默顺延。
- DeepSeek官方2026-09-09中文峰值价格：未命中输入9元/M、输出27元/M；工程费率上界12/36。以每请求32768输入tokens（含协议/工具结构裕量）、4096输出tokens（含thinking）估算每请求≤0.540672元，两次≤1.081344元。32768是本实验体积限制下的保守工程假设，正式执行前仍核对价格/账号币种；非人民币账号需重算。平台保守预留0.10元及剩余裕量；当前账号免费额度足够一条base trace，执行前重新核对额度/规则。DeepSeek登录账号显示CNY余额足够本轮，无需充值。
- 全部2元在独立PostgreSQL实验记录中保持未核账占用，不因token估算/失败释放。单次授权引用及experiment ID唯一，抢占原子提交后才出站，重复启动和中断一律handoff，不自动重发模型。代码/fixture/锁文件摘要、账号key指纹与目标身份绑定私有批准文件，仓库不提交已批准文件。文件是工程操作者记录真实人工授权的媒介，不是对恶意本机操作者的安全隔离。
- 业务结果和白名单outbox同事务提交后才上传；仅固定标识、计数、状态及版本摘要，不上传prompt/响应/私有协议/凭据。平台失败保留业务结果和pending/unknown记录。首条不做自动恢复导出；后续trace-only恢复须另定有界合同，不能靠重跑模型恢复trace。

## 判据与证据边界

正常通过须真实返回一个有效工具调用、配对续接的最终JSON匹配target/evidence_id、白名单trace在正确项目回读一致。任一条件不符记录failed/handoff；不得解析思维链判断成功。HTTP尝试数、业务/trace各自状态、token数（有则记录）、未知账单占用及版本进入本地PostgreSQL；不打印原始错误。

本轮测试用内存HTTP和专属本地PostgreSQL，不加载真实key到测试进程。独立审查覆盖授权拒绝、账号错配、请求上限、deadline、泄漏、并发claim、重启拒绝和trace失败保留业务记录。

原任务各例重复3次及流中断、工具错误、压缩、受限协议持久恢复、平台恢复导出仍未完成。本次normal-1是运行前冻结的首个单次连通性子批，不替代原矩阵。没有真实运行前不得称服务连通或M0通过。

## 当前依据

- main/origin/main：ad99e6a；#4–#11已合并；main CI 34330344820成功。
- [DeepSeek人民币价格](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)、[thinking协议](https://api-docs.deepseek.com/guides/thinking_mode/)（2026-09-09查阅）。
- [LangSmith默认workspace规则](https://docs.langchain.com/langsmith/administration-overview)、[地区](https://docs.langchain.com/langsmith/regions-faq)、[价格](https://www.langchain.com/pricing)（2026-09-09查阅）。锁定SDK的read_project使用/sessions/{id}，create_run使用/runs；真实后端兼容性尚未运行。

## 已核实的配置与新发现

2026-09-09只读UI核对US endpoint、Developer Free（0/5000）、唯一现有default项目、Workspace 1；已通过一次GET项目元数据核对精确project/tenant标识与key默认范围。精确账号UUID及key指纹保存在ignored的0600 `tmp/m0-01-live/approval-draft.json`，不发布到公开仓库。主工作区.env仅追加缺少的endpoint/project/workspace，原key和其他行保留；预算仍未配置为正，deadline与授权仍未填。草案approved=false、billing_checked=false，不能运行。未执行模型调用或trace上传；这一次免费只读元数据GET与浏览器页面读取不是付费实验。

DeepSeek账户用量页现显示公告：约北京时间9月10日发布V4.1 Flash，之后V4-Pro请求会路由到V4.1 Flash并按其价格计费。此为已读取的账户公告，生效时间/实际服务端版本尚未知；现有公共价格页仍列V4-Pro-0813。执行前需复核实际路由并在人工批准记录中明确接受哪个服务版本；若与获批版本不符则停止，不偷偷换模型或把别名请求称为0813固定版本证明。代码仍请求批准的deepseek-v4-pro别名。此变化不阻塞本轮无外部调用的入口实现/审查/PR。

设计预审由全新上下文review_live_design执行：同意首条one-shot范围，要求独立live账本、审批引用唯一、全部读取计数、业务/outbox先提交、未知费用不释放、明确后续恢复与原三次重复尚待执行；这些条件纳入本方案。
