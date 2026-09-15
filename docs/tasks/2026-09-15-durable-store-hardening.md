# DurableStore 技术栈与 SQL 加固

- 状态：A 类实施完成，PR 待用户审核合并；B、C 类仍待用户决定
- 更新日期：2026-09-15
- 依据：`docs/design/technical-proposal-2026-09-07.md` 第 4、5、7 节；`ADR-0003` 业务记录恢复权威；`SPEC.md` 第 63 行（依赖锁定清单尚未选定）；承接 [M1-01 持久化与恢复](2026-09-14-m1-01-durable-state.md) 的未完成项。
- 工作区：`/Users/shenghuikevin/dev/AI/production-ops-agent-durable-hardening`，分支 `chore/durable-store-hardening`（起点 main `b483a12`）

## 目标与范围

PR #22 合并后（`686965f`）对 `opspilot/persistence.py` 做了一轮技术栈层面的复核，
对象是 SQL 写法、依赖选型与 schema 演进机制，不是业务行为。

本记录收拢三类互不相同的事项，**它们不应混在一个 PR 里**：

- **A 类｜确定性缺陷修复**：单文件内可改、有确定性测试可验、无取舍，本任务范围。
- **B 类｜依赖边界与选型**：需用户决定，本任务只记录证据与决策点。
- **C 类｜架构决定**：需用户决定且牵动 `opspilot/domain`，本任务只记录，不实施。

范围界限：不改业务语义、不改验收步骤、不扩大到 `scripts/` 与 `feature_list.json`。
DDL 变更（补索引）落在开发 lab，按 AGENTS.md 属须授权项，实施前单独说明。

## 前提与完成条件

- 前提：本地 PostgreSQL lab（`scripts/m0/postgres_lab.py start`）；不调用模型或外部付费服务，未知费用为 0。
- 完成条件：A 类各条均有对应回归测试，且经变异测试确认能转红；`make check` 与
  `M1_DURABLE_POSTGRES=1` 定向测试通过；独立审查完成且发现已处置。
- B、C 类不计入本任务完成条件，取得用户决定后另立任务。

## A 类｜确定性缺陷（本任务范围）

### A1　三条写路径的代际栅栏建在重复列名上，且无测试承重

`opspilot/persistence.py:253`（`reserve_budget`）、`:305`（`commit_step`）、
`:359`（`commit_tool`）使用 `SELECT r.*,i.control_generation ...`。
`r.*` 已经包含 `control_generation`，结果集里该列出现两次，
`psycopg.rows.dict_row` 静默取**最后一个**。

实测（PostgreSQL 17.9，把 run 代际置 11、incident 代际置 77）：

```
返回列里 control_generation 出现次数: 2
row['control_generation'] 实际取到 -> 77      （即 i 的值）
=> run 自身的 control_generation 在 dict 中不可达
```

变异测试：把 `control_generation` 的取值翻转为 run 自身代际（即把 `i.control_generation`
移到 `r.*` 之前），三条路径**分别**变异并各跑一次全量检查。`commit_tool` 的 SQL 多一个
`s.control_generation AS step_generation` 列，与另外两条不同，因此单独变异：

| 变异位置 | 变异后全量结果 |
|---|---|
| `reserve_budget` + `commit_step`（同一条 SQL，2 处） | `1059 passed, 54 skipped, 2 xfailed` |
| `commit_tool`（`:359`，单独变异 1 处） | `1059 passed, 54 skipped, 2 xfailed` |
| 基线（未变异） | `1059 passed, 54 skipped, 2 xfailed` |

mypy strict 三次均为 `Success: no issues found in 13 source files`。
**三条路径逐条验证，整个测试套件无一转红，mypy strict 与 ruff 同样无感。**

该构造与 PR #19 修复的 `claim()` 同名列缺陷同源
（见 2026-09-14-m1-01-durable-state.md「独立审查」一节），当时只修了一处。

修复方向：`persistence.py` 内所有 `r.*` / `i.*` 展开为显式列名。

