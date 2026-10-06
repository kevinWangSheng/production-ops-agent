# M1 准备：数据访问层标准化（Alembic、连接池、状态约束，保留参数化 SQL）

- 状态：进行中（PR-a 已合并 #104，本机 lab 库已接管；PR-b 连接池已提 PR 待用户门，见 issue #77；PR-c/d 见 #78、#79）
- 更新日期：2026-10-05
- 依据：[ADR-0007](../adr/0007-data-access-raw-sql-with-standard-tools.md)；[DurableStore 加固记录](2026-09-15-durable-store-hardening.md) B2/C1/C2；C3 §7「故障恢复与提交一致性」；F8 第 3 步（版本化 state-schema 升级与回滚）；[ADR-0003](../adr/0003-business-state-recovery-authority.md)；issue #76（PR-a）
- 工作区：PR-a 的 `chore/m1-prep-alembic` worktree 已在合并后删除

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
- 真实旧库接管实验（2026-10-05，lab 数据目录 `tmp/m0-b/postgres` 整体复制到 scratchpad、在 55433 端口启动副本，源目录只读未动，实验后副本已删）：
  - 副本现状：`m0_budget` 只有 5 张 `opspilot_*` 表（incidents/runs/steps/controls/budget_reservations，共约 7 万行），缺 10 张表与后加的列——它停在 09-16 的版本，旧版 `install()` 之后再没对它跑过。直接 `migrate` → 拒绝，diff 144 行，全是 `-`（缺表缺列）。
  - 对副本执行旧版 DDL（`psql -f legacy_schema_2026-10-05.sql`，等价于旧版 web/worker 再启动一次）后再 `migrate` → 仍拒绝，diff 只剩两处**列顺序**：`opspilot_incidents.lifecycle`（历史 `ADD COLUMN` 追加在 `created_at` 后，新建表里在 `state` 后）、`opspilot_steps.sequence`（追加在 `control_generation` 后，新建表里在 `run_id` 后）；类型、默认值、NOT NULL 均相同。`opspilot_suspension_audit.actor DEFAULT 'unknown'` 的差异没有出现（该表在副本里本就不存在，被整表新建）。
  - 结论：经历过历史 `ADD COLUMN` 的真实库，用逐字比对不能接管。**用户决定（2026-10-05，方案 C）**：默认比对保持逐字；`migrate --accept-column-order`（`make migrate MIGRATE_FLAGS=--accept-column-order`）只对每个 `CREATE TABLE` 块内的列行排序后比对，其余全部逐字；只有列顺序之差时 stamp，并打印与日志记录被接受的 diff；不带 flag 时列顺序之差仍拒绝并提示 flag；任何其他差异带不带 flag 都拒绝。`tests/test_sql_column_order_independence.py` 静态把守产品 SQL 不依赖列顺序（INSERT 必列列名、无按位置读行；8 处 `SELECT *` 全在 `persistence.py` 唯一的 `dict_row` 连接后按名读取，查询 SQL 未改）。
  - 方案 C 复验（同样复制 lab 数据目录到 55433、源目录只读、实验后删副本）：旧版 DDL 补齐后，`migrate` 不带 flag → 拒绝并提示 `--accept-column-order`；带 flag → `schema stamped: 0001_baseline`，被接受的 diff 正是上述两处列顺序（`-/+ lifecycle text DEFAULT 'open'::text NOT NULL`、`-/+ sequence integer DEFAULT 0 NOT NULL`）；`check` → `schema at head`；`opspilot_incidents` 仍 10,609 行。PG 测试 `test_grown_database_needs_the_column_order_flag`、`test_semantic_difference_is_refused_even_with_the_flag` 复现这两例与 `actor DEFAULT 'unknown'` 的语义差异。
- 已知边界（设计使然，留给 PR-c / F8）：`verify_head` 只看版本表；已 stamp 的库之后丢了索引或列，`install()` 仍放行，由迁移和 CHECK/F8 的版本化校验补。
- 集成测试的 `OPSPILOT_LAB_DSN` 覆盖（`scripts/m0/postgres_lab.py` 读环境变量，跨进程测试的子进程同样生效）是为了在另一端口的临时实例上跑全套测试、不碰 55431 lab；库名仍须是 `m0_budget`。
- 未执行：CI 两条路径只在 PR 上跑（ubuntu runner 需装 `postgresql-client-17`，步骤已写进 `ci.yml`）；真实生产库接管未做（没有生产库）。

## PR-b 执行（2026-10-05）

