# M0-02 报告收束候选方案

日期：2026-09-10。状态：待独立方案审查，尚未实现或真实执行；不打开 SPEC 产品实施门槛。工作区 `production-ops-agent-m0-environment`，分支 `chore/m0-02-environment`。实验 `m0-02-20260910-convergence`，2026-09-10T01:49:00Z 至 2026-09-11T01:49:00Z。父执行者独占环境启停、注入、付费请求与 trace 上传；本工作项仅维护 harness 与离线检查。

依据：[SPEC](../../../SPEC.md)、[首片入口计划](../../plans/first-vertical-investigation-2026-09-09.md)、C3 数据流合同。保持固定 Holmes `5e983c17f30e93099c7d775167266d4cd1d586c4` 原调查循环、Flash thinking enabled/high。不提供注入答案、不执行修复、不以模型自评认证恢复。

## 已核查原因与证据

- `holmes/core/llm.py:518–526` 的 `OVERRIDE_MAX_CONTENT_SIZE` 是 tokens；现 wrapper 同时以 131072 限制 HTTP bytes，单位不同。`fault-02/request-envelope-checks.json` 第四包 158029 bytes，前三包成功，第四包被本地信封限制拦截。
- `fault-handoff-01/result-business.json` 为 length 空正文；旧账本记录 26618 prompt / 8191 completion tokens，约 66.07 秒。证明8192输出不足；不是请求超时证据。无需再次付费重现此失败。
- 上游 `tool_calling_llm.py:1161` 已在最后一步设置 tools=None，4步包含报告槽；`core/truncation/input_context_window_limiter.py:96–110` 可能触发额外模型压缩调用。本轮显式禁用压缩，超限 fail closed，不删减同 Run 私有协议。
- fault-02 e5/e11 trace raw 各约338KB，实际 view 各8008bytes；e10 metrics view 25629bytes。保留完整 raw 与投影，不能把本地 raw 当作模型已看见。
- 官方当前 [价格](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/) 为 Flash 1M context、最大384K输出，峰值输入缓存未命中3 CNY/M、输出9 CNY/M；[thinking协议](https://api-docs.deepseek.com/guides/thinking_mode/) 要求有tools时完整回传历史 reasoning_content。这里只引用协议要求，不读取或展示真实推理正文。

## 统一候选参数与账本

单一配置对象派生 env、LLM args、HTTP guard、工件与账本，不继续散落抬常数。

| 项目 | 冻结候选 |
| --- | --- |
| 模型 | deepseek-v4-flash，thinking enabled/high，无降级或自动重试 |
| Holmes context | 131072 tokens，明确与 HTTP bytes 分开 |
| 最大输出 | 32768 tokens；8192失败后4倍空间，仍待运行验证，不声称保证收束 |
| 请求 bytes | 512KiB；完整同Run协议保留，超限拒绝而非截断 |
| 响应 bytes | 2MiB；首次真实调用前合成最大输出编码检查，不成立则回方案审查 |
| 模型请求 | 360秒硬wall-time；旧样本约124 output tokens/s，32k约264秒加余量，非已测SLA |
| Run | 1800秒硬wall-time，受绝对deadline再次限制 |
| 正常/故障 | 默认各4实际HTTP，20工具查询，原Holmes最后一步报告 |
| 工具 | 每工具30秒硬wall-time，每Run工具阶段累计240秒；客户端IO超时不等于硬清理 |
| 总合同 | 20CNY、20实际HTTP、5 trace上传、绝对24小时；旧轮24CNY不释放不复用 |

每请求保守预留 `(1048576*3 + 32768*9)/1000000 = 3.44064 CNY`。使用供应商完整输入上界，避免将未知 tokenizer 或 HTTP bytes 估计误认为硬费用上界；1Mi取值避免1M口径歧义。本地token估计仅做容量诊断。完整可信usage到达且非负、总数/缓存字段自洽、未超过预留假设后，以全输入按峰值未命中计费作为已知费用上界，释放预留差额；未知、异常usage和中断继续保留全额。超过假设则记异常并禁止继续，不能低报费用。此数不是供应商账单。

发送前原子持久预留，判断 `已知上界 + unknown全额预留 + 新请求预留 <= 总额与所属phase额度`，所有HTTP尝试均计数；预留后中断不释放。父执行者才能在总额不变条件下调配phase，wrapper不得自行借款。

| Phase | CNY | HTTP/上传上限 | 默认执行 |
| --- | --- | --- | --- |
| saved report | 4 | 2 HTTP | 1请求、0新工具 |
| normal | 5 | 5 HTTP | 4请求以内 |
| fault | 5 | 5 HTTP | 4请求以内 |
| PG | 4 | 4 HTTP | 父执行者管理，默认2 |
| contingency | 1.5 | 4 HTTP | 未分配，须父转拨后才可覆盖一次完整预留 |
| trace | 0.5 | 5上传 | 仅允许业务投影 |

合计20CNY/20HTTP/5上传。较多请求上限只为有依据复验留余地，不要求花满。normal/fault的5CNY是总消耗与未知预留守门；每请求结算后才可能继续下一次，若剩余不足3.44064则停待父调配。

## 最小实现边界

1. wrapper读取新合同和独立allocation账本；旧工件与账本不改。上游源码不编辑，关闭compaction并确定性验证没有隐式额外调用；完整assistant协议留同provider/Run，不删除private字段以适配bytes。
2. 工具使用可终止子进程完成限定只读HTTP，单个30秒到期终止并join确认；Run工具累计阶段240秒到期不再调度，未退出子进程必须清理。保留明确timeout/denied/error及已取得raw。不得把ThreadPoolExecutor的退出等待误当硬停止。
3. raw与view分别不可变保存，登记raw hash、view hash、投影版本、保留字段/片段、采样/截断计数。动态交付日志在guarded_send从真正发出请求的业务messages构建，记录本请求序号/ID、evidence ID与实际view hash；只登记确实进入请求的业务材料，不复制reasoning。预发送准备与实际HTTP尝试状态分开，不能把被本地拒绝的包算已交付。
4. 私有字段仅同provider/Run续传；报告、日志和trace用允许字段导出。完整raw业务证据保留；完整provider响应若为恢复而持久化，则独立私有工件权限600、Run/provider绑定，禁止交评审或trace。不得序列化全部Holmes event。
5. 错误分类保留原边界拒绝原因，避免SDK包装成InternalServerError后丢失本地拒绝证据。结果只在stop且非空正文时为returned，质量始终交独立业务证据评审。

## 执行与完成条件

按 diagnosing-bugs skill，历史真实失败已为模型输出红证据；离线测试先构造158029bytes信封、同Run私有字段、输出length空正文、预算中断/未知usage、工具挂起/子进程清理的最小红反馈，再实施并转绿。不为再次看到length而付费重跑旧配置。

先过确定性离线检查和独立方案/实现审查。父执行者再运行saved report新Run：仅导入旧业务views及来源hash，不导入旧provider私有字段，不查询新证据；stop非空、引用可回读且独立评审确认有据或明确unknown/handoff后，才进入真实正常与故障主动循环。相同配置执行，原始问题不含注入答案。独立评审只读实际可见证据与最终业务报告，保留缺源/采样限制。

报告失败、deadline、预算不足或证据合同失配即保全并停依赖阶段，不逐次抬常数。新增ModelStep/ToolOperation恢复只按父任务范围和单独验收实施。完成报告区分离线合同、真实调查和产品入口，不能将本候选或一次运行自动转为passes=true。

## 实现与第二次 saved 后的更新（历史候选参数以上保留）

第二saved实际HTTP响应 reported model 为 deepseek-flash，15104 completion tokens、stop且有正文；原failed保留，不回改为passed。独立名称兼容审查批准本轮请求仍exact deepseek-v4-flash，响应仅exact {deepseek-v4-flash,deepseek-flash}；不是权重/版本相同的证明。profile同时绑定受认证GET/models原响应hash `0c5d2ba6ebb791e893b0e7efed32c64415a633ed774e8d89dad8e33c4d69ae77` 与安全metadata工件hash `37a7903d6cc9ade127bd5d688953237d0c419de581a17351089bf62ee0eb900b`。首请求3.44064 unknown仍保留；第二请求独立复核费用上界0.243099由父附会计审计调整，原失败不改、旧24不释放。

父当前阶段分配为report4CNY/2HTTP、normal6/7、fault6/7、PG3.5/4、contingency0/0、trace0.5/5且上传仍禁用。第二saved报告存在统计/引用/因果质量问题，不能标质量passed；父决定不为形式gate重复saved请求，转为真实normal/fault主动调查验证新generic约束。保持32768/512KiB/360秒等运行参数不变。

离线实现候选已完成31项定向测试与Ruff；真实Holmes专用venv active import/schema preflight `m002-active-preflight-impl01` 通过（历史offline scope，0HTTP，非真实环境运行）。实际schema只开放services无参数、metrics query、logs/traces service，时间窗由独立scope注入；actual_sources在返回处校验，越scope只交denied view而保留raw。原始与view均有canonical与精确file bytes hash，trace v2保留parent references与精确身份映射依据；无匹配记unknown。

通用报告约束补充：无可比baseline不判趋势；累计histogram buckets不能相加计次数；spans不等于独立请求；逐条完整evidence_id；只基于可见parent refs讨论调用边，采样缺失不称完整链路。旧12views保持不变，不注入故障答案。

模型HTTP进程保留受限响应prefix，byte超限/中途读取失败标incomplete并固定拒绝码，业务投影不含prefix。200异名/JSON异常/error envelope、非200响应先保留受限raw再拒绝；usage异常保留全额、持久blocked与固定诊断，任何phase不能继续。私有工件仅同provider/Run，不进入报告、trace或评审。

当前active候选送独立验证，尚未执行真实正常/故障，不能据本节声称入口通过。
