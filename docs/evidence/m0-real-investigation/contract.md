# 本轮真实调查验证合同

开始：2026-09-09T17:14:30Z；绝对截止：2026-09-10T17:14:30Z。
用户本轮明确授权新的有界模型调用、必要复验、白名单 trace 与专属本地环境/可逆故障；取消旧轮不自动重跑限制。新增累计上限20 CNY、20次模型请求、5次trace上传；旧Pro与首次Flash各2 CNY未核账原地保留，不复用批准或重置账本。不购买云/订阅、不扩大数据用途、不自动合并。

依据 SPEC实施门槛/数据出口、C3 §5/7/8/11–13、M0 §1–7；关联F1/F2/F3/F7/F8/F12/F14，只形成设计验证证据，不改passes。

## 工作与限额

- Flash诊断与必要修复复验：先分配最多3个独立normal-1实验，每个2 CNY预留、2次模型、最多1次trace。先运行当前请求并增加仅本地安全正文结构诊断；根据真实失败决定改动，无新证据不重复。固定 read_fixture / target=m0-target-a / evidence_id=m0-evidence-a；最终严格JSON判据不变。每例420秒、模型180秒/请求、其余HTTP10秒，16KiB请求/128KiB响应。默认显式deepseek-v4-flash / thinking enabled / high，权重仅reported_alias。
- OTel Demo/HolmesGPT环境：独立worktree与专属实例，由环境执行者唯一负责。固定既有来源版本，先正常日志/指标/trace/身份/只读实际权限，再故障事实及调查。调查调用后续按此总额分配子限额，分配前不得调用；总剩余额度不超14 CNY/14次模型/2次trace。
- 主Agent是累计账本唯一写入者；分配额度先持久记录，未知费用全额占用不退款。每次调用在已有独立实验身份/尝试账本登记；新环境子实验采用独立身份与有界请求计数，不以20轮推理循环代替20次HTTP请求限制。
- 超预算、次数、期限或证据不足即停止相应付费执行，继续本地独立工作。每次保留失败、次数/usage/耗时及可归属费用未知项。

## 数据与保全

凭据仅从主仓库.env读入可信客户端认证，不复制、不打印。LangSmith沿用US/default workspace/default项目，不新建，出口仅既有DTO；provider reasoning仅同Run内存续传，不保存/导出。诊断仅观察最终content（非reasoning_content）的类型、长度、JSON结构、围栏特征、精确目标/证据比较；未知文本不进入公开工件，必要本地正文只在0600私有文件保留且不导出。

原M0-01 worktree从干净6c4de3e快进main738b5c7，新分支chore/m0-real-investigation；旧数据库/批准/证据不搬不清。专属PG位于tmp/m0-b/postgres:55431，系统PG另行存在不操作。环境分支chore/m0-real-environment从同基线新建。收尾仅停止本轮服务，保留全部数据库/证据。

## 判据与版本/费用

Flash必须完成有效工具调用、配对续接、精确最终JSON、PG业务/outbox/诊断同事务提交、LangSmith白名单上传及回读TRACE_VERIFIED。原失败不追改；新成功不证明旧具体原因或完整M0。
正常与故障至少各一实际调查，独立核查可见证据与故障事实，记录工具查询、来源、时间窗/身份/版本、结论、缺口。只据这些证据冻结首个提交调查→查询→展示→人工跟进/取消→持久保存纵向流程所需验收、权限、预算/超时与恢复前提；缺项明确保留，不能以工具安装/mock替代。

2026-09-09现场官方价格：Flash峰值cache-hit0.1/cache-miss3/输出9 CNY每百万tokens，空闲减半。normal-1按32768输入/4096输出每请求的保守假设，两次上界0.270336 CNY，另预留trace0.10，2 CNY子额度足够覆盖估算不确定性但不宣称供应商硬账单证明。LangSmith Developer当前官方含5000 base traces/月；实际账户/用量执行前只读核对，不开自动化/评测/订阅。来源：https://api-docs.deepseek.com/zh-cn/quick_start/pricing/ 、https://www.langchain.com/pricing 。实际账单与估算分别报告。
