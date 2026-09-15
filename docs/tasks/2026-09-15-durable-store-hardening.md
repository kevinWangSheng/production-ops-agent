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
`make check` = `1051 passed, 83 skipped, 2 xfailed`；
`M1_DURABLE_POSTGRES=1 pytest tests/integration/test_m1_durable_state_postgres.py` = `29 passed`；
`M1_DURABLE_POSTGRES=1 pytest -q` 全量 = `1080 passed, 54 skipped, 2 xfailed`
（实施前基线分别为 `1050 passed, 75 skipped, 2 xfailed` 与 `21 passed`；
审查修复前为 `81 skipped` / `27 passed`）。

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
| `rebuild()` 过滤改读 run 副本（审查发现） | `test_rebuild_filters_pending_tools_by_the_incident_generation_not_the_run_copy` | `1051 passed` |
| 删 `_lease_revoked` 的 `owner` 子句（审查发现） | `test_lease_identity_and_deadline_each_fence_all_four_write_paths` | `1051 passed` |
| 删 `epoch` 子句（审查发现） | 同上 | `1051 passed` |
| 删 `deadline` 子句（审查发现） | 同上 | `1051 passed` |

十四次变异中，mypy strict 十三次报 `Success: no issues found in 13 source files`、ruff 十四次 `All checks passed!`——
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

#### 独立审查（2026-09-15，两个全新上下文 agent，未参与实现）

两轮审查各自独立跑了变异实验。**均无阻断级发现。** 逐条处置：

**采纳并修复（提交 `d440306`）**

1. `rebuild()` 新增的代际过滤「读哪一份代际」零覆盖。审查方把它改成读 run 行副本
   （`(run or row)["control_generation"]`，mypy 干净的等价写法），实测 `1078 passed`
   无一转红——**这是本 PR 要修的 A1 缺陷类在读路径上的复刻**。已把权威绑成具名变量
   并补专门用例，变异实测转红。
2. `_lease_revoked` 的 owner / epoch / deadline 三条子句零覆盖。审查方核实这是既存
   问题（在 `b483a12` 上删掉四份守卫的 owner/epoch 共 8 处，PG 测试同样 `21 passed`
   无一转红），但 A3 第 2 条正是「租约守卫收敛」，合成一份后补测试的成本已最低。
   已补用例，三条子句逐条变异转红。
3. `test_non_cancel_control_is_refused_from_every_unlisted_run_state` 名字承诺
   "every unlisted run state"，实际只遍历两个状态。已改为遍历
   `RUN_EXECUTION.states` 减去四个放行状态。
4. A2 测试对共用 lab 的**产品索引**做 `DROP INDEX`（取 ACCESS EXCLUSIVE 表锁）。
   审查方在自建表上实测：并发写入方持 ROW EXCLUSIVE 时 `CREATE INDEX` 会
   `LockNotAvailable: canceling statement due to lock timeout after 4.0s`——删到一半失败
   就把 lab 留在「索引已删、未重建」状态并改变其他 worktree 的查询计划。
   初版只加了 `finally: store.install()`，缩小窗口但没有消除。
   **已在提交 `131b7b9` 重做**：默认 schema 只做只读断言；「已有数据库上也补得上」
   这一半改在 `m1_index_backfill_<uuid>` 一次性 schema 里验证
   （`options=-csearch_path=` 把 `install()` 指过去，用完 `DROP SCHEMA CASCADE`），
   默认 schema 全程零 DDL。变异验证：删掉任一条 `CREATE INDEX`，该用例仍转红
   （`1 failed, 31 passed`），承重性未因改写减弱。

**采纳，但只能在记录层面更正（历史改写与 force-push 未获授权）**

5. 提交 `059c733` 正文写「三条写路径与 publish 用 …，结果集里 `control_generation`
   出现两次」——**对 publish 不成立**。main 的 publish SQL 里 run 侧那份已经起了别名
   `run_generation`，审查方实测 `dup_cols=[]`。本记录 A1 节写的才是对的
   （publish 当前不是缺陷，纳入展开的理由是可维护性）。该提交信息把 PR #23
   机器人审查已撤回的结论重新写了回去，**以本记录与 PR 描述为准**。
6. 提交 `99e9401` 正文写「`failed` 与 `budget_exhausted` 在 RUN_EXECUTION 里与
   [`blocked`] 同为终态」——`blocked` **不是**终态，
   `opspilot/domain/runs.py:70` 给它留了 `human_cancel` 与 `handoff_failed` 两条出边。
   `failed`/`budget_exhausted`/`completed`/`cancelled` 才是终态。测试 docstring 已更正。