关于 `publish()` 的 `:449`（PR #23 机器人审查指出本记录初稿在此处高估了风险，已核实并更正）：
该行用 `SELECT i.*,r.owner,r.epoch,r.lease_until,r.deadline,...`，所有 `r.x` 都排在 `i.*`
之后，因此**向 `opspilot_incidents` 增加同名列不会翻转它的取值**。实测在事务内给
`opspilot_incidents` 加上 `owner`/`deadline` 列并写入伪造值后，`row['owner']` 取到的
仍是 run 的真实 owner。初稿「一旦新增同名列即静默翻转」的说法不成立，已删除。
`publish()` 仍纳入展开列名的范围，理由是其正确性依赖 select 列表的书写顺序而非任何
显式约束——调换顺序或改用 `r.*` 即会静默改变语义——属可维护性问题，不是当前缺陷。

### A2　两张表的 `incident_id` 无索引

PostgreSQL 不为外键列自动建索引。实测：

```
opspilot_runs 上的索引:  opspilot_runs_pkey | btree (run_id)      （仅此一个）
EXPLAIN SELECT * FROM opspilot_controls WHERE incident_id=%s
  -> Seq Scan on opspilot_controls  (cost=0.00..636.53 rows=107)
```

`control()` 的 `UPDATE opspilot_runs ... WHERE incident_id=%s` 走全表扫描，
而 PR #22 新增的三处 incident 前置锁把 worker 写路径排在它之后，表增长后同时显现。
`opspilot_steps` 与 `opspilot_budget_reservations` 由各自的 `UNIQUE(run_id, ...)`
前缀覆盖，不受影响。缺索引的是 `opspilot_runs` 与 `opspilot_controls` 两张表。

### A3　承接自 M1-01 的既有未完成项

以下已在 [M1-01 记录](2026-09-14-m1-01-durable-state.md)「未完成项」复现并记录，
移入本任务一并处理：

- `rebuild()` 的 `pending_tools` 不按 `control_generation` 过滤。
- 租约守卫复制 4 份且语义已分叉：`publish` 将 `lease_until IS NULL` 视为拒绝，另三处视为通过。
- `CONTROL_CONFLICT` 语义收窄：「incident 存在但 run 行缺失」落入该码，调用方按重试处理会无限循环（当前不可达）。
- `control()` 只守 `blocked`，未守 `failed`/`budget_exhausted`（当前不可达，但与 `waiting_human` 处理不对称）。

## B 类｜依赖边界与选型（待用户决定，不在本任务实施）

### B1　产品代码依赖未声明，与 M0 实验依赖混在同一组

```
产品代码 opspilot/ 实际 import:  psycopg, pydantic
pyproject [project]:            dependencies = []
两者实际声明在:                  [dependency-groups] m0 = [... psycopg, pydantic ...]
```

现在不报错，是因为 `[tool.uv] package = false` 使 `[project].dependencies`
从不被安装，而 `default-groups = ["dev","m0"]` 默认把 m0 组装上。
即：**产品运行时依赖清单目前是空的，实际依赖靠实验组兜住。**

`SPEC.md` 第 63 行已声明 "exact dependency locks are not selected by this decision"，
该文件自身记录了这件事尚未选定。把 `psycopg`/`pydantic` 移出 m0 组、
划出运行时依赖边界是选型动作，不在 A 类范围。

### B2　每次调用新建连接，无连接池

`transaction()` 每次 `psycopg.connect()`，一次 TCP + 认证 + 两条 `SET LOCAL`。实测：

```
rebuild() x20            = 58ms   平均 2.9ms/次（每次新建连接）
同样三条查询复用连接 x20 =  4ms   平均 0.2ms/次
```

约 14 倍。持续运行的 Agent 每个 step 会多次进出该路径。
`psycopg_pool` 是 psycopg 项目自身提供的连接池（尚未加入 `uv.lock`，版本未核）。
采用与否取决于 B1 的边界怎么划，因此合并为同一个决策点。

