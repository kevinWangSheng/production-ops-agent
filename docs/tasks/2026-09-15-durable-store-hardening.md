# DurableStore 技术栈与 SQL 加固

- 状态：待开始
- 更新日期：2026-09-15
- 依据：`docs/design/technical-proposal-2026-09-07.md` 第 4、5、7 节；`ADR-0003` 业务记录恢复权威；`SPEC.md` 第 63 行（依赖锁定清单尚未选定）；承接 [M1-01 持久化与恢复](2026-09-14-m1-01-durable-state.md) 的未完成项。
- 工作区：未开始（建议 `chore/durable-store-hardening`）

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

变异测试：把两处 SQL 的列顺序改为 `SELECT i.control_generation,r.*`
（栅栏语义翻转为比较 run 自身代际）后运行全量检查：

```
变异后（全量，含 mypy strict 与 test_architecture）: 1059 passed, 54 skipped, 2 xfailed
                                  mypy: Success: no issues found in 13 source files
还原后: 1059 passed, 54 skipped, 2 xfailed
```

**整个测试套件无一转红，mypy strict 与 ruff 同样无感。** 该构造与 PR #19 修复的 `claim()` 同名列缺陷同源
（见 2026-09-14-m1-01-durable-state.md「独立审查」一节），当时只修了一处。
它对 `ruff`、`mypy` 与现有测试全部不可见。

修复方向：`persistence.py` 内所有 `r.*` / `i.*` 展开为显式列名。
`publish()` 的 `:449` 用 `SELECT i.*,r.owner,...`，当前靠列出现顺序恰好正确，
但 `opspilot_incidents` 一旦新增 `owner`/`deadline`/`epoch`/`lease_until` 同名列即静默翻转，一并处理。

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

### A3　`claim()` 的 `assert` 在 `python -O` 下被剥离

`persistence.py:238` 的 `assert lease is not None` 是返回值的唯一保护。
实测 `python -O` 下该断言被剥离，函数返回 `None` 而签名声明 `Lease`。
mypy strict 因 assert 的类型收窄而通过，检查不到。

### A4　承接自 M1-01 的既有未完成项

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
「尚未做出的架构决定……决定做出并实施后删除此标记」。该决定牵动
`opspilot/domain`，而 `feature/m1-01-domain-types` 分支尚未推送、未建 PR，
现在定会与那条线冲突。**建议等 domain 线落地后再开 ADR，避免写一条随即要改的 ADR。**

### C3　lint 未覆盖行宽

`pyproject.toml` 的 `select = ["E4","E7","E9","F","I","TID251"]` 不含 E501。
`persistence.py` 最长行 272 字符（`:359`），开启 E501 会报 40 处。
是否收紧取决于长 SQL 字面量怎么排版，与 A1 的展开列名一并决定。

## 已核查为无问题的项

- **SQL 注入面为零**：74 个 `%s` 参数占位符，0 处 f-string / `.format` / 字符串拼接进 SQL；
  扫描 `execute(f"`、`.format(`、`" + `、`SQL(` 全部无匹配，36 条 SQL 均为静态字面量。
- **`SET LOCAL` 确实生效**：事务内 `statement_timeout=5s`、`lock_timeout=4s`，
  `transaction_status=INTRANS`。该设置在 autocommit 下会静默失效，此处未踩。

## 执行进展与证据

- 2026-09-15：复核完成，尚未实施任何修复。上述各条的实测命令与输出在本记录内，
  探针针对本地 PostgreSQL 17.9 lab；变异测试用文件副本还原，`git diff --stat` 无残留。
- 未执行：A 类全部修复、对应回归测试、独立审查。

## 下一步与交接

1. 取得 A 类实施授权后开 `chore/durable-store-hardening` 分支，按 A1 → A2 → A3 → A4 顺序修复，每条配回归测试并做变异验证。
2. B 类需用户就「运行时依赖边界怎么划 + 是否引入 psycopg_pool」给出决定后另立任务。
3. C1 需用户就迁移机制给出决定；C2 建议推迟至 `feature/m1-01-domain-types` 落地之后；C3 随 A1 一并定。
4. 本记录不改变任何 SPEC 门槛、`feature_list.json` 的 `passes`，也不代表 M1-01 验收状态变化。
