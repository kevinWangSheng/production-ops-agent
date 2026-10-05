# ADR-0007：数据访问保留参数化 SQL，用标准工具补齐迁移、连接池与约束

状态：**已决定**（用户 2026-10-05）。

## 背景

- 持久化 SQL 分在 `opspilot/persistence.py`（1952 行）与 `opspilot/web/{store,events,evidence}.py`，约 158 处 `execute`，全部参数化（`%s`）；唯一的 f-string 拼接在 `persistence.py:990`，拼的是内部固定表 `_SETTLEMENTS` 里的列名，不来自外部输入。
- 约 78 行用到 PG 专有能力：`FOR UPDATE`、`ON CONFLICT`、`RETURNING`、`clock_timestamp()`、`jsonb`；PR #22 的「先锁 incident 再锁 run」修复依赖加锁顺序显式可见。
- [DurableStore 加固记录](../tasks/2026-09-15-durable-store-hardening.md)留下三项待决：B2 无连接池（每次新建连接约慢 14 倍）、C1 schema 演进无机制（当时建议版本表加编号 SQL，不引 Alembic）、C2 状态列无 CHECK 约束。
- 上游 HolmesGPT 没有自己的数据库，无可照搬做法。用户原则：有成熟框架先用，避免造轮子。

## 决定

1. **不用 ORM（SQLAlchemy ORM/SQLModel），也不改写成 SQLAlchemy Core。** 查询继续用 psycopg 参数化 SQL。理由：ORM 的 unit-of-work 让 flush 时机和加锁顺序变成隐式，与 C3 §7 提交一致性及 PR #22 的修复冲突；改写 Core 要重写全部查询、触及恢复路径，收益主要是风格。
2. **迁移用 Alembic**（C1）。推翻 09-15 记录「自建版本表」的建议：自建属造轮子。Alembic 只用于迁移，迁移内容用 `op.execute` 写 SQL，SQLAlchemy 只作为 Alembic 的依赖进入。
3. **连接池用 `psycopg_pool`**（B2），psycopg 项目官方组件。
4. **状态列加 CHECK 约束**（C2），经 Alembic 迁移落地，取值与 `opspilot/domain` 的 `Literal` 一致并有测试比对。`tests/test_architecture.py` 中「持久化层是否建立在领域状态机之上」的 xfail 是另一项架构决定，本 ADR 不处理。
5. **`persistence.py` 按业务对象拆分**为多个模块，纯搬移、不改行为。

## 后果

- 正面：迁移、连接池、约束都用标准组件；锁与事务仍在 SQL 里一眼可见；不碰已验证的恢复逻辑。
- 负面：SQL 仍是字符串，类型检查覆盖不到列名；新增 Alembic、SQLAlchemy、psycopg_pool 依赖。
- 反转条件：若后续出现大量与锁无关的 CRUD（如复盘、知识管理页面），可在那些模块单独评估 SQLAlchemy Core。