7. `control()` 的可观察行为变化实际覆盖 **4 个 run 状态 16 个组合**
   （`failed`、`budget_exhausted`、`cancelled`、`completed`），提交信息只点名了两个。
   补充实测：见下节差分探针——**公开接口可达的状态组合零差异**。

**记录、不实施（超出 A 类范围）**

8. A2 的两条索引在产品代码里**没有调用方**：`opspilot/` 内零处调用 `install()`，
   14 处全在 `tests/integration/` 与 `scripts/` 下。这与 C1 同源，C1 仍开着。
9. `install()` 的 `CREATE INDEX` 不是 `CONCURRENTLY`，且跑在
   `statement_timeout=5s` / `lock_timeout=4s` 的事务里（`persistence.py:116-117`）。
   审查方在自建表上实测：有并发写入时会 `LockNotAvailable` 超时，映射为 `TIMEOUT`。
   因此提交 `4867bae` 的「对已建好的库再次 `install()` 即补建」**只在空库/小库或无并发写入时成立**，
   在有并发写入的非空库上不成立。`CONCURRENTLY` 不能在事务块内执行，改它要动
   `install()` 的结构——属 C1。叠加第 8 条（生产零调用方），这条 DDL 目前只对开发 lab 实际生效。
   **该限定已写入 PR 描述。**
12. `control()` 的实际行为 delta 比本记录 A3 的字面更宽：除「run 行缺失」与
    「`failed`/`budget_exhausted`」外，还改了①事故不存在的错误码、②非法 action 与 run 行缺失
    同时出现时的检查顺序（`CONTROL_CONFLICT` → `INVALID_INPUT`）、③`completed`/`cancelled`
    两个 run 状态。三项均实测为 fail-closed、公开接口不可达、无生产调用方，不回退；
    **完整 delta 已列入 PR 描述**供用户判断是否超范围。
10. `_CONTROL_OPEN_RUN_STATES` 是 run 状态词汇的第四份副本，
    与 `opspilot/domain/runs.py:78` 的 `ACTIVE_RUN_EXECUTIONS` 差一个 `paused` 却没引用它。
    引用它会让 persistence 依赖 domain，直接翻掉 `test_persistence_builds_on_domain`
    的 `xfail(strict=True)`——那正是 C2 要决的事，本 PR 不代为决定。C2 的欠债因此加大一点。
11. `cancel` 在终态 run 上仍会造成 incident/run 状态分叉（既存，非本 PR 引入）。
    审查方实测：run 强制为 `failed` 时 `control(incident,0,"cancel")` 返回 1，事后
    incident 为 `cancelled`/gen 1 而 run 仍是 `failed`/gen 0，因为 `persistence.py`
    cancel 分支的 `state IN (...)` 不含 `failed`。main 行为相同。本 PR 关掉了非 cancel
    的一半不对称，另一半留着待决。
13. `rebuild()` 保留三处裸 `SELECT *` 并原样返回给调用方（单表查询，无重复列风险）。
    新增的结构测试只禁「带表别名的 `*`」，向这三张表加一列仍会静默改变
    `rebuild()["run"]` / `["steps"]` 的返回形状。
14. A1 四条路径的行为覆盖集中在
    `test_generational_fence_is_keyed_to_the_incident_not_the_run_copy` 一个用例上
    （四次变异每次只有它转红）。结构测试是第二道但覆盖的是另一种失效模式，
    两者互补、不可合并或删除其一。审查方另用 MUT-C 实测：既有的
    `test_old_generation_step_and_tool_writes_are_fenced` 在代际栅栏被削弱时**不会**转红
    （`control()` 同时清空 owner，owner 一项就足以拒绝），即新用例是代际栅栏的唯一承重点。

#### 独立审查明确标注的未验证项（不得记为已验证）

- **并发 / ABBA 死锁压测未做。** 第一份审查的加锁顺序结论来自 `EXPLAIN` 计划与代码路径
  分析；第二份用 `FOR UPDATE NOWAIT` 实测了 main 与 HEAD 的加锁行集合完全一致
  （`commit_step`/`publish` 为 `['incident','run']`，`commit_tool` 为 `['incident','run','step']`），
  这比 EXPLAIN 推断强，但**仍不是死锁注入压测**。`control()` 拆成两条语句后的新路径未做交错压测。
