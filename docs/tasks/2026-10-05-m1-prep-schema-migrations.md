# M1 准备：数据访问层标准化（Alembic、连接池、状态约束，保留参数化 SQL）

- 状态：进行中（PR-a 待审查；PR-b/c/d 待开始，见 issue #77–#79）
- 更新日期：2026-10-05
- 依据：[ADR-0007](../adr/0007-data-access-raw-sql-with-standard-tools.md)；[DurableStore 加固记录](2026-09-15-durable-store-hardening.md) B2/C1/C2；C3 §7「故障恢复与提交一致性」；F8 第 3 步（版本化 state-schema 升级与回滚）；[ADR-0003](../adr/0003-business-state-recovery-authority.md)；issue #76（PR-a）
- 工作区：`chore/m1-prep-alembic`，worktree `../production-ops-agent-alembic`（PR-a）

## 目标与范围

按 ADR-0007 用标准组件补齐数据访问层：schema 从「各模块 `install()` 里内联 DDL」改成 Alembic 版本化迁移（M1-02 的观察表以第一条增量迁移落地，并为 F8 第 3 步留出升级/回滚路径），加 `psycopg_pool` 连接池，状态列加 CHECK 约束，`persistence.py` 按业务对象拆分。查询继续用 psycopg 参数化 SQL。

范围内：基线迁移、现有库接管、owner 连接执行迁移与 `install()` 改为版本校验、CI 两条路径（空库、已有库）。
范围外：ORM 或 SQLAlchemy Core 改写查询（ADR-0007 已否决）、除 CHECK 约束外的表结构变更（基线必须与现状逐列一致）、`test_architecture.py` 的持久化/领域层 xfail。

## 已核查的现状

- DDL 分在四个 `install()`：`opspilot/persistence.py:212`（12 张表）、`opspilot/web/events.py:164`、`opspilot/web/evidence.py:161`、`opspilot/web/store.py:235`，共 15 张表；`ADD COLUMN IF NOT EXISTS` 共 13 条（`persistence.py` 12 条，`web/evidence.py:172` 1 条），基线必须逐条覆盖。
- `.install()` 调用点 72 处，运行时入口 `opspilot/worker_main.py`、`opspilot/web/__main__.py`，其余是测试和实验脚本。
- 依赖：`psycopg[binary]==3.3.3`；没有 SQLAlchemy/Alembic。
- 上游 HolmesGPT 没有自己的数据库和迁移工具（数据在 Robusta SaaS 的 Supabase），无可照搬做法。

## 计划（四个 PR，按序；PR-d 与 trace 接入改不同文件，可并行）

**PR-a：Alembic 基线与接管**（执行情况见下节）

1. 加依赖 `alembic` 与 `sqlalchemy`（psycopg3 方言 `postgresql+psycopg`），版本按开工当天官方发布核对并锁定；理由写一行进本记录。
2. 基线迁移 `0001_baseline`：用 `op.execute` 原样搬入现有 DDL（含四个模块），不改任何列；无 downgrade（写明原因）。
3. 已有库接管：迁移前比对 `pg_dump --schema-only` 与空库执行基线后的结果，一致才 `alembic stamp`，不一致拒绝并报差异。
4. 迁移由拥有 DDL 权限的 owner 连接在启动前执行（`make migrate` 或部署步骤）；运行时 `install()` 保留签名，改为只校验当前版本是否为 head，不是 head 则拒绝启动。测试与本地开发用 owner 连接时可直接 `upgrade head`。这样 M1-02 的 Observer 受限 PG 角色（D3）不需要 DDL 权限。调用点不动，并发迁移由 Alembic 版本表与 PG 锁保证只执行一次。
5. CI `m0-postgres` 作业加两条：空库升级到 head；用 main 旧 DDL 建库后接管再升级。
6. 文档：`docs/development.md` 写迁移命令和新增迁移的约定（每条迁移带 downgrade，除基线外）。

**PR-b：连接池**（#77）

7. `transaction()` 改用 `psycopg_pool.ConnectionPool`，保留每个事务的 `SET LOCAL` 超时；池大小与超时写成配置。证据：重跑 09-15 记录 B2 的对照（`rebuild()` x20 新建连接 vs 池化），并跑全部 PG 集成测试。

**PR-c：状态 CHECK 约束**（#78）

8. 一条 Alembic 迁移给各状态列加 CHECK，取值来自 `opspilot/domain` 的 `Literal`；加测试比对迁移里的取值与 `Literal` 一致；接管已有库前先查有无非法值，有则拒绝并报告，不自动改数据。

**PR-d：拆分 `persistence.py`**（#79）

9. 按业务对象拆成多个模块（如 incidents/runs、controls/suspensions、budgets、steps），纯搬移；`DurableStore` 公开接口与 import 路径保持兼容；不改任何 SQL 文本（用 diff 证明搬移前后 SQL 字符串集合一致）。

## PR-a 执行（2026-10-05）

