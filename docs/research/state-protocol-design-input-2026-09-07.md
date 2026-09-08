# 状态、调度与证据协议：技术方案输入

日期：2026-09-07。提案，未实施、未运行验证；不替代 SPEC 或验收清单。

## 依据与复用边界

- [OpenSRE 调度 claim](https://github.com/Tracer-Cloud/opensre/blob/cd7e1b9136ca5f9dd3614e586813d98ac9a2a543/infrastructure/scheduling/scheduler/storage/run_store.py#L51-L170)：task/fire_time 去重、过期接管、attempt/owner 条件完成值得移植其协议。不能直接移植 JSON 任务定义 + SQLite run + JSONL 会话三种存储为我们的统一事务。详见本目录 opensre-source-audit-2026-09-07.md；其中 WAL 吞错与旧 task 整对象覆盖是反例。
- [Holmes 工具所有权](https://github.com/HolmesGPT/holmesgpt/blob/5e983c17f30e93099c7d775167266d4cd1d586c4/holmes/core/conversations_worker/tool_call_worker.py#L247-L309)：旧 owner 结果拒绝可借鉴；[临时结果存储](https://github.com/HolmesGPT/holmesgpt/blob/5e983c17f30e93099c7d775167266d4cd1d586c4/holmes/core/tools_utils/filesystem_result_storage.py#L26-L44) 不适合作持久证据。
- [PostgreSQL SELECT](https://www.postgresql.org/docs/current/sql-select.html)：SKIP LOCKED 可供多消费者队列领取，但不是一般一致性读；网络调用不能占着事务锁执行。[INSERT ON CONFLICT](https://www.postgresql.org/docs/current/sql-insert.html) 支持唯一键冲突处理。
- [LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence)：复用 checkpointer，不推断它与业务表、blob 同事务，也不将存在 checkpoint 等同完整事故恢复。

## 推荐最小组成

单套 PostgreSQL 保存 incidents、runs、jobs、tool_operations、evidence 元数据、人类操作与持久事件；LangGraph saver 单独 schema。API 与 worker 进程分开但同仓库。首期不需要 Kafka、Redis 或 Temporal。PG jobs 协议是我们的少量定制逻辑，不声称来自上游现成完整库；SQL 驱动与 LangGraph PG saver 直接复用官方库。现有 worker 库若后续证明满足以下所有权及事务契约，可替换实现而不改变契约。

## 事件与调度

1. 以 `(source, external_event_id)` 去重同一次投递，唯一键；不要用消息正文代替事件身份。事故关联另行处理，不把相似告警必然合并。
2. 同一事务提交事件、Incident/Run 变化及待执行 job，提交后才响应接收成功。jobs 已是可持久派发表，不为同数据库派发再叠一份 outbox。
3. worker 短事务 `FOR UPDATE SKIP LOCKED` 领取到期 job，写 owner、递增 lease_epoch、lease_until，再出事务工作。续租与结果提交均验证 epoch、未过期及业务控制版本。
4. 定时观察持久化 due_at 与 schedule_generation；`(schedule_id, generation, due_at)` 唯一。进程重启扫描数据库到期任务，不依赖内存 timer。改时间/关闭时递增 generation，旧任务自然失效。
5. 需要向外部追踪平台异步出口时，才使用与业务事件同事务的 outbox。消费者重试至少一次；外部幂等能力决定是否去重，不能承诺 exactly-once。

## 人类状态与迟到结果

Incident 生命周期与单次 Run 状态分离。暂停自动调查、取消本 Run、关闭事故是不同操作。每次操作使用预期版本 CAS，递增 control_generation；取消、暂停/关闭时令旧执行权失效。关闭不删除历史；显式重开产生新 generation，并按需要建新 Run。

worker 不得把旧完整 Incident 对象写回。终态/结果更新必须验证预期 control_generation、lease_epoch 和当前状态。迟到结果不能恢复运行、发布结论或关闭事故；允许作为标注为未采纳的审计材料保存，但不自动进入当前模型上下文。底层网络查询未必可立刻杀死，权限失效阻止后续调用和业务接受，deadline 限制存活时间。

## 证据、领域表与 checkpoint 非原子提交

建议先限制单次查询大小；小证据可与元数据同 PostgreSQL 事务保存。大证据若启用 blob，采用以下协议，不建立分布式事务：

1. 查询前记录 stable operation_id（Run + 已持久模型 tool-call 标识），参数、目标和明确时间窗；同一步重放使用同 ID，新一轮相同查询获得新 ID，避免误用过期数据。
2. 查询结果先写不可变 blob，取得内容 hash 并确认可读；随后事务提交 evidence 元数据与 operation 完成记录，验证执行权。失败时不把未提交结果送入模型。可保留租约失效结果为未采纳审计，不更新当前 Run。
3. 工具包装层在业务事务成功后才返回 evidence_id 给 graph。重放 operation_id 时先读已提交结果；无需再请求目标系统。只有 blob 已写但业务事务失败时可能产生孤立对象，使用延迟 GC；GC 必须与写入租约/暂存登记协调，不能只凭当前没有引用就立即删除。
4. graph checkpointer 写入失败时，业务证据可能已存在：恢复重放读取 operation 完成记录。领域状态是权威，checkpoint 不能绕过暂停/取消/关闭。
5. checkpoint 还需防旧 worker：不能不加判断读取共享 thread 的 latest。候选方案为每个执行 epoch 独立 checkpoint namespace，完成持久 checkpoint 后用 epoch/control_generation CAS 提升业务表的 accepted_checkpoint_id。恢复只读被提升的具体 checkpoint；迟到保存留在旧 namespace，无法被提升。该集成需验证 saver 与 LangGraph 恢复 API 后才定案，不能声称现成保证。
6. 若 checkpoint 成功、指针提升前崩溃，恢复从旧 accepted checkpoint 重放；上述 operation 幂等保护已完成工具结果。LLM 调用也可能重复付费，因此记录调用和预算预留，恢复对未知用量保守计入，不能声称调用一次。

read-your-write 条件：模型只能获得已提交 evidence 的引用；证据读取用主库，不从可能滞后副本读取；blob 可读性失败时显式失败/重试，不发送悬空引用。完成报告在验证证据引用存在、执行权有效后才发布。

## 必测与代价

故障窗口至少覆盖：领取后 kill；工具成功但结果未保存；blob 成功但 DB 失败；DB 成功但 checkpoint 失败；checkpoint 成功但提升失败；lease 接管后旧 worker 归来；暂停/关闭与最终结果并发；timer 编辑后旧任务到期；备份恢复时缺 blob。预期是任务可重试、结果不静默丢失或错误采纳，不是外部查询恰好一次。

成本是少量协议代码、状态迁移、主库写入量与孤立对象清理。若初期 PG 内有界证据满足容量，可先避免 blob 分布式边界；不能在没有容量测量的情况下声称必须对象存储。保留期是运行配置与预算决策，不应在本输入任意定天数。
