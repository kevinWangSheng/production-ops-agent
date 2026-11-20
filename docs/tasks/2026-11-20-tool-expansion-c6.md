# 工具面拓展 C6：数据库只读查询

- 状态：进行中（C6a 已合并 #746；C6b ClickHouse/SQL Server 公开接口已定，等测试作者；C6c MongoDB 另开）
- 更新日期：2026-11-20
- 依据：[B/C 批次记录](2026-11-16-tool-expansion-bc.md)、[决定表](2026-11-12-tool-expansion-contract.md) R8-9、R8-15、R8-16、R8-25 与 r11（C6-U1=a；C6-0 至 C6-22 可逆默认全部采纳）；PRODUCT-CONSTRAINTS 第 18 行（模型自写查询只在注册的只读工具里跑，SQL 以只读账号为第一道、语句检查为第二道）；C3 第 3、8 节；上游 HolmesGPT `a0009195` `holmes/plugins/toolsets/database/`。issue #570。预审全文在仓库外（`~/.agent/handoffs/opspilot-c6-prereview-2026-11-19.md`），细节以它和 r11 为准
- 工作区：C6a 实现 `../poa-570-sql`，分支 `feature/570-sql-a`（已合并，worktree 已删）；C6b 实现 `../poa-570-sqlb`，分支 `feature/570-sql-b`（起点 `0c0492f1`）

## 目标

调查能对一个配置好的 SQL 数据库执行只读查询、列出表、看表结构（C6a：PostgreSQL、MySQL、MariaDB）。工具只进扩展档位 `otel-demo-ext`，且只有设了 `OPSPILOT_SQL_URL` 时才出现；未设时 `otel-demo` 与扩展档位所有现有 revision 字节不变。只读由工具保证，四层：只读账号（第一道）、语句检查（第二道）、会话只读事务、每次连接后工具自查账号权限（C6-U1）。

## 行为（公开接口，C6a）

| 工具 | 参数（必填加粗） | 做什么 | 返回 `results` 每行 | `row_unit` | verb |
|---|---|---|---|---|---|
| `database_sql_query` | **`sql`**（字符串）、`limit`（整数，默认 100，1–500） | 原样执行模型的一条只读语句，最多取 `limit+1` 行判断截断；工具不改写 SQL | `{列名: 值}`，列顺序同结果集；重名列第 2 个起加 `#2`、`#3` 后缀（递增到未被占用） | `rows` | `query` |
| `database_sql_list_tables` | `schema`、`include_views`（布尔，默认 true） | 写死的目录查询 | `{schema, name, type}`，`type` ∈ `table`、`view`、`materialized_view`、`partitioned_table`、`foreign_table`（MySQL/MariaDB 只有前两种）；按 `schema`、`name` 排序；最多 500 行 | `tables` | `get` |
| `database_sql_describe_table` | **`table_name`**、`schema` | 写死的目录查询 | 每列 `{name, type, nullable, default, ordinal}`，按 `ordinal`；视图另带 `primary_key`（列名数组）、`foreign_keys`（`{name, columns, referred_schema, referred_table, referred_columns}`）、`indexes`（`{name, columns, unique, primary, definition}`，`definition` Postgres 为 `pg_get_indexdef`，MySQL 为 `null`） | `columns` | `get` |

- 方言（C6-7、C6-13）：URL 前缀定方言，不连库。`postgresql://`、`postgres://` → `postgresql`；`mysql://`、`mariadb://` → `mysql`（MySQL 与 MariaDB 共用方言与描述，连上后按 `VERSION()` 是否含 `MariaDB` 选超时语法）。C6a 里 `clickhouse+http://`、`mssql://` 与其他前缀报 `SQL_CONFIG_INVALID`（C6b 再开）。方言写进 `database_sql_query` 的描述，所以每种方言一个 revision。
- `schema` 缺省：Postgres 列表取所有用户 schema（去掉 `pg_catalog`、`information_schema`、`pg_toast*`、`pg_temp*`），describe 按 `search_path` 解析（`pg_table_is_visible`）；MySQL/MariaDB 取 `DATABASE()`。`table_name` 含一个 `.` 且没给 `schema` 时按 `schema.table` 拆。`schema`、`table_name` 只接受 `[A-Za-z0-9_.$-]`、1–128 字符，否则 `INVALID_IDENTIFIER`；目录查询一律参数绑定，不拼字符串。describe 查不到列报 `INVALID_PARAMS`，notes 开头 `TABLE_NOT_FOUND`。
- 时间（C3 §8 当前状态类）：读的是查询时刻的库内状态。来源时间留空（同 B3-U1）；notes 第一条写「查询时刻读取（即本次调用的观察时间），不是授权窗口内的状态」；`cannot_prove`：查询时刻的数据，不能证明事故窗内的状态。工具不接受 `start`/`end`，不替模型加时间过滤。

### 语句检查（第二道，C6-3，发请求前；拒绝时替身驱动收到 0 条语句）