- 依赖：`psycopg-pool==3.3.3`（PyPI 2026-09-22，仅依赖 `typing-extensions>=4.6`，Python>=3.10；psycopg 3.3.3 自身的 `pool` extra 就是不限版本的 `psycopg-pool`，与 3.3.x 同系列发布；来源 pypi.org JSON 元数据），放 `m0` 组；`uv lock` 只新增 psycopg-pool 一包。
- 设计：`DurableStore.transaction()` 从 `psycopg_pool.ConnectionPool.connection()` 取连接，`with` 退出沿用 commit/rollback 语义后连接回池；每事务两条 `SET LOCAL` 不变；`snapshot=True` 设的 `isolation_level`/`read_only` 由池的 `reset` 回调在回池时复位（否则一次快照会把连接永久变只读）；取出前 `check` ping 一次，数据库重启后的死连接直接换新。`PoolConfig`（构造参数或 `OPSPILOT_POOL_{MIN_SIZE,MAX_SIZE,TIMEOUT_SECONDS,MAX_IDLE_SECONDS}`，默认 1/4/5s/60s，见 development.md）；池满等待超时映射为 `TIMEOUT`。池在首个事务时懒建，同一进程里相同 DSN+配置的 store 共用一个池并计数，最后一个 store 被回收或 `close()` 时关池（回收只登记、不拿锁，结清在下一次用池/`close()` 时做：finalizer 在持锁分配触发的 GC 里拿同一把锁会死锁，实测出现过一次整套卡死）；键含 pid，fork 后子进程另建池、不 close 父进程的。worker/web 退出时 `store.close()`。`schema.py` 的迁移/接管连接与测试里的直连 `psycopg.connect` 不经 `transaction()`，未改；web 三个模块都经 `DurableStore.transaction()`，无需改。
- 对照（同机、PG 17.9 临时实例、`rebuild()` x20 取 5 轮最好值，两次重复）：改前每次新建连接 43.4ms / 45.8ms（≈2.2ms/次）；池化后 6.7ms / 7.7ms（≈0.35ms/次），约 6 倍；09-15 记录 B2 为 58ms vs 复用连接 4ms（无取出前 ping）。
- 测试：新增 `tests/integration/test_m1_pool_postgres.py`（复用、快照后不残留只读、`SET LOCAL` 每事务生效、出错回滚后连接干净、池满 TIMEOUT、`close()` 后重建、同 DSN 共池与最后一个释放、env 覆盖）。`M1_DURABLE_POSTGRES=1 M0_B_POSTGRES=1 M0_STEP_POSTGRES=1 pytest tests/integration`（55441 临时实例，`max_connections=12`）：296 passed、10 skipped（仅重启 lab 的用例）、0 failed；全程采样客户端连接峰值 8（默认 max_size=8 时峰值 12 曾打满、一池一 store 时当场 `too many clients`，故默认改 4 并共池）。`make check`：2531 passed、306 skipped、2 xfailed（`tests/test_sql_column_order_independence.py` 的静态守卫原只认 `psycopg.connect(` + `row_factory=dict_row`，扩成同时认 `ConnectionPool(` + `"row_factory": dict_row`，意图不变，需审查者确认）。
- 未执行：fork 后子进程行为只有 pid 键的代码路径，没有真实 fork 测试（web/worker 都是单进程，macOS multiprocessing 为 spawn）；lab 55431 未动，PG 重启下池的换新只靠 `check` 回调，未跑 `M0_B_RESTART=1`。
## PR-c 执行（2026-10-05）

