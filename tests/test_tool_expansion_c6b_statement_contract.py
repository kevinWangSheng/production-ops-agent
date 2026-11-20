"""Independent contract tests for the C6b statement check (C6-3 per dialect,
the second line of PRODUCT-CONSTRAINTS line 18) for ClickHouse and SQL
Server: ``sql_query.sql_statement_problem`` and the same refusals through
``database_sql_query``.

Written from the C6 task record (「C6b 公开接口」「语句检查（在 C6a 规则上，按
方言）」), without reading the implementation. Neither dialect has a full
read-only session, so this deny list matters more than in C6a: every
refused statement must come back ``INVALID_PARAMS`` with
``QUERY_CONSTRUCT_NOT_ALLOWED`` or ``INVALID_QUERY`` first in notes while the
recording connector receives nothing; every accepted statement reaches it
verbatim, once.

The bypass cases follow each dialect's real lexer as the record fixes it:

* ClickHouse: ``'...'`` with backslash escapes and ``''`` (one reading, the
  server's), ``"..."``/backticks likewise, ``--`` always a comment,
  ``/* */`` not nested (a second ``/*`` inside refuses), ``#`` and ``$``
  outside literals refuse; extra words (SETTINGS, FORMAT, SYSTEM, KILL, ...)
  and every listed table/side-effect function.
* SQL Server: ``'...'``/``N'...'`` with ``''`` only (backslash is literal),
  ``"..."``/``[...]`` identifiers, nested ``/* */``; T-SQL needs no ``;`` to
  start a second statement, so DECLARE, WAITFOR, EXEC, GO, USE, ... refuse
  anywhere, and ``NEXT VALUE``.
* PostgreSQL and MySQL keep their C6a verdicts for the new words.
"""

from __future__ import annotations

import os
import re

import pytest

from tests.c6a_sql_support import make_tools, notes, refused_before_request, view
from tests.c6b_sql_support import make_tools_b
from tests.test_tool_expansion_c6a_statement_contract import (
    REFUSED_FUNCTIONS as C6A_REFUSED_FUNCTIONS,
)
from tests.test_tool_expansion_c6a_statement_contract import (
    REFUSED_INVALID as C6A_REFUSED_INVALID,
)
from tests.test_tool_expansion_c6a_statement_contract import (
    REFUSED_MULTI as C6A_REFUSED_MULTI,
)
from tests.test_tool_expansion_c6a_statement_contract import (
    REFUSED_WORDS as C6A_REFUSED_WORDS,
)

CONSTRUCT = "QUERY_CONSTRUCT_NOT_ALLOWED"
INVALID = "INVALID_QUERY"
EITHER = (CONSTRUCT, INVALID)
CH, MS = "clickhouse", "mssql"
NEW = (CH, MS)


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    for name in list(os.environ):
        if name.startswith("OPSPILOT_SQL") or re.fullmatch(
            r"OPSPILOT_[A-Z0-9_]*DSN", name
        ):
            monkeypatch.delenv(name, raising=False)
    ca_file = tmp_path / "synthetic-k8s-ca.pem"
    ca_file.write_text("synthetic ca placeholder\n")
    monkeypatch.setenv("C6A_TEST_K8S_CA_FILE", str(ca_file))


def _problem(sql: str, dialect: str):
    from opspilot.tools import sql_query

    return sql_query.sql_statement_problem(sql, dialect)


# == ClickHouse ======================================================================

CH_TABLE_FUNCTIONS = [
    "url", "urlCluster", "file", "fileCluster", "s3", "s3Cluster", "gcs",
    "azureBlobStorage", "azureBlobStorageCluster", "hdfs", "hdfsCluster",
    "remote", "remoteSecure", "cluster", "clusterAllReplicas", "mysql",
    "postgresql", "sqlite", "mongodb", "redis", "odbc", "jdbc", "executable",
    "input", "iceberg", "icebergS3", "icebergAzure", "icebergHDFS",
    "deltaLake", "hudi",
]  # fmt: skip