1. 长度 1–20000 字符；只许可打印字符加 `\n`、`\r`、`\t`，否则 `INVALID_QUERY`。
2. 词法剥离（方言相关），剥掉的区域换成一个空格：注释 `--…`（MySQL 方言里 `--` 后不是空白或行尾则拒绝）、`/* … */`（Postgres 允许嵌套；MySQL 方言 `/*!`、`/*M!` 可执行注释拒绝）、MySQL `#…`；字符串 `'…'`（`''` 转义）、Postgres `E'…'`、`$tag$…$tag$`，MySQL `"…"`；带引号的标识符 `"…"`（Postgres）、`` `…` ``（MySQL，``` `` ``` 转义）。反斜杠转义对 `'…'`/MySQL `"…"` 两种理解（转义、不转义）各扫一遍，**两遍都通过才放行**（服务器 `NO_BACKSLASH_ESCAPES` / `standard_conforming_strings` 不同也不漏）。Postgres `U&"` 拒绝。字符串、注释、标识符未闭合报 `INVALID_QUERY`。
3. 只许一条语句：剥离后的文本里只允许末尾一个 `;`（后面只有空白）。
4. 第一个词（可跳过前导 `(`）必须是 `SELECT`、`WITH`、`EXPLAIN`、`SHOW`、`DESCRIBE`、`DESC`。
5. 任意位置出现这些整词（不分大小写）拒绝：`INSERT UPDATE DELETE DROP CREATE ALTER TRUNCATE REPLACE MERGE GRANT REVOKE VACUUM`（上游全部）加 `INTO COPY CALL EXEC EXECUTE DO SET LOAD HANDLER LOCK OUTFILE DUMPFILE`，以及词序列 `FOR UPDATE`、`FOR SHARE`、`FOR NO KEY UPDATE`、`FOR KEY SHARE`。`REPLACE()` 函数与 MySQL `CHARACTER SET` 因此也被拒（宁可误拒，描述里写明）；带引号的同名标识符放行。
6. 函数调用（名字后跟可选空白与 `(`，含 `schema.` 限定，以及带引号标识符后跟 `(`）拒绝：`pg_sleep*`、`pg_terminate_backend`、`pg_cancel_backend`、`set_config`、`lo_*`、`pg_read_file`、`pg_read_binary_file`、`pg_ls_dir`、`dblink*`、`pg_advisory*`、`pg_notify`、`pg_stat_reset*`、`pg_reload_conf`、`pg_rotate_logfile`、`pg_switch_wal`、`pg_logical_emit_message`、`SLEEP`、`BENCHMARK`、`GET_LOCK`、`LOAD_FILE`、`MASTER_POS_WAIT`、`SOURCE_POS_WAIT`、`xp_*`、`sp_*`、`OPENROWSET`、`OPENQUERY`、`OPENDATASOURCE`、`url`、`file`、`s3`、`remote`、`remoteSecure`、`mysql`、`postgresql`、`executable`（清单不分方言）。
- 第 3–6 条拒绝报 `QUERY_CONSTRUCT_NOT_ALLOWED`（归 `INVALID_PARAMS`），notes 开头写码与命中的词。`EXPLAIN ANALYZE SELECT …` 放行。宣称里不说「语句检查保证只读」。检查函数为模块级 `sql_query.sql_statement_problem(sql, dialect) -> str | None`（集成测试 ⑥ 用 monkeypatch 跳过它）。

### 每次调用的语句序列（新连接、用完即关，不建池、不重试）

- Postgres（psycopg，`application_name=opspilot-sql-tool`，扩展协议，故服务器侧也拒多语句）：`BEGIN READ ONLY` → `SET LOCAL statement_timeout = <ms>`、`SET LOCAL lock_timeout = <ms>`、`SET LOCAL idle_in_transaction_session_timeout = <ms>` → 自查（注释标记 `/* opspilot:account-check */`）→ 模型语句或目录查询（标记 `/* opspilot:catalog */`）→ `ROLLBACK` → 关连接。
- MySQL/MariaDB（PyMySQL，不开 `MULTI_STATEMENTS`，`autocommit=True` 后显式开事务）：`SELECT VERSION(), CONNECTION_ID()` → MySQL `SET SESSION max_execution_time = <ms>` / MariaDB `SET SESSION max_statement_time = <秒>`，两者再 `SET SESSION lock_wait_timeout = <秒>` → `/* opspilot:account-check */ SHOW GRANTS` → `START TRANSACTION READ ONLY` → 模型语句或目录查询 → `ROLLBACK` → 关连接。
- 任何一步失败：尽力 `ROLLBACK` 并关连接；只读事务开不起来报 `SOURCE_ERROR`，不吞（C6-4）。结果按行流式读（psycopg `stream`、PyMySQL `SSCursor`），够 `limit+1` 行即停读、取消并关连接。
- 驱动接缝：`SqlTransport(config, *, connector=None, request_timeout_seconds=30.0)`；`connector(config, timeout_seconds) -> SqlConnection`，`SqlConnection` 有 `execute(statement, params=())`、`query(statement, params=(), max_rows) -> (columns, rows, more)`、`server_version`、`cancel()`、`close()`。工具发出的每条语句都经过这两个方法，合同测试用替身 connector 记录完整顺序。

### 账号自查（C6-U1）与业务库隔离（C6-8）

- Postgres 自查是一条查询，每行一个问题码，0 行为通过。对当前用户及其能 `SET ROLE` 的所有角色（`pg_has_role(..., 'MEMBER')`）：`rolsuper`、`rolcreaterole`、`rolcreatedb`、`rolreplication`、`rolbypassrls` 任一为真；属于 `pg_read_all_data`、`pg_read_all_stats`、`pg_read_all_settings`、`pg_stat_scan_tables`、`pg_monitor` 之外的任何 `pg_*` 预定义角色；对用户 schema 里任一表/视图/物化视图/外表有 `INSERT`、`UPDATE`、`DELETE`、`TRUNCATE`、`REFERENCES`、`TRIGGER`（17 起加 `MAINTAIN`）；对任一列有 `INSERT`、`UPDATE`、`REFERENCES`（列级授权，`has_any_column_privilege`；只查表级会漏）；对任一序列有 `UPDATE` 或 `USAGE`；对任一用户 schema 有 `CREATE`；对当前库有 `CREATE` 或是库的属主。默认给 PUBLIC 的 `TEMP` 不算（`CREATE TEMP` 被语句检查与只读事务拦）。另一问题码 `opspilot_schema`：库里任一 schema 有名为 `opspilot_runs` 的关系（`pg_class` 对所有账号可见）。
- MySQL/MariaDB 自查：`SHOW GRANTS` 每一行必须是 `GRANT <权限> ON <范围> TO <账号>`，权限 ⊆ {`USAGE`、`SELECT`（含列级）、`SHOW VIEW`、`PROCESS`、`SHOW DATABASES`、`REPLICATION CLIENT`、`BINLOG MONITOR`、`SLAVE MONITOR`、`REPLICA MONITOR`}，且不带 `WITH GRANT OPTION`；列级授权的权限名同样按白名单（`UPDATE (col)` 不通过）；角色授予、`PROXY`、`ALL PRIVILEGES`、一行里含 `;` 或多个 `GRANT` 与解析不了的行一律不通过。notes 只写命中的权限名与范围，不回显整行（MariaDB 的行可能带口令散列）。自查判定函数为模块级 `sql_query.session_problem(dialect, rows) -> str | None`（集成测试 ⑥ 用 monkeypatch 跳过）。
- 结果：账号问题报 `SQL_ACCOUNT_NOT_READ_ONLY`，`opspilot_schema` 报 `SQL_TARGETS_OPSPILOT_DB`，都归 `SOURCE_FORBIDDEN`，notes 开头写码；模型语句一条不发。
- 业务库隔离 ①（worker 启动时）：`OPSPILOT_SQL_URL` 的（主机, 端口）与进程环境里每个匹配 `^OPSPILOT_[A-Z0-9_]*DSN$` 的变量（URI 或 libpq 键值形式，用 `psycopg.conninfo.conninfo_to_dict` 解析；缺主机按 `PGHOST` 再按回环，缺端口按 `PGPORT` 再按 5432）比较；`localhost`、`127.0.0.0/8`、`::1`、Unix 套接字路径视为同一主机；SQL 端口缺省 Postgres 5432、MySQL 3306。相同则启动失败 `SQL_TARGETS_OPSPILOT_DB`。② 凭据只来自 `sql-ro` 槽，不读任何 DSN，不与业务库连接池共用对象。③ 即上面的 `opspilot_schema`（每次调用，Postgres）。

