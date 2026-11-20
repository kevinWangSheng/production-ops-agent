# 工具面拓展 C6：数据库只读查询

- 状态：进行中（C6a 实现与本机单测完成，等测试作者提交与 CI；C6b ClickHouse/SQL Server、C6c MongoDB 另开）
- 更新日期：2026-11-20
- 依据：[B/C 批次记录](2026-11-16-tool-expansion-bc.md)、[决定表](2026-11-12-tool-expansion-contract.md) R8-9、R8-15、R8-16、R8-25 与 r11（C6-U1=a；C6-0 至 C6-22 可逆默认全部采纳）；PRODUCT-CONSTRAINTS 第 18 行（模型自写查询只在注册的只读工具里跑，SQL 以只读账号为第一道、语句检查为第二道）；C3 第 3、8 节；上游 HolmesGPT `a0009195` `holmes/plugins/toolsets/database/`。issue #570。预审全文在仓库外（`~/.agent/handoffs/opspilot-c6-prereview-2026-11-19.md`），细节以它和 r11 为准
- 工作区：C6a 实现 `../poa-570-sql`，分支 `feature/570-sql-a`（起点 `75dfb490`）

## 目标

调查能对一个配置好的 SQL 数据库执行只读查询、列出表、看表结构（C6a：PostgreSQL、MySQL、MariaDB）。工具只进扩展档位 `otel-demo-ext`，且只有设了 `OPSPILOT_SQL_URL` 时才出现；未设时 `otel-demo` 与扩展档位所有现有 revision 字节不变。只读由工具保证，四层：只读账号（第一道）、语句检查（第二道）、会话只读事务、每次连接后工具自查账号权限（C6-U1）。

## 行为（公开接口，C6a）

| 工具 | 参数（必填加粗） | 做什么 | 返回 `results` 每行 | `row_unit` | verb |
|---|---|---|---|---|---|
| `database_sql_query` | **`sql`**（字符串）、`limit`（整数，默认 100，1–500） | 原样执行模型的一条只读语句，最多取 `limit+1` 行判断截断；工具不改写 SQL | `{列名: 值}`，列顺序同结果集；重名列第 2 个起加 `#2`、`#3` 后缀 | `rows` | `query` |
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
- 结果体不按键排序，行对象的键保持结果集列序。
- DSN 比对读传入的环境映射与进程环境两处（只会扩大拒绝）；web 不调用它。refusal 码：库侧语法/对象错 `SQL_INVALID`（回显截短脱敏的库文本，`http_status=400` 走现有 `source_error` 通道）、`SQL_FORBIDDEN`、`SESSION_READ_ONLY`、`SQL_SOURCE_ERROR`、`TABLE_NOT_FOUND`，分别归 `INVALID_PARAMS`、`SOURCE_FORBIDDEN`、`SOURCE_FORBIDDEN`、`SOURCE_ERROR`、`INVALID_PARAMS`。
- MySQL/MariaDB 读到 `limit+1` 行或 2 MiB 后，另开一条连接 `KILL QUERY` 再丢弃剩余行；模型语句后的 `ROLLBACK` 尽力发送，失败不改变已得的结果。
- 扩展档位 revision：仅 Postgres `otel-demo-ext-be3a9913af82`，仅 MySQL/MariaDB `otel-demo-ext-ffed48334876`；不设 `OPSPILOT_SQL_URL` 时无后端 `c63f058afb18`、仅 ES `4f7faf728706` 等现有值不变（本分支实测）。
- CI 镜像 digest（2026-11-20 从 Docker Hub 读取）：`postgres@sha256:7e5df973…`、`mysql@sha256:d58ac933…`、`mariadb@sha256:bfb1298c…`。

## 交给 #669 租机

建只读账号与切换配置按预审「交给 #669」节；方言变了要同时重启 web 与 worker。

## 证据

- 本机：实现者单测 `tests/test_sql_query.py` 81 passed；B1、C5a、C5b、B3、C2b 合同测试与相邻单测 1204 passed（revision 钉值不变）；ruff、mypy 干净。真实库集成测试交 CI `db-backends`。

## 待决

无。实施中再冒出的不可逆项或合同层取舍追加到 r11。