- 状态：PR 已开（issue #78，分支 `chore/m1-prep-state-checks`，worktree `../production-ops-agent-check`），待独立审查与用户合并。
- 迁移 `0002_state_checks`（down_revision `0001_baseline`，downgrade 删约束）。列 → Literal：`opspilot_runs.state` → `RunExecution`（9 值；代码只写其中 7 个，`failed`/`budget_exhausted` 尚无写入路径，约束照 Literal 放行）；`opspilot_incidents.lifecycle` → `IncidentLifecycle`（4 值；代码只写 `open`）。`tests/test_schema_state_checks.py` 用 `typing.get_args` 比对迁移里的字面量与 Literal。
- 未加约束的枚举型列（偏差照实列出，不替用户挑一边）：`opspilot_incidents.state`（领域 `Incident` 没有这个字段，写入值 queued/paused/running/cancelled 是 run 状态的镜像）；`opspilot_controls.action`（写 `new_run`、`cancel`，`ControlAction` 是 `cancel_run` 且无 `new_run`；Literal 的 takeover/close/reopen/authorize_observation/revoke_observation 无写入）；`opspilot_steps.status`（response_committed/tool_result_committed/late_result，领域无 Literal）；`opspilot_inputs.kind`（event/follow_up/correct，无 Literal）；`opspilot_budget_reservations.state`（reserved/spent/unknown，无 Literal）；`opspilot_evidence.status`（`ToolStatus` 在 `opspilot/tools/outcomes.py` 而非 domain，且 `pin` 路径从 view 字典取 status）；`opspilot_subject_events.kind` 是开放字符串。这些要么先对齐词汇（属 `tests/test_architecture.py` xfail 那项架构决定），要么另开 issue。
- 升级前先按表/列数非法值，有则抛 `schema.IllegalStateValues`（表.列 = 值: 行数），`transaction_per_migration` 回滚、数据不动、库停在 0001；`make migrate` 退出 1。
- 接管路径改为：旧库 dump 与新建库升到 **`0001_baseline`** 的 dump 比对 → stamp 0001 → `upgrade head`；`MigrateResult("stamped", <head>)`，CI 两条路径的 grep 改成 `0002_state_checks`；PR-a 的三条断言随 head 移动而改（`test_head_is_the_newest_revision`、stamped 后 dump 等于 fresh head、grown 库 stamp 结果），未削弱。
- 验证（临时 PG 17.9，端口 55451，`OPSPILOT_LAB_DSN` 指向，不碰 55431）：`M1_DURABLE_POSTGRES=1 M0_B_POSTGRES=1 M0_STEP_POSTGRES=1 pytest tests/integration` → 291 passed, 10 skipped；`make check` 通过（2533 passed, 301 skipped, 2 xfailed）。新 PG 测试：`state='banana'` 抛 `CheckViolation`；带非法值的旧库接管后停在 0001、数据与 dump 不变、修数据后重跑升到 head；upgrade→downgrade→upgrade 的 dump 与 fresh head 一致。
- **合并后用户待办**：本机 55431 lab 库现在停在 `0001_baseline`，`install()` 会以 `SCHEMA_NOT_MIGRATED` 拒绝；需 `OPSPILOT_DSN="host=127.0.0.1 port=55431 dbname=m0_budget user=m0_lab" OPSPILOT_PG_DUMP=/opt/homebrew/opt/postgresql@17/bin/pg_dump make migrate`。它先数非法值；10,609 行事故里若有历史非法状态会被拒并打印清单，不改数据。本 PR 没有跑它。
- 未执行：真实 lab 库迁移（见上）；`opspilot_incidents.state` 等六列的约束；`tests/test_architecture.py` xfail 不动（ADR-0007 明示另决）。

## 前提与完成条件

- 前提：SPEC 门槛段落合并；开工前先核实 Alembic 与 `psycopg_pool` 当前版本对 psycopg 3.3 的支持（官方文档），结论写进本记录（Alembic 已核，`psycopg_pool` 留给 PR-b）。新依赖放在 psycopg 所在的依赖组；产品与 M0 依赖分组（09-15 记录 B1）不在本任务。
- 完成条件：
  - 每个 PR：现有测试全过；PR-a 的两条 CI 路径通过；PR-b 有池化前后对照数据；PR-c 有非法值写入被拒的 PG 测试；PR-d 有 SQL 字符串集合一致的证据。
  - 空库 head 与旧库接管后 schema dump 完全一致（证据入本记录）。
  - 独立审查（全新上下文）通过，发现已处置。
- 合并类别：四个 PR 都触及持久化与状态恢复，属用户门，由用户合并。

## 下一步与交接

- PR-a 已合并（#104），本机 lab 库已接管（见下）。PR-b 连接池（#77）已提 PR，等用户门；之后 PR-c（#78）、PR-d（#79）。
- M1-02 D3 的受限 Observer 角色除业务表权限外还需 `GRANT SELECT ON alembic_version`，否则 `install()` 抛 `SCHEMA_VERSION_UNREADABLE`（#104 最后一次修复后），不是 `SCHEMA_NOT_MIGRATED`。
- 本机 55431 lab 库已接管（2026-10-05，用户指示）：不带 flag 被拒、只有两处列顺序差异；带 `--accept-column-order` 接管到 `0001_baseline`，行数不变，接管前全库备份已留存。命令、diff、备份位置与哈希见[接管证据](../evidence/m1-prep-alembic/lab-takeover-2026-10-05.md)。
- 开工顺序：PR-b（#77）→ PR-c（#78）→ PR-d（#79）→ [trace 接入](2026-10-05-m1-prep-trace-langsmith.md) → M1-02 计划第 0 步。