### 超时、上限与投影

- 单次调用 30 秒总期限。连接超时取剩余期限；库侧语句超时取剩余期限减 2 秒（至少 1 秒）。客户端兜底：Postgres 到期由看门狗调 `cancel_safe()`；MySQL 读超时取剩余期限，触发后另开一条连接 `KILL QUERY <connection_id>`（尽力）。库侧超时（Postgres 57014、MySQL 3024、MariaDB 1969）与客户端到期都报 `TOOL_TIMEOUT`。
- 值转 JSON：`None`/布尔/整数/有限浮点原样；`Decimal`、`UUID`、非有限浮点转字符串；日期时间转 ISO 8601；`timedelta` 转秒数字符串；字节转 `0x` 加十六进制；JSON/数组列转紧凑 JSON 字符串；其余 `str()`。所有字符串值截到 2000 字符，行内 `_truncated_fields` 列出被截的列。
- 原始读取按截断前每行 JSON 编码累计，超过 2 MiB 停读并整条 `RESULT_TOO_LARGE`；视图 25k token 超限整条拒绝。到 `limit` 标不完整，原因「已到 limit，结果集还有更多行」。
- `database_sql_query` 视图另带 `dialect`、`columns`、`limit`、`long_values_cut`、`notes`、`incomplete_reason`。`result_path=("results",)`，`source="otel-demo"`，`may_contain_secrets=True`（按列名脱敏，起别名或裸密钥认不出，PRODUCT-CONSTRAINTS:40 残余风险），地址在 selector `sql_endpoint`（不含凭据）。

### 错误分类（C6-14，工具自己的表）

| 情况 | 结果 |
|---|---|
| 发请求前：`INVALID_QUERY`、`INVALID_LIMIT`、`INVALID_IDENTIFIER`、`QUERY_CONSTRUCT_NOT_ALLOWED` | `INVALID_PARAMS`，码在 notes 开头 |
| Postgres 42xxx（除 42501）、22xxx；MySQL 1064、1146、1054、1052、1305、1630 一类语法/对象/数据错 | `INVALID_PARAMS`，回显截到 500 字符、脱敏、标为不可信的库错误文本 |
| 认证失败、无权限（Postgres 28xxx、42501；MySQL 1044、1045、1142、1143、1227） | `SOURCE_FORBIDDEN`，不回显 |
| 只读事务拒写（Postgres 25006、MySQL/MariaDB 1792） | `SOURCE_FORBIDDEN`，notes `SESSION_READ_ONLY` |
| `SQL_ACCOUNT_NOT_READ_ONLY`、`SQL_TARGETS_OPSPILOT_DB` | `SOURCE_FORBIDDEN` |
| 连不上、TLS 握手失败、连接中断（Postgres 08xxx、57P0x、53xxx；MySQL 2003、2006、2013 未到期） | `SOURCE_UNAVAILABLE` |
| 超时（见上） | `TOOL_TIMEOUT` |
| 其他驱动错误 | `SOURCE_ERROR` |
| 成功但 0 行 | `no_data` |

### 配置、凭据、lab 闸门（C6-7、C6-9，worker 读凭据）

- `OPSPILOT_SQL_URL`：`<scheme>://<host>[:<port>]/<database>`；带 userinfo、查询串、片段，缺库名或多段路径，报 `SQL_CONFIG_INVALID`。`OPSPILOT_SQL_USERNAME`、`OPSPILOT_SQL_PASSWORD` 必填（缺任一 `SQL_CONFIG_MISSING`）。`OPSPILOT_SQL_TLS` ∈ `verify-full`（默认：加密、校验证书链与主机名，CA 取 `OPSPILOT_SQL_CA_FILE`，否则系统信任库）、`require`（加密、不校验，同 libpq 语义）、`disable`；其他值、`disable` 时配 CA、没设地址却设了其余 `OPSPILOT_SQL_*`，报 `SQL_CONFIG_INVALID`。凭据槽 `sql-ro`，不与其他槽回退；凭据不进视图、notes、trace（trace 已按 `_PASSWORD` 后缀擦除）。
- web 只读 `OPSPILOT_SQL_URL` 取方言（不读凭据）；前缀不认识时 web 与 worker 都启动失败 `SQL_CONFIG_INVALID`。启动码 `SQL_CONFIG_MISSING`、`SQL_CONFIG_INVALID`、`SQL_TARGETS_OPSPILOT_DB` 进 `profiles.py` 启动码集合。入口脚本放行 `OPSPILOT_SQL_*`。
- lab 闸门：设了 `OPSPILOT_SQL_URL` 就必须是 lab 主机（回环、`localhost`、`*.svc`），否则 `SQL_NOT_LAB`。

### 扩展档位与 revision（C6-13）

- 三个工具接在当前所有后端之后（ES → Loki → VictoriaLogs → 实时用量 → 知识；#743 的 todo 若先合并则排在它之后），`data_sources.available.sql` 为三个名字排序。`tool_schema_revision(..., sql=None | "postgresql" | "mysql")`；`sql=None` 时所有现有值字节不变。新常量 `SQL_POSTGRESQL_TOOL_SCHEMA_REVISION`、`SQL_MYSQL_TOOL_SCHEMA_REVISION`、`SQL_TOOL_NAMES`；值实现后实测写进本记录。web 与 worker 方言不一致时 Run 在认领时被拦（同 B1/C5）。
- 模型可见说明（C6-15）：只改工具描述，不改调查提示词。写方言、只读语句类型、被拒的词与函数（含 `REPLACE()`）、带引号可绕开同名列、一条语句、建议 SQL 里自带 `LIMIT`、结果是查询时刻的数据；去掉上游「可经配置开启写」。