- 依赖：`alembic==1.20.0`（PyPI 2026-09-11，要求 SQLAlchemy>=2.0）、`sqlalchemy==2.1.3`（PyPI 2026-10-03，`postgresql-psycopg` extra 要求 `psycopg>=3.0.7,!=3.1.15`，覆盖已锁的 3.3.3；来源 pypi.org JSON 元数据与 Alembic changelog），放 `m0` 组；`uv lock` 只新增 alembic、mako、sqlalchemy 三包。psycopg 3.3.3 下方言实际跑通见下方证据。
- 布局：`opspilot/migrations/`（`env.py` 从 `OPSPILOT_DSN` 或 `config.attributes["dsn"]` 取 libpq conninfo 转 `postgresql+psycopg` URL，迁移前取 `pg_advisory_lock` 串行化并发迁移）；`opspilot/schema.py`（`verify_head`/`migrate`/`schema_dump`/CLI）；`pyproject.toml` `[tool.alembic]`；`make migrate`。
- 基线 `0001_baseline`：四段 `op.execute`，DDL 由 AST 从 commit 3d6c94f 的四个 `install()` 提取、只去缩进；同一份文本冻结为 `tests/integration/legacy_schema_2026-10-05.sql`，`tests/test_schema_baseline.py` 断言两者逐行一致、15 张表、13 条 `ADD COLUMN IF NOT EXISTS`。无 downgrade：没有更早的 schema，删表会毁掉 ADR-0003 的恢复权威。
- 接管：只比 `opspilot_*` 对象（lab 库里还有 m0 实验表），`pg_dump --schema-only --no-owner --no-privileges --no-comments`，去掉注释、`\restrict`、`SET`/`set_config` 行；参考库是同服务器 `CREATE DATABASE ... TEMPLATE template0` 升到 head 后再 `DROP ... WITH (FORCE)`，需要 owner 角色有 CREATEDB。
- 测试适配（需审查者确认）：`test_m1_durable_state_postgres.py::test_install_indexes_the_incident_foreign_keys_on_an_existing_database` 原断言「删掉索引后再 `install()` 会补建」，与第 4 步「`install()` 只校验版本」直接冲突；改为「一次性 schema 升到 head 后两索引存在、运行时 `install()` 接受，未迁移时拒绝」。缺索引的旧库由接管比对拒绝（`test_drifted_database_is_refused_and_left_unversioned`）。其余 71 处 `install()` 调用未改。
- 本机验证（PostgreSQL 17.9 Homebrew，临时实例于 scratchpad、端口 55431、lab 数据目录 `tmp/m0-b/postgres` 未动）：
  - 空库：`make migrate` → `schema upgraded: 0001_baseline`；`check` → `schema at head`；再 `migrate` → `unchanged`。
  - 旧库：`psql -f legacy_schema_2026-10-05.sql` 后 `migrate` → `schema stamped: 0001_baseline`；`schema_dump` 空库 head 与接管库比对 `identical, 209 lines`。
  - 漂移库（多一列 `stray`）：`migrate` 退出 1 并打印 diff（`+    stray text`），版本表未建，临时参考库已删。
  - `M1_DURABLE_POSTGRES=1 M0_B_POSTGRES=1 pytest tests/integration`：首轮 254 passed、38 skipped、1 failed（即上面适配前的索引测试）；适配后清空 lab 库复跑（含 `M0_STEP_POSTGRES=1`）：283 passed、10 skipped（仅重启 lab 的用例）、0 failed。`M0_STEP_POSTGRES=1 test_m0_step_store_postgres.py` 28 passed。`make check`：2520 passed、293 skipped、2 xfailed。
- 未执行：CI 两条路径只在 PR 上跑（ubuntu runner 需装 `postgresql-client-17`，步骤已写进 `ci.yml`）；真实生产库接管未做（没有生产库）。

## 前提与完成条件

- 前提：SPEC 门槛段落合并；开工前先核实 Alembic 与 `psycopg_pool` 当前版本对 psycopg 3.3 的支持（官方文档），结论写进本记录（Alembic 已核，`psycopg_pool` 留给 PR-b）。新依赖放在 psycopg 所在的依赖组；产品与 M0 依赖分组（09-15 记录 B1）不在本任务。
- 完成条件：
  - 每个 PR：现有测试全过；PR-a 的两条 CI 路径通过；PR-b 有池化前后对照数据；PR-c 有非法值写入被拒的 PG 测试；PR-d 有 SQL 字符串集合一致的证据。
  - 空库 head 与旧库接管后 schema dump 完全一致（证据入本记录）。
  - 独立审查（全新上下文）通过，发现已处置。
- 合并类别：四个 PR 都触及持久化与状态恢复，属用户门，由用户合并。

## 下一步与交接

- PR-a：独立审查 → `@codex review` 分诊 → 用户合并。合并后本机 lab 库首次跑集成测试会走接管（需 PG 17 `pg_dump` 在 PATH 或 `OPSPILOT_PG_DUMP`）。
- 开工顺序：PR-b（#77）→ PR-c（#78）→ PR-d（#79）→ [trace 接入](2026-10-05-m1-prep-trace-langsmith.md) → M1-02 计划第 0 步。