def _function_variants(name: str) -> list[str]:
    """Each listed function in several spellings and positions."""
    return [
        f"SELECT * FROM {name}('http://10.0.0.1/x', CSV)",
        f"SELECT * FROM {name.upper()}('x')",
        f"SELECT * FROM {name.lower()} ('x')",
        f"SELECT * FROM {name[0].upper() + name[1:]}\n\t('x')",
        f"SELECT * FROM {name}/* gap */('x')",
        f"SELECT count() FROM t JOIN {name}('x') AS r ON t.id = r.id",
        f"SELECT 1 WHERE 1 IN (SELECT * FROM {name}('x'))",
        f"WITH r AS (SELECT * FROM {name}('x')) SELECT * FROM r",
        f"EXPLAIN SELECT * FROM {name}('x')",
        f"DESCRIBE TABLE {name}('x')",
        f"SELECT * FROM `{name}`('x')",
        f"SELECT * FROM \"{name}\"('x')",
        f"SELECT * FROM default.{name}('x')",
    ]


CH_REFUSED_FUNCTIONS = [
    sql for name in CH_TABLE_FUNCTIONS for sql in _function_variants(name)
] + [
    "SELECT sleep(3)",
    "SELECT SLEEP(3)",
    "SELECT sleep (3)",
    "SELECT sleepEachRow(1) FROM numbers(10)",
    "SELECT SLEEPEACHROW(1) FROM numbers(10)",
    "SELECT number FROM numbers(10) WHERE sleepEachRow(0.5) = 0",
    "SELECT `sleep`(3)",
    'SELECT "sleepEachRow"(1)',
    # C6a's list still applies.
    "SELECT pg_sleep(1)",
    "SELECT xp_cmdshell('x')",
]

CH_REFUSED_WORDS = [
    "SELECT 1 SETTINGS max_execution_time = 0",
    "SELECT 1 settings readonly = 0",
    "SELECT * FROM t SeTtInGs max_threads = 64",
    "SELECT 1\nSETTINGS\tmax_memory_usage = 0",
    "SELECT 1 /* x */ SETTINGS max_execution_time = 0",
    "WITH x AS (SELECT 1 SETTINGS max_execution_time = 0) SELECT * FROM x",
    "EXPLAIN SELECT 1 SETTINGS max_execution_time = 0",
    "EXPLAIN PIPELINE SELECT 1 SETTINGS max_threads = 1",
    "SELECT 1 FORMAT TabSeparated",
    "SELECT 1 format JSON",
    "SHOW TABLES FORMAT JSON",
    "SELECT * FROM format(JSONEachRow, '{\"a\":1}')",
    "SYSTEM FLUSH LOGS",
    "SYSTEM DROP DNS CACHE",
    "SELECT 1 SYSTEM SHUTDOWN",
    "KILL QUERY WHERE 1",
    "OPTIMIZE TABLE t FINAL",
    "ATTACH TABLE t",
    "DETACH TABLE t",
    "RENAME TABLE a TO b",
    "EXCHANGE TABLES a AND b",
    "UNDROP TABLE t",
    "BACKUP TABLE t TO Disk('backups', 'x.zip')",
    "RESTORE TABLE t FROM Disk('backups', 'x.zip')",
    "WATCH lv",
    "MOVE USER u TO local_directory",
    "COMMIT",
    "ROLLBACK",
    "SELECT 1 COMMIT",
    "SELECT 1 ROLLBACK",
    "INSERT INTO FUNCTION file('x.csv') SELECT 1",
    "ALTER TABLE t DELETE WHERE 1",
    "ALTER TABLE t UPDATE a = 1 WHERE 1",
    "SET readonly = 0",
    "SELECT 1 INTO OUTFILE 'x.csv'",
    "SELECT 1 INTO OUTFILE 'x.csv' TRUNCATE",
    "CREATE TEMPORARY TABLE x (a UInt8)",
    "SHOW CREATE TABLE t",
    "EXISTS TABLE t",
    "USE other",
    # Documented false refusals: system tables and settings are whole words.
    "SELECT * FROM system.tables",
    "SELECT * FROM system.settings WHERE name = 'readonly'",
    "SELECT name, value FROM `system`.settings",
]