### 依赖（C6-12）

- Postgres 用已有 `psycopg[binary]==3.3.3`。MySQL/MariaDB 加 `PyMySQL==1.2.3`（纯 Python、MIT、2026-09-17 发布；1.2.4 发布不足两周不选）。理由：唯一主流纯 Python MySQL 驱动，上游同款，无原生扩展，MariaDB 兼容。

## CI（R8-25，C6-18 至 C6-21 的 C6a 部分）

- 新 job `db-backends`（`ubuntu-24.04`，`timeout-minutes: 15`，与 `search-backends`、`log-backends` 并行，同样的文档改动跳过逻辑）。`docker run` 起 `postgres:17.2-alpine`（`-c log_statement=all`，256m）、`mysql:8.0.40`、`mariadb:11.6.2`（`--general-log=1 --log-output=TABLE`，各 512m），镜像钉 digest，只绑 127.0.0.1，管理员口令用低熵测试串。不复用 OpsPilot 自己的 Postgres。测试由 `OPSPILOT_DB_BACKENDS=1` 门控，本机跳过。文件：测试作者 `tests/integration/test_tool_expansion_c6a_sql.py`，实现者 `..._impl.py`。
- 夹具用管理员账号建表灌合成数据（含 600 行表、5000 字符长值、`password`/`api_token` 列、视图、主外键与索引、另建一个含 `opspilot_runs` 表的库），按 C6-21 建 `opspilot_ro`；工具只用只读账号。
- 断言：① 三个工具对三种库都返回真实数据（视图、主键、外键、索引）；② `EXPLAIN`、`EXPLAIN ANALYZE SELECT` 返回计划；③ 600 行按 limit 截断并标不完整，长值截断，2 MiB 与视图超限整条拒；④ `password`/`api_token` 列脱敏，起别名的情况照实记录（残余风险证据，不断言）；⑤ 语句检查的代表语句在真实库上被拒，前后行数与表清单不变；⑥ 会话只读：monkeypatch 跳过 `sql_statement_problem` 与 `session_problem`、用管理员账号执行 `INSERT`，三库都被只读事务拒（`SOURCE_FORBIDDEN`/`SESSION_READ_ONLY`），行数不变；⑦ 身份层：绕开工具，只读账号直连 `INSERT`/`UPDATE`/`DELETE`/`CREATE`/`DROP` 全被权限错误拒，行数不变；⑧ 超时：低期限下跑重查询报 `TOOL_TIMEOUT`，之后管理员查 `pg_stat_activity`/`SHOW PROCESSLIST` 确认已无这条查询；⑨ C6-U1：管理员账号配给工具报 `SQL_ACCOUNT_NOT_READ_ONLY`，带标记注释的模型语句不出现在 Postgres 语句日志与 MySQL/MariaDB `general_log` 里；⑩ C6-8：`OPSPILOT_WORKER_DSN` 与工具地址同主机同端口时启动失败 `SQL_TARGETS_OPSPILOT_DB`；地址指向含 `opspilot_runs` 的库报同码；⑪ 错误分类用真实响应验证（语法错、表不存在、密码错、无权限的表、库不可达；MySQL 8 上 `require` 能连、`verify-full` 无 CA 报 `SOURCE_UNAVAILABLE`）。不调模型、不跑题。

## 实现决定（实现者自定，可逆，2026-11-20）

