# B 独立审查与验证

日期：2026-09-08。审查者为未参与实现、全新上下文的独立 Agent。受检 worktree `production-ops-agent-m0-b`，分支 `chore/m0-b-budget`，HEAD `dfec6b63610fe58b17a365797c003a347a2752f3`；相对 `chore/m0-batch-baseline` 的 B 新增。开始时 Git 工作区干净。本文件是审查记录，不修改实现。

## 依据与范围

完整读取 AGENTS、SPEC、ROADMAP；读取 C3 §7/12/13、M0 执行计划、B 任务、共享接口，以及 B 实验合同、原始测试输出、结果和源码哈希。检查 budget.py、budget.sql、postgres_lab.py 与全部 B 测试。归档的 7 个源码/锁文件 SHA-256 全部与受检工作区一致。

核对原子预留/结算/未知占用、跨 Run 共享实验额度、跨进程唯一 created、身份固定、期限、事务失败与服务归属。没有读取、复制或打印真实 .env；模型、trace、云和生产调用为 0。

## 实际运行

- 启动前确认 55431 无监听，既有 PID4391 为 PostgreSQL；使用 `.venv/bin/python scripts/m0/postgres_lab.py start` 启动专属 cluster。`verify_server()` 实际校验服务数据目录与 PID 文件。
- `M0_B_POSTGRES=1 M0_B_RESTART=1 .venv/bin/python -m pytest tests/test_m0_budget.py tests/integration/test_m0_budget_postgres.py -q`：**18 passed in 2.15s**，包含实际 PostgreSQL 停启、4 个 spawn 子进程竞争、跨 Run 限额、同请求唯一 created、锁等待越过期限、未知占用、结算超额阻断、身份冲突、提交确认丢失及超过 28 位整数金额。
- `make check`：离线锁校验、Ruff 检查/格式通过；**55 passed, 10 skipped in 0.98s**。10 项数据库集成在上一命令已实际执行，默认开发检查跳过仍属预期。
- 独立补充反例通过：真实数据库上 6 个线程执行 24 次 settle(30)/settle(40)/retain_unknown 竞争；仅首个已提交实际费用保留，另一费用返回 SETTLEMENT_CONFLICT，unknown 不回退 settled，最终 reserved/unknown 均为 0。
- 独立补充反例通过：在真实事务提交前注入 psycopg.OperationalError；返回 STORAGE_UNAVAILABLE 且无原始异常 context，数据库 snapshot 完全不变；同 request_id 重试获得首次持久化预留 created=True。与现有提交后丢失确认用例互补，后者重试仅 replay。
- 独立补充反例通过：已注册实验 deadline 向前/向后各改 1 秒均返回 IDENTITY_CONFLICT；synthetic=False 返回 INVALID_INPUT。
- 实测 PostgreSQL `17.9 (Homebrew)`。结束运行 `.venv/bin/python scripts/m0/postgres_lab.py stop` 成功；55431 无监听，既有 PID4391 仍存活。专属数据库和日志保留，未操作其他 daemon。

## 结论与边界

本次受检 B 增量未发现需修复的正确性或合同违反问题，独立审查和上述本地机制验证通过。实验行锁串行化共享额度变更；created=True 在提交成功后返回；重放不形成第二次发送许可；未知与超额保守计量符合共享合同。服务脚本的固定路径、owner marker、symlink 拒绝、服务端目录/PID 核对与本轮启停证据支持该专属环境的归属判断。

不构成真实计费授权、身份/网络权限隔离、真实模型或 LangSmith 组合证据、完整 F2 恢复/调度、峰值资源或硬限额、soak、产品验收或 SPEC 实施门槛开放。实际 A+B 汇合与 PR 最新 CI 仍由协调者完成；用户合并授权保持独立。