CH_REFUSED_LEXICAL = [
    # The server reads \' as an escaped quote: the string ends one quote
    # later and SETTINGS is live (a ''-only lexer would hide it).
    (r"SELECT 'a\'' SETTINGS max_execution_time = 0 --'", EITHER),
    (r"SELECT 'a\\' SETTINGS max_execution_time = 0 --'", EITHER),
    (r"SELECT 'x\'' , * FROM url('http://10.0.0.1/', CSV) --'", EITHER),
    (r'SELECT "a\"" SETTINGS max_threads = 1 --"', EITHER),
    (r"SELECT `a\`` SETTINGS max_threads = 1 --`", EITHER),
    ("SELECT 'a'' SETTINGS x = 1 --", EITHER),  # unterminated
    # Comments: -- to end of line only; /* */ does not nest here.
    ("SELECT 1 -- x\nSETTINGS max_execution_time = 0", EITHER),
    ("SELECT 1 /* a /* b */", EITHER),
    ("SELECT 1 /* outer /* inner */ SETTINGS max_threads = 1 */", EITHER),
    ("SELECT 1 /* /* */ */", EITHER),
    ("SELECT 1 /* never closed", EITHER),
    # # and $ outside literals (heredoc and # comments are not verified).
    ("SELECT 1 # SETTINGS max_threads = 1", EITHER),
    ("SELECT 1 #\nSETTINGS max_threads = 1", EITHER),
    ("SELECT 1 #!", EITHER),
    ("SELECT $$SETTINGS$$", EITHER),
    ("SELECT $tag$ SETTINGS $tag$", EITHER),
    ("SELECT $heredoc$x$heredoc$ AS s", EITHER),
    ("SELECT a$b FROM t", EITHER),
    ("SELECT 1 AS $x", EITHER),
    # Unterminated literals and identifiers.
    ("SELECT 'abc", EITHER),
    ('SELECT "abc', EITHER),
    ("SELECT `abc", EITHER),
    # Non-ASCII outside literals; fullwidth lookalikes of SETTINGS.
    ("SELECT 1 AS é", (CONSTRUCT,)),
    ("SELECT 1 ＳＥＴＴＩＮＧＳ max_threads = 1", (CONSTRUCT,)),
    ("SELECT 1 SETTİNGS x = 1", (CONSTRUCT,)),
    ("SELECT ur\u0142('x')", (CONSTRUCT,)),
    # Control characters and length (C6a rule 1).
    ("SELECT 1\x00", (INVALID,)),
    ("SELECT\u00a01", (INVALID,)),
    ("SELECT 1\u200b", (INVALID,)),
    # One statement only.
    ("SELECT 1; SELECT 2", (CONSTRUCT,)),
    ("SELECT 1;;", (CONSTRUCT,)),
    ("SELECT 1; SYSTEM FLUSH LOGS", (CONSTRUCT,)),
]

CH_ACCEPTED = [
    "SELECT 1",
    "SELECT 1;",
    "select count() from numbers(10)",
    "SELECT * FROM numbers(3)",
    "SELECT number FROM numbers(10) LIMIT 3",
    "SELECT 'SETTINGS readonly = 0' AS s",
    "SELECT 'FORMAT JSON; SYSTEM KILL' AS s",
    "SELECT 'url(x) file(y) sleep(1)' AS s",
    r"SELECT 'it\'s' AS s",  # one reading: the server's backslash escape
    r"SELECT 'a\\' AS s",
    r"SELECT 'C:\\tmp\\x' AS p",
    "SELECT 'it''s' AS s",
    "SELECT '#$' AS s",
    'SELECT "settings", "format", "system" FROM t',
    "SELECT `settings`, `kill`, `move` FROM t",
    'SELECT "a""b" FROM t',
    "SELECT `a``b` FROM t",
    r'SELECT "a\"b" FROM t',
    "SELECT name FROM `system`.`tables` WHERE database = currentDatabase()",
    "SELECT query FROM `system`.query_log LIMIT 5",
    "SELECT 1 -- SETTINGS max_threads = 1",
    "SELECT 1 --SETTINGS",
    "SELECT 1 /* SETTINGS FORMAT */",
    "-- note\nSELECT 1",
    "SELECT url, file, s3, remote, cluster, input FROM t",
    "SELECT domain(url), path(url) FROM t",
    "SELECT cutURLParameter(url, 'x') FROM t",
    "SELECT urls, file_name, sleep_ms, input_rows, cluster_name FROM t",
    "SELECT formatDateTime(now(), '%Y'), formatReadableSize(1024)",
    "SELECT arrayMap(x -> x * 2, [1, 2, 3]) AS a",
    "SELECT toStartOfHour(ts) AS h, count() FROM t GROUP BY h ORDER BY h",
    "SELECT * FROM t FINAL WHERE id = 1",
    "SELECT * FROM t PREWHERE id = 1",
    "SELECT 'é中文' AS s",
    'SELECT "é" FROM t',
    "EXPLAIN SELECT 1",
    "EXPLAIN PIPELINE SELECT count() FROM numbers(10)",
    "EXPLAIN PLAN SELECT 1",
    "EXPLAIN SYNTAX SELECT 1",
    "EXPLAIN AST SELECT 1",
    "SHOW TABLES",
    "SHOW DATABASES",
    "SHOW PROCESSLIST",
    "DESCRIBE TABLE t",
    "DESC t",
    "WITH a AS (SELECT 1 AS n) SELECT n FROM a",
    "(SELECT 1) UNION ALL (SELECT 2)",
]