- **真实生产库或大表上 `install()` 补建索引的可行性未验证**（见第 9 条）。
- **A2 的 `EXPLAIN (ANALYZE, BUFFERS)` 数字第二份审查未复现**；第一份在 lab 长到
  8555 runs / 36412 controls 时复核，方向一致（cost 8.30 vs 277.06、4.35 vs 905.71）。
- **B 类、C 类未审**（不在本轮范围）。
- 第二份审查未单独跑 `make check`，跑的是 `pytest -q` + `mypy` + `ruff check .`；
  未逐行通读 PRODUCT-CONSTRAINTS 与 SPEC 的 Verification and delivery 一节。

#### ROADMAP 未在本分支更新（team lead 判定）

现有四条分支并行（`feature/m1-01-tool-executor`、`feature/m1-01-intake-auth`、
`chore/instruction-contract-impl` 与本分支），ROADMAP 顶部是所有人共用的同一段项目级状态
文字，各分支各改一次必然连环冲突，且合并顺序未定、先合的那条写的状态到后面就是错的
（`docs/tasks/2026-09-14-m1-01-tool-executor.md:152-153` 记有同样判断）。
本分支曾在 `d399b3b` 改过 ROADMAP，已于 `131b7b9` 回退到与 main 一致；
项目级状态由 team lead 在四条 PR 合并后按真实状态统一更新。

#### 差分探针：公开接口可达状态下的行为对照

审查发现 7 促成的复核。同一脚本分别在 HEAD 与 `b483a12` 的 `persistence.py` 下运行：

- `control()` × 9 个公开接口可达场景 × 5 个 action（45 组，incident 与 run 状态
  始终一致）：**零差异**。
- `claim` / `reserve_budget` / `commit_step` / `commit_tool` / `publish` / `rebuild`
  × 4 个场景（live_lease、after_pause、after_follow_up、after_cancel）：
  **唯一差异是 `rebuild()["pending_tools"]`**——代际推进后的三个场景从
  `[(step, 1)]` 变为 `[]`，`live_lease` 场景完全相同。写路径结果、`claim` 结果、
  `publish` 结果、`rebuild` 的其余字段全部一致。

即：`control()` 对 `cancelled`/`completed` run 状态的行为变化，只在 incident 与 run
状态被直接改库改成互相矛盾时可见；`publish()` 与 `control('cancel')` 都在同一事务里
同时写两边，公开接口构造不出这种状态。

#### 机器人 code review 处置

Codex Code Review 与 Security Review 已在 `063f48c` 上完成。**一条 P1 inline comment**
（`opspilot/persistence.py:150`，"Build indexes without blocking active writers"）——
与独立审查 F5 同一条：`install()` 的 `CREATE INDEX` 非 `CONCURRENTLY`，在有并发写入的
非空库上会被 `lock_timeout` 打断并回滚整个 install。

**处置：拒绝在本 PR 修改，事实采纳并已记录。** 依据：①`CONCURRENTLY` 不能在事务块内执行，
而 `install()` 整体包在 `self.transaction()` 里，改它要重构 DDL 执行结构或引入迁移路径，
正是 C1 的对象，本任务对 C 类只记录不实施；②`install()` 产品代码零调用方，这两条 DDL
今天只对开发 lab 执行；③该限定已写入 PR 描述与本记录「记录、不实施」第 9 条。
已在 thread 内回复上述理由并 resolve。

`76447ef` 上返回**第二条 P1**（`persistence.py:415`，"Verify that the locked run belongs to
the incident"）。**采纳并修复：`75abf95`。**

它描述的机制有一处不成立，已在 thread 内用实测更正：`UPDATE ... WHERE incident_id=%s`
并非「affects zero rows」——本 incident 真正的 run 的 `incident_id` 仍指向它，cancel 场景下
那一行会被正确更新（探针实测 victim 自己的 run 为 `cancelled`/1）。

但结论成立，真实机制是另一条：状态**判定**读 `current_run_id` 指向的 run，而状态**推进**按
`incident_id` 作用于本 incident 真正的 run；两者指向不同的行时，一个外来的 running run 会把
blocked run 的保护顶开。实测（HEAD 与 main 完全一致，**属既存问题**——main 的 JOIN 同样只按
`r.run_id=i.current_run_id` 关联）：victim 自己的 run 为 `blocked` 时 `pause` 仍返回
generation 1，事后 incident 为 `paused`/1 而它自己的 run 仍是 `blocked`/0。