- MySQL 8 的默认认证插件 `caching_sha2_password` 在明文连接上要 `cryptography` 包做 RSA 交换；不加这个依赖，MySQL 8 须配 `OPSPILOT_SQL_TLS=require`（镜像首次启动自带自签证书）或 `verify-full` 加 CA；配 `disable` 时登录失败报 `SOURCE_UNAVAILABLE`。MariaDB 默认 `mysql_native_password`，`disable` 可用。理由：强制加密，不新增依赖。
- 产品代码禁用进程墙钟（`datetime.now`），视图不带 `read_at`；查询时刻即执行器记录的本次调用观察时间。
- 驱动错误由传输层分类：connector 与连接可以直接抛 psycopg（读 `.sqlstate`）或 PyMySQL（读错误号）的原始异常，也可以抛已分类的 `SqlDriverError(kind, code, message)`（`kind` ∈ `invalid`、`forbidden`、`read_only`、`unavailable`、`timeout`、`error`）。到期后的失败一律 `TOOL_TIMEOUT`；MySQL/MariaDB 到期由传输层另开一条连接发 `KILL QUERY <CONNECTION_ID()>`，Postgres 调连接的 `cancel()`。
- `OPSPILOT_SQL_CA_FILE` 必须是可读文件，否则 `SQL_CONFIG_INVALID`（同 Loki/VictoriaLogs 先例）。
- 结果体不按键排序，行对象的键保持结果集列序；重名列的后缀一直递增到没被占用（`a, a, a#2` → `a, a#2, a#2#2`），任何两列的键都不同（PR #746 审查 R1）。
- 语句检查再收紧（PR #746 审查，lead 定，全部 fail-closed）：字符串、注释、带引号标识符之外出现任何非 ASCII 字符一律 `QUERY_CONSTRUCT_NOT_ALLOWED`，两种方言都做（PG 允许 \200–\377 作标识符与 dollar tag 字符，词法器不认会错位，审查实测 5 条绕过，B1）；禁用函数加 `*_to_xml*` 族（`query_to_xml` 等会执行字符串里的 SQL）、`pg_try_advisory*`、`txid_current`、`pg_current_xact_id`、`WAIT_FOR_EXECUTED_GTID_SET`、`WAIT_UNTIL_SQL_THREAD_AFTER_GTIDS`；MySQL 方言拒绝优化器提示 `/*+ … */`（可覆盖 `MAX_EXECUTION_TIME`/`SET_VAR`）与 `:=` 赋值。
- 地址与 DSN 比对收紧：`OPSPILOT_SQL_URL` 带逗号（多主机）报 `SQL_CONFIG_INVALID`；DSN 同时给 `host` 与 `hostaddr` 时两者都参与比对；`127.1`、`2130706433` 一类 inet_aton 简写按其地址比对，`*.localhost`、`localhost.localdomain` 视为回环。
- 禁用函数再加 `ts_stat`、`ts_rewrite`（同样执行传入字符串里的 SQL；PR #746 复审）。
- 已知限制（不修，记录）：账号自查不把默认给 PUBLIC 的 `TEMP` 算作写权限（`CREATE TEMP` 由语句检查与只读事务拦）；PG 15 以前 `public` schema 默认对 PUBLIC 有 `CREATE`，自查会因此拒绝这类库上的账号（只会误拒）；`SECURITY DEFINER` 函数的 `EXECUTE` 不检查（函数内的写仍受只读事务约束，但经 dblink 一类的外连不受，dblink 由语句检查拦）；MySQL `mandatory_roles` 与 `activate_all_roles_on_login` 给的角色不出现在 `SHOW GRANTS`，自查看不到；反斜杠用法怪异的字符串按两种读法都检查，可能误拒合法语句（fail-closed）；DSN 写了多个主机和多个端口时按位置配对，端口个数与主机不一致时用最后一个端口补齐，个别写法可能与 libpq 的配对不同。
- DSN 比对读传入的环境映射与进程环境两处（只会扩大拒绝）；web 不调用它。refusal 码：库侧语法/对象错 `SQL_INVALID`（回显截短脱敏的库文本，`http_status=400` 走现有 `source_error` 通道）、`SQL_FORBIDDEN`、`SESSION_READ_ONLY`、`SQL_SOURCE_ERROR`、`TABLE_NOT_FOUND`，分别归 `INVALID_PARAMS`、`SOURCE_FORBIDDEN`、`SOURCE_FORBIDDEN`、`SOURCE_ERROR`、`INVALID_PARAMS`。
- MySQL/MariaDB 读到 `limit+1` 行或 2 MiB 后，另开一条连接 `KILL QUERY` 再丢弃剩余行；模型语句后的 `ROLLBACK` 尽力发送，失败不改变已得的结果。
- 扩展档位 revision：仅 Postgres `otel-demo-ext-be3a9913af82`，仅 MySQL/MariaDB `otel-demo-ext-ffed48334876`；不设 `OPSPILOT_SQL_URL` 时无后端 `c63f058afb18`、仅 ES `4f7faf728706` 等现有值不变（本分支实测）。
- CI 镜像 digest（2026-11-20 从 Docker Hub 读取）：`postgres@sha256:7e5df973…`、`mysql@sha256:d58ac933…`、`mariadb@sha256:bfb1298c…`。

## C6b 公开接口（ClickHouse、SQL Server，2026-11-20）

在 C6a 主体上加两个方言，工具名、参数、投影、上限、错误分类表与 C6a 相同，只列不同处。PG 与 MySQL/MariaDB 的行为、描述与 revision 字节不变（下面的新词、新函数只对新方言生效）。

### 方言与配置

- 前缀：`clickhouse+http://`、`clickhouse+https://` → `clickhouse`；`mssql://`、`mssql+pytds://` → `mssql`。地址规则同 C6a（单主机、一段库名、无 userinfo/查询串/片段）。缺省端口 ClickHouse 8123（https 8443）、SQL Server 1433，参与 C6-8 第一层比对。
- TLS：`clickhouse+http` 只许 `OPSPILOT_SQL_TLS=disable`，`clickhouse+https` 只许 `verify-full`（默认）或 `require`，不一致报 `SQL_CONFIG_INVALID`。SQL Server 在 C6b 只许 `disable`：驱动 python-tds 的 TLS 要 pyOpenSSL（连带 cryptography），不引入；租机上题目库经 port-forward 走回环、lab 闸门只放回环与 `*.svc`，残余风险是集群内这一跳与登录包不加密，写进记录。
- 依赖：SQL Server 加 `python-tds==1.17.1`（MIT，纯 Python，无必需依赖，2025-09-13 发布，上游同款）；ClickHouse 走标准库 HTTP，不加依赖。
- 凭据仍是 `sql-ro` 槽。ClickHouse 用请求头 `X-ClickHouse-User`、`X-ClickHouse-Key` 送凭据（不进 URL），不跟重定向、不走代理。
- revision：`tool_schema_revision(sql="clickhouse" | "mssql")` 两个新值，常量 `SQL_CLICKHOUSE_TOOL_SCHEMA_REVISION`、`SQL_MSSQL_TOOL_SCHEMA_REVISION`，实现后实测写进记录；`postgresql`、`mysql` 与未设地址的值不变。

### 会话只读这一层（各方言最接近的手段）

- ClickHouse：工具只发 HTTP GET。ClickHouse 对非 POST 请求在账号 `readonly=0` 时强制 `readonly=2`（源码 `src/Server/HTTPHandler.cpp`，v24.1 第 750–755 行），即只许读、可改除 `readonly` 外的设置；工具不发 `readonly` 参数（`readonly>0` 时改它会被拒，`SettingsConstraints.cpp:406`）。CI ⑥ 用管理员账号经工具发 `INSERT` 验证被 `READONLY`（164）拒。
- SQL Server：没有只读会话。`ApplicationIntent=ReadOnly`（pytds `readonly=True`）只在可用性组里把连接路由到只读副本，单实例上不起作用，不采用。工具改用回滚兜底：模型语句一律在 `BEGIN TRANSACTION` 里执行、结束一律 `ROLLBACK TRANSACTION`，并 `SET XACT_ABORT ON`、`SET IMPLICIT_TRANSACTIONS OFF`。这一层不阻止写入，只撤销事务性写入；非事务性副作用（扩展存储过程、发邮件、序列取号等）撤销不了，所以语句检查把这些构造全部拒掉，真正挡写的是账号与 C6-U1 自查。宣称里写明：SQL Server 的只读靠只读账号、工具自查与语句检查，回滚只是兜底。

### 语句检查（在 C6a 规则上，按方言）