# == SQL Server =====================================================================

MS_REFUSED_WORDS = [
    # A second statement without a semicolon (T-SQL).
    "SELECT 1 DECLARE @x int",
    "DECLARE @x int = 1",
    "SELECT 1 WAITFOR DELAY '00:00:10'",
    "SELECT 1 waitfor delay '00:00:10'",
    "SELECT 1 WAITFOR TIME '23:59'",
    "SELECT 1 WaItFoR DELAY '0:0:5'",
    "WAITFOR DELAY '00:00:10'",
    "SELECT 1 EXEC xp_cmdshell 'dir'",
    "SELECT 1 EXEC('DROP TABLE t')",
    "SELECT 1 EXECUTE sp_who",
    "SELECT 1 EXECUTE AS LOGIN = 'sa'",
    "EXEC sp_executesql N'SELECT 1'",
    "SELECT 1 GO",
    "SELECT 1\nGO\nSELECT 2",
    "SELECT 1\n\tgo\t\n",
    "SELECT 1 GOTO lbl",
    "SELECT 1 USE master",
    "USE master",
    "SELECT 1 DBCC FREEPROCCACHE",
    "DBCC CHECKDB",
    "SELECT 1 KILL 52",
    "SELECT 1 SHUTDOWN WITH NOWAIT",
    "SELECT 1 RECONFIGURE",
    "SELECT 1 CHECKPOINT",
    "SELECT 1 BEGIN TRAN",
    "SELECT 1 BEGIN TRANSACTION",
    "SELECT 1 BEGIN DISTRIBUTED TRANSACTION",
    "SELECT 1 COMMIT",
    "SELECT 1 COMMIT TRAN",
    "SELECT 1 ROLLBACK",
    "SELECT 1 SAVE TRAN s1",
    "SELECT 1 SAVE TRANSACTION s1",
    "SELECT 1 BACKUP DATABASE x TO DISK = 'y.bak'",
    "SELECT 1 RESTORE DATABASE x FROM DISK = 'y.bak'",
    "SELECT 1 BULK INSERT t FROM 'x.csv'",
    "SELECT * FROM OPENROWSET(BULK 'C:\\x.txt', SINGLE_CLOB) AS x",
    "SELECT 1 DENY SELECT ON t TO u",
    "SELECT 1 ENABLE TRIGGER ALL ON t",
    "SELECT 1 DISABLE TRIGGER ALL ON t",
    "SELECT 1 WRITETEXT t.c @p 'x'",
    "SELECT 1 UPDATETEXT t.c @p 0 NULL 'x'",
    "SELECT 1 READTEXT t.c @p 0 10",
    "SELECT 1 RAISERROR('x', 16, 1)",
    "SELECT 1 THROW 50000, 'x', 1",
    "SELECT 1 REVERT",
    "SELECT 1 SETUSER 'dbo'",
    "SELECT * FROM OPENXML(@h, '/root')",
    "SELECT NEXT VALUE FOR dbo.seq",
    "SELECT next value for dbo.seq",
    "SELECT NEXT\n  VALUE FOR dbo.seq",
    "SELECT NEXT /* gap */ VALUE FOR dbo.seq",
    "SELECT id, NEXT VALUE FOR dbo.seq OVER (ORDER BY id) FROM t",
    # Lead ruling (C6b review): WHILE, IF, PRINT refuse as whole words.
    "SELECT 1 WHILE 1=1 SELECT 2",
    "SELECT 1 while 1 = 1 SELECT 2",
    "WHILE 1 = 1 SELECT 1",
    "IF 1=1 SELECT 1",
    "SELECT 1 IF 1 = 1 SELECT 2",
    "SELECT 1 If (1 = 1) SELECT 2 ELSE SELECT 3",
    "SELECT 1 PRINT 'x'",
    "SELECT 1\nPRINT 'x'",
    # Lead ruling: locking table hints inside WITH (...) refuse.
    "SELECT * FROM t WITH (UPDLOCK)",
    "SELECT * FROM t with (updlock)",
    "SELECT * FROM t WITH (XLOCK, ROWLOCK)",
    "SELECT * FROM t WITH (HOLDLOCK)",
    "SELECT * FROM t WITH (TABLOCK)",
    "SELECT * FROM t WITH (TABLOCKX)",
    "SELECT * FROM t WITH (PAGLOCK)",
    "SELECT * FROM t WITH (ROWLOCK)",
    "SELECT * FROM t WITH (SERIALIZABLE)",
    "SELECT * FROM t WITH (REPEATABLEREAD)",
    "SELECT * FROM t WITH (READPAST)",
    "SELECT * FROM t WITH (NOLOCK, UPDLOCK)",
    "SELECT * FROM t WITH ( NOLOCK , XLOCK )",
    "SELECT * FROM t WITH\n(\n  HOLDLOCK\n)",
    "SELECT * FROM t WITH (/* x */ UPDLOCK)",
    "SELECT a FROM t AS x WITH (TABLOCKX) WHERE a = 1",
    "SELECT * FROM t JOIN u WITH (UPDLOCK) ON t.id = u.id",
    "WITH c AS (SELECT * FROM t WITH (XLOCK)) SELECT * FROM c",
    # Lead ruling: the ten lock hints refuse as whole words anywhere outside
    # strings and quoted identifiers, WITH or not (old-style hints).
    "SELECT * FROM t (UPDLOCK)",
    "SELECT * FROM t a (XLOCK)",
    "SELECT * FROM t AS a (HOLDLOCK)",
    "SELECT * FROM t (NOLOCK, TABLOCKX)",
    "SELECT * FROM t JOIN u (UPDLOCK) ON t.id = u.id",
    "SELECT * FROM t JOIN u b (XLOCK) ON t.id = b.id",
    "SELECT * FROM t LEFT JOIN u AS b (ROWLOCK) ON t.id = b.id",
    "SELECT * FROM t CROSS JOIN u (PAGLOCK)",
    "SELECT 1 AS holdlock",
    "SELECT serializable FROM t",
    "SELECT * FROM t WHERE readpast = 1",
    # C6a words in T-SQL shapes.
    "SELECT 1 SET NOCOUNT ON",
    "SET SHOWPLAN_XML ON",
    "SET STATISTICS XML ON",
    "SELECT 1 INSERT t VALUES (1)",
    "SELECT 1 UPDATE t SET a = 1",
    "SELECT 1 DELETE t",
    "SELECT 1 TRUNCATE TABLE t",
    "SELECT 1 DROP TABLE t",
    "SELECT 1 CREATE TABLE x (a int)",
    "SELECT 1 ALTER TABLE t ADD c int",
    "SELECT 1 MERGE t USING s ON 1 = 1 WHEN MATCHED THEN DELETE",
    "SELECT 1 GRANT SELECT ON t TO u",
    "SELECT 1 REVOKE SELECT ON t FROM u",
    "WITH x AS (SELECT 1 AS a) DELETE FROM t",
    "WITH x AS (SELECT 1 AS a) UPDATE t SET a = 1",
    "SELECT * INTO #tmp FROM t",
    "SELECT * INTO dbo.copy FROM t",
    "SELECT a INTO ##global FROM t",
    "SELECT TOP 1 * INTO t2 FROM t",
    "SELECT * FROM t; EXEC sp_who",
]

