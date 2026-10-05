# M1 准备：schema 迁移改用 Alembic

- 状态：待开始（用户 2026-10-05 决定基础设施先行，门槛见 SPEC 同日段落）
- 更新日期：2026-10-05
- 依据：C3 §7「故障恢复与提交一致性」；F8 第 3 步（版本化 state-schema 升级与回滚）；[ADR-0003](../adr/0003-business-state-recovery-authority.md)
- 工作区：开工时新建 `chore/m1-prep-alembic` worktree

## 目标与范围

把 schema 从「各模块 `install()` 里内联 DDL」改成 Alembic 版本化迁移，让 M1-02 的观察表以第一条增量迁移落地，并为 F8 第 3 步留出升级/回滚路径。

范围内：基线迁移、现有库接管、owner 连接执行迁移与 `install()` 改为版本校验、CI 两条路径（空库、已有库）。
范围外：表结构变更（基线必须与现状逐列一致）、拆分 `persistence.py`、ORM 化业务查询（查询仍用 psycopg 原生 SQL）。

## 已核查的现状

- DDL 分在四个 `install()`：`opspilot/persistence.py:212`（12 张表）、`opspilot/web/events.py:164`、`opspilot/web/evidence.py:161`、`opspilot/web/store.py:235`，共 15 张表；`ADD COLUMN IF NOT EXISTS` 共 13 条（`persistence.py` 12 条，`web/evidence.py:172` 1 条），基线必须逐条覆盖。
- `.install()` 调用点 72 处，运行时入口 `opspilot/worker_main.py`、`opspilot/web/__main__.py`，其余是测试和实验脚本。
- 依赖：`psycopg[binary]==3.3.3`；没有 SQLAlchemy/Alembic。
- 上游 HolmesGPT 没有自己的数据库和迁移工具（数据在 Robusta SaaS 的 Supabase），无可照搬做法。

## 计划（一个 PR）

1. 加依赖 `alembic` 与 `sqlalchemy`（psycopg3 方言 `postgresql+psycopg`），版本按开工当天官方发布核对并锁定；理由写一行进本记录。
2. 基线迁移 `0001_baseline`：用 `op.execute` 原样搬入现有 DDL（含四个模块），不改任何列；无 downgrade（写明原因）。
3. 已有库接管：迁移前比对 `pg_dump --schema-only` 与空库执行基线后的结果，一致才 `alembic stamp`，不一致拒绝并报差异。
4. 迁移由拥有 DDL 权限的 owner 连接在启动前执行（`make migrate` 或部署步骤）；运行时 `install()` 保留签名，改为只校验当前版本是否为 head，不是 head 则拒绝启动。测试与本地开发用 owner 连接时可直接 `upgrade head`。这样 M1-02 的 Observer 受限 PG 角色（D3）不需要 DDL 权限。调用点不动，并发迁移由 Alembic 版本表与 PG 锁保证只执行一次。
5. CI `m0-postgres` 作业加两条：空库升级到 head；用 main 旧 DDL 建库后接管再升级。
6. 文档：`docs/development.md` 写迁移命令和新增迁移的约定（每条迁移带 downgrade，除基线外）。

## 前提与完成条件

- 前提：SPEC 门槛段落合并；开工前先核实 Alembic 当前版本对 psycopg 3.3 的支持（官方文档），结论写进本记录。
- 完成条件：
  - 两条 CI 路径通过；现有测试全过。
  - 空库 head 与旧库接管后 schema dump 完全一致（证据入本记录）。
  - 独立审查（全新上下文）通过，发现已处置。
- 合并类别：触及持久化与状态恢复，属用户门，由用户合并。

## 下一步与交接

- 开工顺序：本项 → [trace 接入](2026-10-05-m1-prep-trace-langsmith.md) → M1-02 计划第 0 步。
