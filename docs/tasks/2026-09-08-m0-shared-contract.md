# M0 最小共享接口 v1

本文件是本批实验接口记录，依据 C3 §5/7/12/13 和 M0 §1/2/3/6；不冻结产品实现或校准阈值。Python 类型由协调者维护于 `scripts/m0/contracts.py`，A/B 依赖类型而非对方实现。

## 身份、期限与权限

`RunContext(experiment_id, run_id, provider, deadline)`：experiment_id/run_id 为 UUID，provider 为受控 provider 标识，deadline 为带时区 UTC 绝对时间。每个新 Run 独立 UUID；同一 Run 重试保留身份、原期限与累计费用，provider 切换创建新 Run。provider 私有状态额外绑定 provider + run_id，不可跨 Run 续传或出口。身份和预算不是联网授权。SDK 隐式重试固定为 0；任何后续物理重试必须新建 request_id 并重新预留。
`RequestIdentity(run, request_id)`：request_id 为 UUID，标识一次可能计费的物理请求；重试新建 request_id，同一次数据库调用重试保持 request_id。不将稳定逻辑轮次当物理调用身份，避免重试漏计费。deadline 不可在重启或重试延长；请求 timeout 取配置上限与剩余 deadline 的较小值，<=0 不发起请求。reserve 返回后、实际发送前再次核对绝对 deadline；预留或锁等待不得使旧的 remaining 值继续有效。确定未发送可 settle(0)，发送/提交不明保留未知占用；数据库侧在取得锁后用 clock_timestamp() 当前时钟核对期限，不使用等待前的事务起始 now()；增加锁等待跨期限用例。

## 实验账本接口

金额使用非负整数微元 CNY（1 元 = 1,000,000 单位），拒绝 bool/float/负数。合成额度显式标为 synthetic；未来真实费用必须使用单一权威实验账本，多个数据库实例不能各自代表同一授权额度。价格换算、真实计费上限及授权接入留到汇合点。

- `reserve(request: RequestIdentity, upper_bound: int) -> Reservation`：upper_bound > 0；数据库原子校验实验总额、实验/Run 原 deadline 和身份，先持久提交再允许替身传输。`Reservation` 含 request、reserved、state（reserved/unknown/settled）、actual（未结算为 None）、created: bool（仅此次事务新建预留且提交成功返回 True；重放/查询/结算返回 False）。可用额度 = limit - settled 实际费用 - reserved/unknown 占用。多个 Run 共用实验额度。
- 相同 request_id 与完全相同输入的重复 reserve 返回既有记录，不新增额度也不构成再次发送许可；调用者必须区分 created=True 与 replayed(created=False)，仅 created=True 可发送一次。并发相同 request_id 最多一个调用返回 created=True；不确定提交后的重查只返回 replay，禁止发送。身份/金额/deadline 冲突返回 `IDENTITY_CONFLICT`。已过期时拒绝任何新请求；历史记录仍可查询和结算。
- `settle(request: RequestIdentity, actual: int) -> Reservation`：仅接受已有请求；准确费用已知才结算，释放 reserved-actual。actual 超过预留时仍保守记入全部实际费用并将实验阻断，不能截断实际费用或释放额度继续调用。相同结算幂等；不同 actual 为 `SETTLEMENT_CONFLICT`，不改既有记录。
- `retain_unknown(request: RequestIdentity) -> Reservation`：断流、发送结果/费用不明或超时后，完整保留预留；重复调用幂等。unknown 可被后续可靠用量 settle；对 settled 请求 retain_unknown 不回退其状态。不得因超时、进程重启、过期或重新初始化清零/自动退还。
- 初始化实验将 UUID、额度、deadline、synthetic 标识固定；重复初始化只接受完全相同参数。注册 Run 同样固定 provider/期限，Run deadline 不得超过实验 deadline。未知实验/Run/request、无效输入、过期、额度不足、存储错误均为固定错误码，拒绝新调用且不输出连接字符串或原始异常。

B 提供上述持久机制及 snapshot（limit、settled、reserved、unknown、blocked）；A 仅消费协议并用记录调用的简单替身测试预留先于传输、结算/未知保留、失败不发送，不另建余额算法。实际 A+B 汇合测试由协调者负责；联网适配和授权系统不在本批实现。储存连接失败等不确定提交按相同请求身份重查/重试，不在不确定时发送；完整 F2 恢复/调度不在范围。

## 失败与可观察结果

错误码至少覆盖 INVALID_INPUT / IDENTITY_CONFLICT / UNKNOWN_IDENTITY / DEADLINE_EXCEEDED / BUDGET_EXHAUSTED / STORAGE_UNAVAILABLE / SETTLEMENT_CONFLICT。适配层另行记录协议、工具、trace 的固定分类，原始异常不进入公开输出。trace 失败独立于模型/工具结果，回读校验归属和白名单；替身回读不代表平台已存储。C 的 outcome 保留执行状态与结论确定性两个维度，调查输入不含 evaluator 专用事实；本批不实现调查模型或最终评分阈值。