MS_REFUSED_FUNCTIONS = [
    "SELECT * FROM OPENROWSET('SQLNCLI', 'Server=x;', 'SELECT 1')",
    "SELECT * FROM openrowset ('SQLNCLI', 'x', 'SELECT 1')",
    "SELECT * FROM OPENQUERY(remote_srv, 'SELECT 1')",
    "SELECT * FROM OPENDATASOURCE('SQLNCLI', 'Data Source=x').db.dbo.t",
    "SELECT xp_cmdshell('dir')",
    "SELECT master.dbo.xp_dirtree('C:\\')",
    "SELECT dbo.sp_helptext('x')",
    "SELECT [xp_cmdshell]('dir')",
    "SELECT [sp_who]()",
    'SELECT "xp_regread"(1)',
    "SELECT [master].[dbo].[xp_dirtree]('C:\\')",
    "SELECT pg_sleep(1)",  # C6a's list is dialect-blind
    "SELECT SLEEP(1)",
]

MS_REFUSED_LEXICAL = [
    # Backslash is literal in T-SQL: 'a\' is a whole string and WAITFOR is
    # live (a backslash-escaping lexer would swallow it).
    (r"SELECT 'a\' WAITFOR DELAY '00:00:05' --'", EITHER),
    (r"SELECT N'a\' WAITFOR DELAY '00:00:05' --'", EITHER),
    (r"SELECT 'x\' ; EXEC xp_cmdshell 'dir' --'", EITHER),
    # A quote inside a [..] or ".." identifier is not a string opener.
    ("SELECT [it's] WAITFOR DELAY '00:00:05' --'", EITHER),
    ("SELECT \"it's\" WAITFOR DELAY '00:00:05' --'", EITHER),
    ("SELECT [a]]] EXEC xp_cmdshell 'dir' --]", EITHER),
    ('SELECT "a""" EXEC xp_cmdshell \'dir\' --"', EITHER),
    # Comments.
    ("SELECT 1 -- x\nWAITFOR DELAY '00:00:05'", EITHER),
    ("SELECT 1 /* a /* b */", EITHER),  # nested and still open
    ("SELECT 1 /* never closed", EITHER),
    # Unterminated.
    ("SELECT 'abc", EITHER),
    ("SELECT N'abc", EITHER),
    ("SELECT [abc", EITHER),
    ('SELECT "abc', EITHER),
    # Non-ASCII outside literals.
    ("SELECT 1 AS é", (CONSTRUCT,)),
    ("SELECT 1 ＷＡＩＴＦＯＲ DELAY '0:0:5'", (CONSTRUCT,)),
    ("SELECT 1\u00a0WAITFOR DELAY '0:0:5'", (INVALID,)),
    ("SELECT 1\x00", (INVALID,)),
    # One statement.
    ("SELECT 1; SELECT 2", (CONSTRUCT,)),
    ("SELECT 1;\nGO", (CONSTRUCT,)),
]