**不建议引入 SQLAlchemy ORM/Core**：本模块的价值在于 `FOR UPDATE`、加锁顺序、
隔离级别与 `ON CONFLICT` 的精确可见控制——PR #22 的三处「先锁 incident 再锁 run」
正是这种可见性的产物，ORM 会抽掉它。裸 SQL 在此是正确选择；
问题不在缺框架，在 A1/A2 这类「该显式的写成了隐式」。

**Temporal 等 durable execution 引擎不重开**：C3 第 35 行明确「暂不增加 Redis、Kafka、Temporal」。
本模块在做的事（租约 fencing、幂等键、step journal、断点重建、人工信号、预算记账）
确实与该类引擎重合，记录此观察供后续阶段评估，不作为本轮建议。

## C 类｜架构决定（待用户决定，不在本任务实施）

### C1　schema 演进没有机制

`install()` 目前是唯一的 DDL 入口，且**生产代码零调用方**——
7 处 `DurableStore.install()` 调用全部来自 `tests/integration/` 与 `scripts/`（另有 7 处 `ledger.install()` 属另一个类）。仓库内无 Alembic，
无 schema 版本表。`persistence.py:112`、`:127` 两条
`ALTER TABLE ... ADD COLUMN IF NOT EXISTS` 贴在 `CREATE TABLE` 之后，
是 schema 漂移的第一个征兆：改不了列类型、不能回滚、无版本号、无顺序。

不建议 Alembic（会把 SQLAlchemy 拖入主依赖，与 B1 的边界冲突）；
一张 schema 版本表加编号 SQL 文件即可。数据库迁移按 AGENTS.md 属须授权项。

### C2　领域状态在三处各存一份，数据库不设约束

```
UPDATE opspilot_runs SET state='banana'  ->  成功
```

状态列是裸 `text NOT NULL`，无 CHECK 约束。同一套状态词汇同时存在于
SQL 字面量、`opspilot/domain` 的 `Literal` 与状态机三处。

`tests/test_architecture.py:36` 的 `xfail(strict=True)` 原文已声明这是
「尚未做出的架构决定……决定做出并实施后删除此标记」，该标记此刻在 main 上仍然有效
（`make check` 报 `2 xfailed`）。

更正（PR #23 机器人审查指出，已核实并采纳）：`opspilot/domain` **已经在 main 上**——
`6efe24d feat: add opspilot domain types and lifecycle state machines [#F2]` 是
`582ab56`（PR #19）与 `686965f` 的祖先。本记录初稿据 ROADMAP 2026-09-14 段落写作
「`feature/m1-01-domain-types` 尚未推送、未建 PR」，该陈述在本提交处已过期。
远端同名分支仍存在但落后 28 个提交，属待清理的陈旧分支，不构成依赖。

因此**不存在需要等待的前置工作**：领域层与持久化层现在同时在 main 上，两者并存的
状态词汇（SQL 字面量、`Literal`、状态机）就是本条要决的对象，ADR 现在即可开。
唯一的前提是用户就依赖方向给出决定，本任务不代为决定。

### C3　lint 未覆盖行宽

`pyproject.toml` 的 `select = ["E4","E7","E9","F","I","TID251"]` 不含 E501。
`persistence.py` 最长行 272 字符（`:359`），开启 E501 会报 40 处。
是否收紧取决于长 SQL 字面量怎么排版，与 A1 的展开列名一并决定。

## 已核查为无问题的项

- **SQL 注入面为零**：74 个 `%s` 参数占位符，0 处 f-string / `.format` / 字符串拼接进 SQL；
  扫描 `execute(f"`、`.format(`、`" + `、`SQL(` 全部无匹配，36 条 SQL 均为静态字面量。
- **`SET LOCAL` 确实生效**：事务内 `statement_timeout=5s`、`lock_timeout=4s`，
  `transaction_status=INTRANS`。该设置在 autocommit 下会静默失效，此处未踩。