- 非 ASCII 规则、一条语句、首词白名单、C6a 的禁用词与禁用函数照旧适用。
- ClickHouse 词法：`'…'`（反斜杠转义与 `''` 都认，只按反斜杠读法一遍，与服务器一致）、`"…"` 与 `` `…` `` 标识符（同样两种转义）；`--` 到行尾是注释；`/* … */` 不嵌套，注释里再出现 `/*` 拒绝；字面量之外出现 `#` 或 `$`（heredoc `$tag$…$tag$` 与 `#` 注释的写法不核实，fail-closed）拒绝。另拒整词：`SETTINGS`、`FORMAT`、`SYSTEM`、`KILL`、`OPTIMIZE`、`ATTACH`、`DETACH`、`RENAME`、`EXCHANGE`、`UNDROP`、`BACKUP`、`RESTORE`、`WATCH`、`MOVE`、`COMMIT`、`ROLLBACK`。另拒调用（表函数与副作用函数，不分大小写）：`url`、`urlCluster`、`file`、`fileCluster`、`s3`、`s3Cluster`、`gcs`、`azureBlobStorage`、`azureBlobStorageCluster`、`hdfs`、`hdfsCluster`、`remote`、`remoteSecure`、`cluster`、`clusterAllReplicas`、`mysql`、`postgresql`、`sqlite`、`mongodb`、`redis`、`odbc`、`jdbc`、`executable`、`input`、`iceberg`、`icebergS3`、`icebergAzure`、`icebergHDFS`、`deltaLake`、`hudi`、`sleep`、`sleepEachRow`。
- SQL Server 词法：`'…'` 与 `N'…'`（`''` 转义，不认反斜杠，只一遍）；`"…"`（`""` 转义）与 `[…]`（`]]` 转义）标识符；`--` 到行尾；`/* … */` 可嵌套（与 T-SQL 相同）。T-SQL 不需要 `;` 分隔多条语句，所以另拒整词：`DECLARE`、`EXEC`、`EXECUTE`（C6a 已有）、`WAITFOR`、`BEGIN`、`COMMIT`、`ROLLBACK`、`SAVE`、`GO`、`GOTO`、`USE`、`DBCC`、`KILL`、`SHUTDOWN`、`RECONFIGURE`、`CHECKPOINT`、`BACKUP`、`RESTORE`、`BULK`、`DENY`、`ENABLE`、`DISABLE`、`WRITETEXT`、`UPDATETEXT`、`READTEXT`、`RAISERROR`、`THROW`、`REVERT`、`SETUSER`、`OPENXML`，以及词序列 `NEXT VALUE`（取序列号）。函数照 C6a（`xp_*`、`sp_*`、`OPENROWSET`、`OPENQUERY`、`OPENDATASOURCE`）。首词仍是 C6a 白名单（`SHOW`、`DESCRIBE`、`EXPLAIN` 在 SQL Server 上由服务器报语法错）。
- 拒绝码同 C6a（`QUERY_CONSTRUCT_NOT_ALLOWED`、`INVALID_QUERY`），notes 写命中的词。

### 每次调用的请求序列

- ClickHouse（每一步是一个独立的 GET，无会话、无事务）：① `/* opspilot:account-check */ SHOW GRANTS`；② `/* opspilot:account-check */ SELECT getSetting('readonly'), version()`；③ 模型语句或目录查询（`/* opspilot:catalog */`，参数用 ClickHouse 查询参数 `{p0:String}` 加 URL 参数 `param_p0`，不拼字符串）。每个 GET 的查询键固定为：`query`、`database`（配置的库）、`default_format=JSONCompactEachRowWithNames`、`max_execution_time`（剩余期限减 2 秒，整数秒，至少 1）、`timeout_overflow_mode=throw`、`cancel_http_readonly_queries_on_client_close=1`、`output_format_json_quote_64bit_integers=0`、`query_id=opspilot-<uuid4>`，以及目录查询的 `param_*`；不发 `readonly`。结果按行流式读，第一行是列名数组，之后每行一个值数组；读够 `limit+1` 行或 2 MiB 即关连接（服务器因 `cancel_http_readonly_queries_on_client_close` 取消查询）；流中途出现非 JSON 行（ClickHouse 把异常文本写进 200 的正文）按错误处理。
- SQL Server（pytds，`autocommit=True`、`appname=opspilot-sql-tool`、`login_timeout` 取剩余期限、`timeout` 取剩余期限减 2 秒、不传 `cafile`）：`SET LOCK_TIMEOUT <ms>` → `SET XACT_ABORT ON` → `SET IMPLICIT_TRANSACTIONS OFF` → 自查（`/* opspilot:account-check */`）→ `BEGIN TRANSACTION` → 模型语句或目录查询 → `ROLLBACK TRANSACTION` → 关连接。只读第一个结果集。到期由看门狗调连接的 `cancel()`（pytds 发 attention 取消）。
- 驱动接缝不变：`SqlTransport(config, *, connector=None, request_timeout_seconds=30.0)`，替身 connector 看到上面每一条语句（ClickHouse 每个 GET 是一次 `query`）。ClickHouse 的真实连接另可用本地 `http.server` 替身按上面的 HTTP 形状测试。SQL Server 驱动原始异常由传输层分类（读消息号），ClickHouse 连接自己抛 `SqlDriverError`。

### 账号自查（C6-U1，按方言写死）

- ClickHouse：`SHOW GRANTS` 每一行必须是 `GRANT <权限> ON <范围> TO <账号>`，权限 ⊆ {`SELECT`（含列级）、`SHOW`、`SHOW DATABASES`、`SHOW TABLES`、`SHOW COLUMNS`、`SHOW DICTIONARIES`}，不带 `WITH GRANT OPTION`；角色授予、`ALL`、`REVOKE` 行、含 `;` 或多个 `GRANT` 的行与解析不了的行一律不通过（`default` 账号是 `GRANT ALL ON *.* … WITH GRANT OPTION`，必拒）。`getSetting('readonly')` 为 `1` 时报 `SQL_ACCOUNT_UNSUPPORTED`（新码，归 `SOURCE_FORBIDDEN`，notes：账号的 `readonly=1` 会挡住工具自己的超时设置，请改用 `readonly=2` 或不设）；为 `2`（即账号 `0` 或 `2`）通过。
- SQL Server：一条查询，每行一个问题码，0 行通过：`IS_SRVROLEMEMBER` 属于 `sysadmin`、`serveradmin`、`securityadmin`、`processadmin`、`setupadmin`、`bulkadmin`、`diskadmin`、`dbcreator` 任一；`IS_ROLEMEMBER` 属于 `db_owner`、`db_datawriter`、`db_ddladmin`、`db_securityadmin`、`db_accessadmin`、`db_backupoperator` 任一；`fn_my_permissions(NULL, 'SERVER')` 里有 `CONNECT SQL`、`VIEW ANY DATABASE` 与 `VIEW %` 之外的权限；`fn_my_permissions(NULL, 'DATABASE')` 里有 `CONNECT`、`SELECT`、`SHOWPLAN` 与 `VIEW %` 之外的权限；`sys.database_permissions` 里授给本用户或其所属角色（`IS_MEMBER`）、状态为 G/W、权限不在上述白名单的任一行（含列级与对象级）；本用户或其所属角色拥有任一 schema。命中报 `SQL_ACCOUNT_NOT_READ_ONLY`，模型语句不发。白名单名称以 CI 实测为准，只能收窄不能放宽。