MS_ACCEPTED = [
    "SELECT 1",
    "SELECT 1;",
    "SELECT TOP 10 * FROM dbo.t",
    "SELECT TOP (5) a FROM t ORDER BY a",
    "SELECT * FROM t ORDER BY a OFFSET 0 ROWS FETCH NEXT 5 ROWS ONLY",
    "SELECT * FROM t WITH (NOLOCK)",
    "SELECT * FROM t with (nolock)",
    "SELECT * FROM t WITH (READUNCOMMITTED)",
    "SELECT * FROM t WITH (NOLOCK) JOIN u WITH (READUNCOMMITTED) ON t.id = u.id",
    "SELECT 'WHILE IF PRINT UPDLOCK' AS s",
    "SELECT [while], [if], [print], [updlock] FROM t",
    "SELECT while_count, if_flag, printed_at, rowlock_id FROM t",
    "SELECT a FROM t FOR JSON AUTO",
    "SELECT a FROM t FOR XML PATH('r')",
    "SELECT @@VERSION AS v",
    "SELECT CAST(a AS NVARCHAR(10)) FROM t",
    "SELECT * FROM sys.dm_exec_sessions",
    "SELECT * FROM sys.dm_exec_requests",
    "SELECT TOP 5 qs.total_elapsed_time, st.text FROM sys.dm_exec_query_stats qs "
    "CROSS APPLY sys.dm_exec_sql_text(qs.sql_handle) st ORDER BY 1 DESC",
    "SELECT qp.query_plan FROM sys.dm_exec_query_stats qs "
    "CROSS APPLY sys.dm_exec_query_plan(qs.plan_handle) qp",
    "SELECT 'WAITFOR DELAY ''00:00:05''' AS s",
    "SELECT 'BEGIN TRAN; COMMIT; GO' AS s",
    "SELECT N'; EXEC xp_cmdshell ''dir''; ' AS s",
    "SELECT N'中文' AS s",
    "SELECT 'it''s; DROP TABLE t' AS s",
    r"SELECT 'C:\temp\' AS p",  # one reading: backslash is literal
    r"SELECT 'a\' AS p, 'b' AS q",
    "SELECT [exec], [waitfor], [go], [use], [begin], [kill] FROM t",
    "SELECT [a]]b] FROM t",
    "SELECT [it's] FROM t",
    'SELECT "exec", "go", "declare" FROM t',
    'SELECT "a""b" FROM t',
    "SELECT [é] FROM t",
    "SELECT [next], [value] FROM t",
    "SELECT begin_date, end_date, go_live, use_count, save_point, next_value, "
    "declared_at, commit_sha, backup_id, bulk_flag, enabled, disabled_at, "
    "throw_count, kill_switch, goto_url FROM t",
    "SELECT 1 /* outer /* inner */ EXEC xp_cmdshell 'x' */",  # nests, as T-SQL
    "SELECT 1 /* WAITFOR */ + 1",
    "SELECT 1 -- EXEC xp_cmdshell 'dir'",
    "SELECT 1 --x",
    "WITH a AS (SELECT 1 AS n) SELECT n FROM a",
    "SELECT a FROM t WHERE b IN (SELECT b FROM u)",
    "SELECT COUNT_BIG(*) FROM sys.all_objects",
]