修法：run 查找补 `AND incident_id=%s`，落到 `INCONSISTENT_STATE` 且不推进任何状态。
严格收紧、无可达行为变化（`current_run_id` 只在 `accept()` 里写成同一事务创建的那个 run，
此后无人改写）。**修它而不修上一条的依据**：它落在 A3 第 3 条的范围内，改的正是本 PR 重写过的
那段查找，一行收紧即可；上一条要重构 `install()` 的 DDL 执行结构，属 C 类。
新增 `test_control_refuses_a_current_run_pointer_into_another_incident`，
变异（去掉 `AND incident_id=%s`）转红。

#### CI 状态与一个仓库级阻塞（不在本任务范围）

PR #26 的 `m0-postgres` job 通过；`checks` job 在 "Secret scan and synthetic detection
self-test" 这一步红，**原因不在本 PR 内**：

- `.github/workflows/ci.yml:27` 的 checkout 用 `fetch-depth: 0`，CI 会取下 origin 上**所有分支**；
- `scripts/check_secrets.py` 最后一段是
  `gitleaks git --log-opts="--all --no-ext-diff --no-textconv"`，扫的是**所有 ref 的历史**。

两者叠加，任何一条分支上的命中都会让**仓库里每个 PR** 的 secret scan 失败。当前命中的是
`feature/m1-01-intake-auth` 的 `64254dfe`（`tests/test_m1_intake_auth.py:198`，
`generic-api-key` 规则）。`git merge-base --is-ancestor` 核实：只有该分支含它，main 不含。

时间线一致：

```
run 35032644464  cf67cb5  started 2026-09-15T22:47:02Z  success
leak commit 64254dfe                 committed 22:54:45Z
run 35033476575  d399b3b  started 2026-09-15T22:57:49Z  failure（重跑一次同样失败）
```

本 PR 内容本身经三次独立复现均为干净：只扫本分支历史 `0 findings`；depth-1 单分支 clone
跑完整 `check_secrets.py` 得 `SECRET_SCAN_PASSED`；取 `refs/pull/26/merge` 后在
**linux/amd64 容器**里用 CI 同一个 checksum 校验过的 `gitleaks_8.30.1_linux_x64` 跑同一脚本，
三段扫描分别为 `SNAPSHOT index 0 / SNAPSHOT worktree 0 / GIT 0`。

未处理，已上报：修 `feature/m1-01-intake-auth` 属该任务的范围；改
`scripts/check_secrets.py` 的 allowlist 或 workflow 的扫描范围属安全检查本身的变更，
两者都不在本任务授权内。该分支处理完后重跑 CI 即可。

#### 本轮新观察到、未处理的项

- `claim()` 内 `elif row["run_state"] == "running" and ... lease_until > now` 与函数开头的
  `active_lease` 判定重复且不可达（同一个 `now` 下前者为真必已抛 `LEASE_ACTIVE`）。
  属范围外的死分支清理，本任务未动。
- 共用的本地 lab 里 `public.m0_live_once` 缺 PR #25 新增的 `models_metadata_sha256` 列，
  导致 `tests/integration/test_m0_live_postgres.py` 三条用例在本机失败。该文件只 import
  `scripts.m0.*`，与本任务改动无接触；CI 用全新容器不复现。这是 **C1 的又一个实例**
  （`install_live()` 的 `CREATE TABLE IF NOT EXISTS` 不会给已有表补列），未在本任务内处理。

## 下一步与交接

1. A 类实施与变异验证已完成，两轮独立审查已完成且发现已逐条处置（采纳并修复 5 项、
   记录层面更正 3 项、记录不实施 7 项）；PR #26 等待用户审核合并（Agent 不合并）。
   ROADMAP 由 team lead 在四条并行 PR 合并后统一更新，不在本分支改。
   **阻塞**：`checks` job 因 `feature/m1-01-intake-auth` 上的 secret-scan 命中而红（见上节），
   该分支处理完后重跑 CI；`m0-postgres` 已通过，Codex code review 与 security review 均无发现。
2. B 类需用户就「运行时依赖边界怎么划 + 是否引入 psycopg_pool」给出决定后另立任务。
3. C1 需用户就迁移机制给出决定（本轮又新增一个实例，见上）；C2 的前置工作已在 main，
   取得依赖方向决定后即可开 ADR；C3 随 A1 一并定——A1 已把长 SQL 展开得更长，
   开启 E501 的取舍未变，仍待决定。
4. 本记录不改变任何 SPEC 门槛、`feature_list.json` 的 `passes`，也不代表 M1-01 验收状态变化。