### 目录查询

- ClickHouse：表 `system.tables`（`database = {p0:String}`，缺省 `currentDatabase()`；`engine` 含 `View` 的为 `view`，`MaterializedView` 为 `materialized_view`，其余 `table`）；列 `system.columns`（`nullable` 看类型是否 `Nullable(…)`，`default` 取 `default_expression`），`primary_key` 取 `is_in_primary_key` 的列；`foreign_keys` 为空；`indexes` 为 `system.data_skipping_indices` 各行加一项排序键 `{name: "sorting_key", columns, unique: false, primary: true, definition: <sorting_key>}`。
- SQL Server：`sys.objects`/`sys.schemas`（`U` 为 table、`V` 为 view），缺省 schema 为 `SCHEMA_NAME()`；列 `sys.columns` + `TYPE_NAME`、`sys.default_constraints`；主键与索引 `sys.indexes`/`sys.index_columns`（`definition` 为 `null`）；外键 `sys.foreign_keys`/`sys.foreign_key_columns`。
- 执行计划：ClickHouse 用 `EXPLAIN`/`EXPLAIN PIPELINE`；SQL Server 没有 `EXPLAIN`、`SET SHOWPLAN_*` 被拒，描述里引导用 `sys.dm_exec_query_stats`、`sys.dm_exec_query_plan` 等 DMV（需要 `VIEW SERVER STATE`/`VIEW DATABASE STATE`）。描述里写 SQL Server 用 `TOP` 而非 `LIMIT`。

### 错误分类（补 C6a 表）

| 情况 | 结果 |
|---|---|
| ClickHouse 异常码 62、60、47、46、81、16、43、53、42、6、27、38、41、69、70、72 一类语法/对象/类型错；SQL Server 消息号 102、156、170、207、208、4104、8134、245、8114 | `INVALID_PARAMS`（`SQL_INVALID`，回显截短脱敏的库文本） |
| ClickHouse 497、516、192、194 与 HTTP 401/403；SQL Server 229、230、262、297、300、18456 | `SOURCE_FORBIDDEN`（`SQL_FORBIDDEN`，不回显） |
| ClickHouse 164（READONLY） | `SOURCE_FORBIDDEN`（`SESSION_READ_ONLY`） |
| ClickHouse 159、394；SQL Server 1222（锁超时）与驱动超时；任一方言到期后的失败 | `TOOL_TIMEOUT` |
| ClickHouse 241、202、210 与 HTTP 5xx、连不上；SQL Server 连接断开或登录前失败（18456 除外） | `SOURCE_UNAVAILABLE` |
| 其他 | `SOURCE_ERROR` |

### CI（写死，测试作者与实现者共用）

- 同一个 job `db-backends`，在 C6a 三个容器之外加：
  - `c6-clickhouse`：`clickhouse/clickhouse-server@sha256:360a13d58afe559036b13ae5e21ba361d990081c93987d3fef0e5d7ea2dcd074`（24.1-alpine 多架构索引，2026-11-20 读取），`--cpus=1 --memory=1g --ulimit nofile=262144:262144`，`-e CLICKHOUSE_PASSWORD=$C6_ADMIN_PASSWORD -e CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT=1`，`-p 127.0.0.1:53204:8123`；就绪看 `GET /ping`。管理员是 `default`，口令 `C6_ADMIN_PASSWORD`；`log_queries` 用镜像默认（开）。
  - `c6-mssql`：`mcr.microsoft.com/mssql/server@sha256:4525e3efac9f3e241cb003bb2ff2a82818b502f37e12049d788b1a9fa7e13b7f`（2022-latest，只有 amd64，2026-11-20 读取），`--cpus=2 --memory=2560m`，`-e ACCEPT_EULA=Y -e MSSQL_PID=Developer -e MSSQL_SA_PASSWORD=$C6_MSSQL_ADMIN_PASSWORD`，`-p 127.0.0.1:53205:1433`；就绪看 `docker exec c6-mssql /opt/mssql-tools18/bin/sqlcmd -C -No -S localhost -U sa -P "$C6_MSSQL_ADMIN_PASSWORD" -Q "SELECT 1"`，最长等 180 秒。