# == PostgreSQL and MySQL keep their C6a verdicts on the new words ================

PG_MY_STILL_ACCEPTED = [
    "SELECT go, waitfor, declare_at, kill_switch FROM t",
    "SELECT 1 AS settings",
    "SELECT url, file, input FROM t",
    "SELECT 'x' AS system",
]
PG_STILL_ACCEPTED = [
    "SELECT format('%s', 1)",  # a PostgreSQL function, banned only in ClickHouse
    "SELECT 1 # 3",
    "SELECT 1 /* outer /* inner */ ; DELETE FROM t */",
]
MY_STILL_ACCEPTED = [
    "SELECT FORMAT(1234.5, 2)",  # a MySQL function
    "SELECT 1 # SETTINGS",
]


def _cases():
    refused, accepted = [], []
    for dialect, words, functions, lexical in (
        (CH, CH_REFUSED_WORDS, CH_REFUSED_FUNCTIONS, CH_REFUSED_LEXICAL),
        (MS, MS_REFUSED_WORDS, MS_REFUSED_FUNCTIONS, MS_REFUSED_LEXICAL),
    ):
        for sql in words + functions:
            refused.append(
                pytest.param(dialect, sql, (CONSTRUCT,), id=f"{dialect}:{sql[:52]!r}")
            )
        for sql, codes in lexical:
            refused.append(
                pytest.param(dialect, sql, codes, id=f"{dialect}:{sql[:52]!r}")
            )
        # C6a's dialect-independent lists apply unchanged (backslash cases
        # are dialect-specific above).
        for sql in C6A_REFUSED_WORDS + C6A_REFUSED_MULTI + C6A_REFUSED_FUNCTIONS:
            refused.append(
                pytest.param(dialect, sql, EITHER, id=f"{dialect}:c6a:{sql[:44]!r}")
            )
        for sql in C6A_REFUSED_INVALID:
            refused.append(
                pytest.param(dialect, sql, (INVALID,), id=f"{dialect}:c6a:{sql[:44]!r}")
            )
    for sql in CH_ACCEPTED:
        accepted.append(pytest.param(CH, sql, id=f"{CH}:{sql[:52]!r}"))
    for sql in MS_ACCEPTED:
        accepted.append(pytest.param(MS, sql, id=f"{MS}:{sql[:52]!r}"))
    return refused, accepted