- **`claim()` 的 `assert lease is not None` 不是可达缺陷**（PR #23 机器人审查指出，已核实并采纳）。
  `persistence.py:193-239` 的 `if/elif` 链是穷尽的：唯一不抛出的分支置 `incompatible=True`，
  随后 `:236-237` 抛 `INCOMPATIBLE_STATE`；`else` 分支必定赋值 `lease`。因此不存在
  「未赋值且不抛出」的可达路径。`python -O` 下跑全量为 `1059 passed, 54 skipped, 2 xfailed`，
  与不带 `-O` 完全一致。该 `assert` 是防御性不变量兼 mypy 收窄点，保留，不列为待修项。

## 执行进展与证据

- 2026-09-15（复核阶段，历史）：复核完成，尚未实施任何修复。上述各条的实测命令与输出在本记录内，
  探针针对本地 PostgreSQL 17.9 lab；变异测试用文件副本还原，`git diff --stat` 无残留。

### A 类实施（2026-09-15）

五个提交，每个提交都单独跑过 ruff / ruff format / mypy strict / 全量 pytest（含 PG opt-in）并通过：

| 提交 | 缺陷 |
|---|---|
| `fix: read the lease fence from an explicit incident generation column` | A1 + A3 租约守卫分叉 |
| `fix: index the incident_id foreign keys in the durable schema` | A2 |
| `fix: filter rebuild pending tools by the current control generation` | A3 `pending_tools` |
| `fix: separate unknown identity and missing run rows from CONTROL_CONFLICT` | A3 `CONTROL_CONFLICT` 收窄 |
| `fix: refuse non-cancel control from run states outside the open list` | A3 `control()` 只守 `blocked` |

#### 取舍与依据

- **租约守卫收敛到哪一侧**：取 `publish` 的「`lease_until IS NULL` 视为拒绝」。
  `control()` 清空 `lease_until` 正是收回 worker 权限的动作；另三份把 NULL 读成通过，
  等于让被收回权限的 worker 只靠 owner 一项守住。
- **A3 第 4 条（`failed`/`budget_exhausted` 当前不可达）选「补守卫」而非「补一条断言不可达的测试」**：
  改成放行名单是*拒绝*，不为这两个状态发明新策略；所有当前可达 run 状态逐一核对行为不变；
  且守卫可被确定性测试覆盖，而「断言不可达」的测试只能钉住今天的实现。
- **A3 第 3 条顺带纠正了「事故不存在」也报 `CONTROL_CONFLICT`**：同一条 JOIN 造成的同类缺陷，
  纠正后与 `claim()`/`rebuild()` 已有的 `UNKNOWN_IDENTITY` 用法一致。**这是本次唯一一处
  可达的对外错误码变化**，已在 PR 中单独列出。
- **A2 不引入迁移机制**：`CREATE INDEX IF NOT EXISTS` 跟随 `install()` 内既有的
  `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` 写法，对已有库再次 `install()` 即补建。
  C1「schema 演进无机制」未被本任务改变，仍然开着。
- **`publish()` 当前不是缺陷**：原写法里所有 `r.x` 都排在 `i.*` 之后（本记录 A1 节已核实）。
  纳入展开范围的理由是可维护性，不是已存在的漏洞。

#### 变异验证（完成条件要求的「测试能转红」）

修复后逐条把代码变异回旧写法，记录真实输出。基线（未变异）：
`make check` = `1051 passed, 81 skipped, 2 xfailed`；
`M1_DURABLE_POSTGRES=1 pytest tests/integration/test_m1_durable_state_postgres.py` = `27 passed`
（实施前基线分别为 `1050 passed, 75 skipped, 2 xfailed` 与 `21 passed`）。