- pytest 步骤新增环境变量（C6a 的全部保留）：`C6_CLICKHOUSE_PORT=53204`、`C6_MSSQL_PORT=53205`、`C6_CLICKHOUSE_ADMIN_USER=default`（口令仍是 `C6_ADMIN_PASSWORD`）、`C6_MSSQL_ADMIN_USER=sa`、`C6_MSSQL_ADMIN_PASSWORD=Changeme570!`（SQL Server 口令复杂度要求，CI 专用低熵串）、`C6_CLICKHOUSE_CONTAINER=c6-clickhouse`、`C6_MSSQL_CONTAINER=c6-mssql`。门控仍是 `OPSPILOT_DB_BACKENDS=1`。
- 文件与命名：测试作者 `tests/integration/test_tool_expansion_c6b_sql.py`，用库 `c6b_tests`、只读账号 `c6b_tests_ro`、其他账号前缀 `c6b_tests_`；实现者 `tests/integration/test_tool_expansion_c6b_sql_impl.py`，用 `c6b_impl`、`c6b_impl_ro`、前缀 `c6b_impl_`。运行顺序：C6a 两个文件之后，先测试作者、后实现者，同一个 pytest 步骤。工具侧 TLS：ClickHouse 用 `clickhouse+http://127.0.0.1:53204/<库>` 加 `OPSPILOT_SQL_TLS=disable`；SQL Server 用 `mssql://127.0.0.1:53205/<库>` 加 `disable`。工程师侧 SQL Server 连接用 pytds（实现提交后已在依赖里），不传 `cafile`；ClickHouse 工程师侧用 POST（写入、建账号）。
- 只读账号授权（C6-21，CI 夹具与租机共用）：ClickHouse `CREATE USER <ro> IDENTIFIED WITH sha256_password BY '<口令>' SETTINGS readonly = 2`、`GRANT SELECT ON <库>.* TO <ro>`、`GRANT SELECT ON system.* TO <ro>`（读 `system.query_log`）。SQL Server `CREATE LOGIN <ro> WITH PASSWORD = '<口令>', CHECK_POLICY = OFF`、库内 `CREATE USER <ro> FOR LOGIN <ro>`、`ALTER ROLE db_datareader ADD MEMBER <ro>`、`GRANT SHOWPLAN TO <ro>`、`GRANT VIEW DATABASE STATE TO <ro>`，服务器级 `GRANT VIEW SERVER PERFORMANCE STATE TO <ro>`（2022 起；不够再加 `VIEW SERVER STATE`）。
- 语句是否到达服务器（⑨）：ClickHouse 管理员 `SYSTEM FLUSH LOGS` 后查 `system.query_log` 的 `query` 含标记；SQL Server 查 `sys.dm_exec_query_stats` CROSS APPLY `sys.dm_exec_sql_text` 含标记（计划缓存，【CI 实测】是否缓存这类即席语句；不缓存则改用 `sys.dm_exec_requests` 加长语句的在途观察，测试作者与实现者按实测一起改）。
- 断言（与 C6a ①–⑪ 对应）：① 三个工具在两库都有真实返回；② ClickHouse `EXPLAIN` 有返回，SQL Server 用 DMV 查到计划；③ 截断、长值、2 MiB 与视图超限；④ 按列名脱敏；⑤ 语句检查代表语句（含 ClickHouse 表函数、`SETTINGS`、`FORMAT`，SQL Server `EXEC`、`WAITFOR`、`DECLARE`、`SELECT … INTO`、`BEGIN`/`COMMIT`）在真实库上被拒、不到达服务器；⑥ 会话层：ClickHouse 管理员经工具（跳过语句检查与自查）`INSERT` 被 164 拒、行数不变；SQL Server 管理员经工具 `INSERT` 后行数不变（回滚兜底），结果不是 `ok` 带数据；⑦ 只读账号直连写入被拒（ClickHouse 用 POST 也拒、SQL Server 拒）；⑧ 超时：重查询报 `TOOL_TIMEOUT`，之后 `system.processes` 与 `sys.dm_exec_requests` 里没有这条；⑨ 管理员账号（ClickHouse `default`、SQL Server `sa`）报 `SQL_ACCOUNT_NOT_READ_ONLY` 且模型语句未到达；ClickHouse 账号 `readonly=1` 报 `SQL_ACCOUNT_UNSUPPORTED`；SQL Server 只授 `db_datawriter`、列级 `UPDATE`、拥有 schema 的账号都被拒；⑩ `OPSPILOT_WORKER_DSN` 同主机同端口时启动失败；⑪ 错误分类用真实响应（语法错、表不存在、密码错、无权限的表、端口无人监听）。

### 实现决定（C6b，实现者自定，可逆，2026-11-20）

- 扩展档位 revision：仅 ClickHouse `otel-demo-ext-a24b3b7f64ab`，仅 SQL Server `otel-demo-ext-3955b33beb0d`；仅 Postgres `be3a9913af82`、仅 MySQL `ffed48334876` 与未设地址的值不变（本分支实测）。`SQL_ACCOUNT_UNSUPPORTED` 只进 ClickHouse 那三个注册的错误表，C6a 注册字节不变。
- ClickHouse 连接用标准库 `http.client`（不读代理环境变量、不跟重定向），到期由看门狗关掉套接字（`cancel()` 对正在读的连接 `shutdown`）；`clickhouse+https` 的 `require` 用不校验证书的 SSL 上下文。连接自己把 HTTP 状态与 `X-ClickHouse-Exception-Code`（没有时取正文里的 `Code: N`）分类成 `SqlDriverError`。
- SQL Server 连接用 python-tds `autocommit=True`、`disable_connect_retry=True`；没有参数时传 `None`（不是 `()`），否则 python-tds 会对模型 SQL 里的 `%` 做格式化。驱动原始异常由传输层按消息号分类；裸的 `TimeoutError`（驱动查询超时）归 `TOOL_TIMEOUT`。读到 `limit+1` 行后调 `cursor.cancel()` 再 `ROLLBACK TRANSACTION`。
- 到期后任何来源的 `unavailable`/`error` 一律改记 `TOOL_TIMEOUT`（看门狗取消后连接报出的断开属于超时）。
- 本仓库 C6a 合同测试 `test_web_refuses_an_unknown_prefix` 的 `clickhouse+http`、`mssql` 两例按 C6a 范围写，C6b 之后这两个前缀合法，由测试作者按 C6b 接口改（实现者不改断言）。

### 交给 #669

- 222（ClickHouse）：题目自带 `default` 无口令账号是管理员，会被 C6-U1 拒；评测脚本按上面的 C6-21 用 `kubectl exec … clickhouse-client` 建只读账号，地址 `clickhouse+http://127.0.0.1:10222/default`、`OPSPILOT_SQL_TLS=disable`。
- 223（SQL Server）：题目用 `sa`，同样被拒；用 `sqlcmd` 建只读登录与库用户，地址 `mssql://127.0.0.1:10223/testdb`、`disable`；租机选 amd64、给 3 GB。

## 交给 #669 租机

建只读账号与切换配置按预审「交给 #669」节；方言变了要同时重启 web 与 worker。

## 证据

- 本机：实现者单测 `tests/test_sql_query.py` 81 passed；B1、C5a、C5b、B3、C2b 合同测试与相邻单测 1204 passed（revision 钉值不变）；ruff、mypy 干净。真实库集成测试交 CI `db-backends`。

## 待决

无。实施中再冒出的不可逆项或合同层取舍追加到 r11。