REFUSED, ACCEPTED = _cases()
OLD_ACCEPTED = [
    *[
        pytest.param(d, sql, id=f"{d}:{sql[:40]!r}")
        for d in ("postgresql", "mysql")
        for sql in PG_MY_STILL_ACCEPTED
    ],
    *[
        pytest.param("postgresql", sql, id=f"pg:{sql[:40]!r}")
        for sql in PG_STILL_ACCEPTED
    ],
    *[pytest.param("mysql", sql, id=f"my:{sql[:40]!r}") for sql in MY_STILL_ACCEPTED],
]
OLD_REFUSED_NEW_WORDS = [
    # Words that are banned only for the new dialects stay out of
    # PostgreSQL/MySQL verdicts, while C6a's own refusals are kept.
    pytest.param("postgresql", "SELECT 1; WAITFOR DELAY '0:0:1'", id="pg-multi"),
    pytest.param("mysql", "SELECT * FROM url('x', CSV)", id="my-url"),
]


def test_statement_problem_seam_knows_the_new_dialects():
    for dialect, sql, _codes in (p.values for p in REFUSED[::17]):
        assert _problem(sql, dialect) is not None, (dialect, sql)
    for dialect, sql in (p.values for p in ACCEPTED[::5]):
        assert _problem(sql, dialect) is None, (dialect, sql)


@pytest.mark.parametrize(("dialect", "sql", "codes"), REFUSED)
def test_refused_statements_never_reach_the_database(monkeypatch, dialect, sql, codes):
    tools = make_tools_b(monkeypatch, dialect)
    outcome = tools.query(sql)
    refused_before_request(outcome, tools.driver, *codes)
    assert tools.driver.connections == [] or tools.driver.sent == []


@pytest.mark.parametrize(("dialect", "sql"), ACCEPTED)
def test_accepted_statements_are_sent_verbatim_once(monkeypatch, dialect, sql):
    tools = make_tools_b(monkeypatch, dialect)
    outcome = tools.query(sql)
    assert outcome.status == "ok", view(outcome)
    sent = [s.statement for s in tools.driver.sent]
    assert sent.count(sql) == 1, sent


@pytest.mark.parametrize(("dialect", "sql"), OLD_ACCEPTED)
def test_postgres_and_mysql_do_not_inherit_the_new_words(monkeypatch, dialect, sql):
    assert _problem(sql, dialect) is None, (dialect, sql)
    tools = make_tools(monkeypatch, dialect)
    outcome = tools.query(sql)
    assert outcome.status == "ok", view(outcome)
    assert [s.statement for s in tools.driver.sent].count(sql) == 1


@pytest.mark.parametrize(("dialect", "sql"), OLD_REFUSED_NEW_WORDS)
def test_postgres_and_mysql_keep_their_own_refusals(dialect, sql):
    assert _problem(sql, dialect) is not None, (dialect, sql)


@pytest.mark.parametrize(
    ("dialect", "sql", "word"),
    [
        (CH, "SELECT 1 SETTINGS max_execution_time = 0", "settings"),
        (CH, "SELECT * FROM s3Cluster('c', 'x')", "s3cluster"),
        (CH, "SELECT * FROM URL('x', CSV)", "url"),
        (CH, "SELECT sleepEachRow(1) FROM numbers(3)", "sleepeachrow"),
        (MS, "SELECT 1 WAITFOR DELAY '0:0:5'", "waitfor"),
        (MS, "SELECT NEXT VALUE FOR dbo.seq", "next value"),
        (MS, "SELECT * FROM OPENXML(@h, '/r')", "openxml"),
        (MS, "SELECT 1 DBCC FREEPROCCACHE", "dbcc"),
        (MS, "SELECT 1 WHILE 1=1 SELECT 2", "while"),
        (MS, "SELECT * FROM t WITH (UPDLOCK)", "updlock"),
    ],
)
def test_notes_name_the_code_and_the_hit_word(monkeypatch, dialect, sql, word):
    tools = make_tools_b(monkeypatch, dialect)
    outcome = tools.query(sql)
    refused_before_request(outcome, tools.driver, CONSTRUCT)
    first = notes(outcome)[0]
    assert word in re.sub(r"\s+", " ", first.lower()), first


@pytest.mark.parametrize("dialect", NEW)
@pytest.mark.parametrize("sql", [None, 1, ["SELECT 1"]])
def test_non_string_sql_is_refused(monkeypatch, dialect, sql):
    tools = make_tools_b(monkeypatch, dialect)
    outcome = tools.run("database_sql_query", {"sql": sql})
    assert outcome.status in ("error", "denied"), view(outcome)
    assert tools.driver.sent == []