| 变异 | 转红的测试 | 全量（无 PG opt-in） |
|---|---|---|
| `reserve_budget` 栅栏改读 run 副本 | `test_generational_fence_is_keyed_to_the_incident_not_the_run_copy` | `1051 passed` |
| `commit_step` 同上 | 同上 | `1051 passed` |
| `commit_tool` 同上 | 同上 | `1051 passed` |
| `publish` 同上 | 同上 | `1051 passed` |
| `commit_step` 改回 `SELECT r.*,i.control_generation` | `test_persistence_sql_never_selects_a_qualified_star` | `1 failed, 1050 passed` |
| 删掉两条 `CREATE INDEX` | `test_install_indexes_the_incident_foreign_keys_on_an_existing_database` | `1051 passed` |
| 去掉 `pending_tools` 代际过滤 | `test_rebuild_drops_pending_tools_from_a_superseded_generation` 与 `test_fresh_lease_cannot_commit_tools_into_pre_follow_up_step` | `1051 passed` |
| 租约守卫改回「NULL 视为通过」 | `test_a_cleared_lease_cannot_be_used_even_when_owner_and_epoch_still_match` | `1051 passed` |
| `control()` 改回单条 JOIN | `test_control_distinguishes_unknown_identity_from_a_retryable_conflict` | `1051 passed` |
| 放行名单改回只点名 `blocked` | `test_non_cancel_control_is_refused_from_every_unlisted_run_state` | `1051 passed` |

十次变异中，mypy strict 九次报 `Success: no issues found in 13 source files`、ruff 十次 `All checks passed!`——
**与复核阶段的结论一致：这一类缺陷靠 lint 与类型检查检不出来，只能靠测试。**
其中五条（A1 四条 + 租约守卫）必须安排存储状态才能暴露，因为公开接口下两份代际
不会分叉、`lease_until` 与 `owner` 总是被一起清空；沿用
`test_require_row_reports_inconsistent_state_not_a_transient_failure` 的同一思路。

#### A2 索引证据

本地 lab（PostgreSQL 17.9，6876 runs / 30645 controls）`EXPLAIN (ANALYZE, BUFFERS)`：

| 查询 | 补索引前 | 补索引后 |
|---|---|---|
| `UPDATE opspilot_runs ... WHERE incident_id=%s AND state IN (...)` | Seq Scan，cost 239.75，137 buffers，过滤掉 6875 行 | Index Scan，cost 8.31，3 buffers |
| `SELECT * FROM opspilot_controls WHERE incident_id=%s` | Seq Scan，cost 652.05，378 buffers，过滤掉 30645 行 | Bitmap Index Scan，cost 33.06，2 buffers |

#### 本轮新观察到、未处理的项

- `claim()` 内 `elif row["run_state"] == "running" and ... lease_until > now` 与函数开头的
  `active_lease` 判定重复且不可达（同一个 `now` 下前者为真必已抛 `LEASE_ACTIVE`）。
  属范围外的死分支清理，本任务未动。
- 共用的本地 lab 里 `public.m0_live_once` 缺 PR #25 新增的 `models_metadata_sha256` 列，
  导致 `tests/integration/test_m0_live_postgres.py` 三条用例在本机失败。该文件只 import
  `scripts.m0.*`，与本任务改动无接触；CI 用全新容器不复现。这是 **C1 的又一个实例**
  （`install_live()` 的 `CREATE TABLE IF NOT EXISTS` 不会给已有表补列），未在本任务内处理。

## 下一步与交接

1. A 类实施与变异验证已完成，独立审查已进行；PR 等待用户审核合并（Agent 不合并）。
2. B 类需用户就「运行时依赖边界怎么划 + 是否引入 psycopg_pool」给出决定后另立任务。
3. C1 需用户就迁移机制给出决定（本轮又新增一个实例，见上）；C2 的前置工作已在 main，
   取得依赖方向决定后即可开 ADR；C3 随 A1 一并定——A1 已把长 SQL 展开得更长，
   开启 E501 的取舍未变，仍待决定。
4. 本记录不改变任何 SPEC 门槛、`feature_list.json` 的 `passes`，也不代表 M1-01 验收状态变化。
