"""Independent contract tests for the C6a statement check (C6-3, the second
line of PRODUCT-CONSTRAINTS line 18): ``sql_query.sql_statement_problem`` and
the same refusals through ``database_sql_query``.

Written from the C6 task record (「语句检查（第二道，C6-3，发请求前）」),
without reading the implementation. Every refused statement must come back
``INVALID_PARAMS`` with ``QUERY_CONSTRUCT_NOT_ALLOWED`` or ``INVALID_QUERY``
first in notes while the recording driver receives no statement at all;
every accepted statement must reach the driver verbatim, once.

The check is a deny list by design (C6-3); the account check (C6-U1) and the
read-only transaction are the layers that stop writes. These cases are the
bypass attempts the record names -- both backslash readings, MySQL
executable comments and ``--`` without a space, ``U&"``, nested comments,
dollar quoting, writes hidden in CTEs, ``SELECT ... INTO``, locking clauses,
side-effect functions -- plus false-refusal guards for the same words inside
strings, comments, quoted identifiers and longer identifiers.
"""

from __future__ import annotations

import re

import pytest

from tests.c6a_sql_support import make_tools, notes, refused_before_request, view

CONSTRUCT = "QUERY_CONSTRUCT_NOT_ALLOWED"
INVALID = "INVALID_QUERY"
EITHER = (CONSTRUCT, INVALID)
PG, MY = "postgresql", "mysql"
BOTH = (PG, MY)


@pytest.fixture(autouse=True)
def _ca_file(tmp_path, monkeypatch):
    import os

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


# -- refused in both dialects: statement types and deny-listed words -------------

REFUSED_WORDS = [
    "DELETE FROM t",
    "delete from t",
    "  DeLeTe FROM t WHERE id = 1",
    "INSERT INTO t VALUES (1)",
    "UPDATE t SET a = 1",
    "DROP TABLE t",
    "CREATE TABLE t2 (a int)",
    "CREATE TEMP TABLE t2 AS SELECT 1",
    "ALTER TABLE t ADD COLUMN c int",
    "TRUNCATE t",
    "TRUNCATE TABLE t",
    "GRANT ALL ON t TO someone",
    "REVOKE ALL ON t FROM someone",
    "VACUUM t",
    "MERGE INTO t USING s ON t.id = s.id WHEN MATCHED THEN DELETE",
    "REPLACE INTO t VALUES (1)",
    "COPY t TO '/tmp/out.csv'",
    "COPY (SELECT 1) TO PROGRAM 'id'",
    "CALL do_things()",
    "EXECUTE prepared_one",
    "EXEC xp_cmdshell 'dir'",
    "DO $$ BEGIN PERFORM 1; END $$",
    "SET search_path = evil",
    "SET SESSION sql_mode = ''",
    "LOAD DATA INFILE '/etc/passwd' INTO TABLE t",
    "HANDLER t OPEN",
    "LOCK TABLE t IN ACCESS EXCLUSIVE MODE",
    "LOCK TABLES t WRITE",
    "PREPARE p AS DELETE FROM t",
    "BEGIN",
    "COMMIT",
    "ROLLBACK",
    "START TRANSACTION",
    "SAVEPOINT s1",
    "LISTEN chan",
    "NOTIFY chan",
    "RESET ALL",
    "DISCARD ALL",
    "REFRESH MATERIALIZED VIEW mv",
    "REINDEX TABLE t",
    "CLUSTER t",
    "ANALYZE t",
    "CHECKPOINT",
    "VALUES (1)",
    "TABLE t",
    "KILL 12",
    "FLUSH TABLES",
    "OPTIMIZE TABLE t",
    "RENAME TABLE t TO t2",
    "USE otherdb",
    "COMMENT ON TABLE t IS 'x'",
    "DECLARE c CURSOR FOR SELECT 1",
    "FETCH ALL FROM c",
    "DEALLOCATE ALL",
    "IMPORT FOREIGN SCHEMA s FROM SERVER x INTO public",
    "INSTALL PLUGIN p SONAME 'x.so'",
    # Writes hidden behind an allowed first word.
    "WITH x AS (DELETE FROM t RETURNING *) SELECT * FROM x",
    "WITH x AS (INSERT INTO t VALUES (1) RETURNING id) SELECT id FROM x",
    "WITH x AS (UPDATE t SET a = 1 RETURNING a) SELECT a FROM x",
    "with x as (\n  delete from t returning *\n) select 1",
    "WITH a AS (SELECT 1), b AS (DELETE FROM t RETURNING 1) SELECT * FROM a",
    "EXPLAIN ANALYZE DELETE FROM t",
    "EXPLAIN ANALYZE INSERT INTO t SELECT 1",
    "EXPLAIN (ANALYZE) UPDATE t SET a = 1",
    "EXPLAIN CREATE TABLE t2 AS SELECT 1",
    "SELECT * INTO t_copy FROM t",
    "SELECT a INTO TEMP t_copy FROM t",
    "SELECT 1 INTO OUTFILE '/tmp/x'",
    "SELECT 1 INTO DUMPFILE '/tmp/x'",
    "SELECT 1 INTO @v",
    "SELECT * FROM t FOR UPDATE",
    "SELECT * FROM t for update",
    "SELECT * FROM t FOR\n  UPDATE",
    "SELECT * FROM t FOR UPDATE NOWAIT",
    "SELECT * FROM t FOR UPDATE SKIP LOCKED",
    "SELECT * FROM t FOR SHARE",
    "SELECT * FROM t FOR SHARE OF t",
    "SELECT * FROM t FOR NO KEY UPDATE",
    "SELECT * FROM t FOR KEY SHARE",
    "SELECT * FROM t LOCK IN SHARE MODE",
    "SELECT * FROM t FOR /* hidden */ UPDATE",
    "(DELETE FROM t)",
    "((INSERT INTO t VALUES (1)))",
    "/* SELECT */ DELETE FROM t",
    "-- SELECT\nDELETE FROM t",
    # Documented false refusals: REPLACE() and CHARACTER SET (C6-3 rule 5).
    "SELECT replace(name, 'a', 'b') FROM t",
    "SELECT CAST(name AS CHAR CHARACTER SET utf8mb4) FROM t",
    "SELECT name FROM t WHERE name = 'x' OR do = 1",
]

# -- refused in both dialects: more than one statement ---------------------------

REFUSED_MULTI = [
    "SELECT 1; DELETE FROM t",
    "SELECT 1;DELETE FROM t",
    "SELECT 1; SELECT 2",
    "SELECT 1;;",
    "SELECT 1 ; ;",
    ";SELECT 1",
    "SELECT 1; /* x */ SELECT 2",
    "SELECT 1\n;\nSELECT 2;",
]

# -- refused in both dialects: side-effect and out-of-bounds functions -----------

REFUSED_FUNCTIONS = [
    "SELECT pg_sleep(10)",
    "SELECT PG_SLEEP (1)",
    "SELECT Pg_Sleep\n(1)",
    "SELECT pg_sleep/**/(1)",
    "SELECT pg_sleep_for('1 minute')",
    "SELECT pg_sleep_until(now() + interval '1 minute')",
    "SELECT pg_catalog.pg_sleep(1)",
    'SELECT "pg_catalog".pg_sleep(1)',
    "SELECT (SELECT pg_sleep(1))",
    "SELECT 1 WHERE pg_sleep(1) IS NOT NULL",
    "SELECT * FROM generate_series(1, 3) g, pg_sleep(1)",
    "SELECT pg_terminate_backend(12345)",
    "SELECT pg_cancel_backend(12345)",
    "SELECT set_config('search_path', 'evil', false)",
    "SELECT lo_import('/etc/passwd')",
    "SELECT lo_export(16400, '/tmp/x')",
    "SELECT lo_unlink(16400)",
    "SELECT lo_from_bytea(0, 'x')",
    "SELECT pg_read_file('/etc/passwd')",
    "SELECT pg_read_binary_file('/etc/passwd')",
    "SELECT pg_ls_dir('.')",
    "SELECT dblink('host=10.0.0.1', 'SELECT 1')",
    "SELECT dblink_exec('host=10.0.0.1', 'DROP TABLE t')",
    "SELECT dblink_connect('c', 'host=10.0.0.1')",
    "SELECT pg_advisory_lock(1)",
    "SELECT pg_advisory_xact_lock(1)",
    "SELECT pg_notify('chan', 'x')",
    "SELECT pg_stat_reset()",
    "SELECT pg_stat_reset_shared('bgwriter')",
    "SELECT pg_reload_conf()",
    "SELECT pg_rotate_logfile()",
    "SELECT pg_switch_wal()",
    "SELECT pg_logical_emit_message(true, 'p', 'x')",
    "SELECT SLEEP(5)",
    "SELECT sleep (5)",
    "SELECT * FROM t WHERE SLEEP(1) = 0",
    "SELECT BENCHMARK(1000000, MD5('a'))",
    "SELECT GET_LOCK('a', 10)",
    "SELECT LOAD_FILE('/etc/passwd')",
    "SELECT MASTER_POS_WAIT('binlog.000001', 4)",
    "SELECT SOURCE_POS_WAIT('binlog.000001', 4)",
    "SELECT xp_cmdshell('dir')",
    "SELECT sp_executesql('x')",
    "SELECT * FROM OPENROWSET('SQLNCLI', 'x', 'SELECT 1')",
    "SELECT * FROM OPENQUERY(remote_srv, 'SELECT 1')",
    "SELECT * FROM OPENDATASOURCE('SQLNCLI', 'x').db.dbo.t",
    "SELECT * FROM url('http://10.0.0.1/x', CSV)",
    "SELECT * FROM file('x.csv')",
    "SELECT * FROM s3('http://bucket/x')",
    "SELECT * FROM remote('10.0.0.1', db.t)",
    "SELECT * FROM remoteSecure('10.0.0.1', db.t)",
    "SELECT * FROM mysql('h:3306', 'd', 't', 'u', 'p')",
    "SELECT * FROM postgresql('h:5432', 'd', 't', 'u', 'p')",
    "SELECT * FROM executable('x.sh', TSV, 'a String')",
]

# -- refused in both dialects: characters, length, unterminated tokens -----------

REFUSED_INVALID = [
    "",
    "SELECT 1\x00",
    "SELECT\x0b1",
    "SELECT 1\u200b",
    "SELECT 1\x1b[0m",
    "SELECT\u00a01",
    "SELECT 1\u2028",
    "SELECT 1\x7f",
    "SELECT '" + "a" * 19_992 + "'",  # 20001 characters
    "SELECT 'abc",
    "SELECT 1 /* never closed",
]

# -- refused in both: the two backslash readings (C6-3 rule 2) -------------------

REFUSED_BACKSLASH = [
    # Without backslash escapes the string ends at \' and a DELETE follows.
    r"SELECT 'a\' ; DELETE FROM t; -- '",
    # With backslash escapes the string ends one quote later and DELETE shows.
    r"SELECT 'x\'' DELETE FROM t --'",
    # Valid under one reading, unterminated under the other.
    r"SELECT 'a\'' AS x",
    r"SELECT 'it\'s' AS x",
    r"SELECT 'a\'' , 'b' ; DELETE FROM t; -- '",
]

ACCEPTED_BOTH = [
    "SELECT 1",
    "select 1",
    "Select 1 AS n",
    "SELECT 1;",
    "SELECT 1 ;",
    "SELECT 1;\n\t ",
    "  \n\tSELECT 1\r\n",
    "(SELECT 1)",
    "((SELECT 1) UNION (SELECT 2))",
    "/* note */ SELECT 1",
    "-- note\nSELECT 1",
    "WITH a AS (SELECT 1 AS n) SELECT n FROM a",
    "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r WHERE n < 3) "
    "SELECT n FROM r",
    "EXPLAIN SELECT * FROM t",
    "EXPLAIN ANALYZE SELECT * FROM t",
    "SELECT 'DELETE FROM t' AS s",
    "SELECT 'x; DROP TABLE t; --' AS s",
    "SELECT 'it''s ; DELETE' AS s",
    "SELECT ';' AS s",
    "SELECT 'pg_sleep(1)' AS s",
    "SELECT 'INTO OUTFILE' AS s, 'FOR UPDATE' AS u",
    "SELECT 1 /* ; DELETE FROM t; */",
    "SELECT 1 -- ; DELETE FROM t",
    "SELECT 1 -- DROP TABLE t\n",
    "SELECT 1 /* INSERT */ + 1",
    "SELECT created_at, updated_at, deleted, inserted_by, settings FROM t",
    "SELECT into_count, dropped, replaced_at, lock_id, copy_count FROM t",
    "SELECT call_id, exec_time, do_flag, set_id, loaded, handler_name FROM t",
    "SELECT merged, granted, revoked_at, truncated, vacuumed, altered FROM t",
    "SELECT sleep_ms, file_name, url_path, remote_addr, mysql_version FROM t",
    "SELECT url, file, s3, remote FROM t",
    "SELECT lower(name), count(*), max(id) FROM t GROUP BY 1",
    "SELECT replace_count FROM t",
    "SELECT count(*) FROM file_table",
    "SELECT now() - interval '1 hour' AS since",
    "SELECT 'café ☕ 中文' AS s",
    "SELECT '" + "a" * 19_986 + "' AS s",  # exactly 20000 characters
    r"SELECT 'C:\path\file' AS p",
    r"SELECT 'a\\' AS p",
    r"SELECT 'a\\', 'b' AS p",
    "SELECT a FROM t WHERE b IN (SELECT b FROM u) ORDER BY a LIMIT 10",
    "SELECT * FROM t t1 JOIN u ON t1.id = u.t_id WHERE t1.x > 0",
]

ACCEPTED_PG = [
    'SELECT "delete", "insert" FROM t',
    'SELECT "lock", "set" FROM t',
    'SELECT "a""b" FROM t',
    "SELECT $$DROP TABLE t$$ AS s",
    "SELECT $$ ; DELETE FROM t $$ AS s",
    "SELECT $tag$ ; DELETE $x$ ; $tag$ AS s",
    "SELECT $a$it's$a$ AS s",
    r"SELECT E'it\'s' AS s",
    r"SELECT E'a\\b' AS s",
    "SELECT 1 /* outer /* inner */ ; DELETE FROM t */",
    "SELECT 1 --DELETE FROM t",
    "SELECT 1--x",
    "SHOW server_version",
    "SHOW statement_timeout",
    "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) SELECT 1",
    "SELECT * FROM pg_stat_activity",
    "SELECT relname FROM pg_catalog.pg_class LIMIT 5",
    "SELECT 1 # 3",
]

ACCEPTED_MY = [
    "SELECT `delete`, `insert` FROM t",
    "SELECT `lock`, `set` FROM t",
    "SELECT `a``b` FROM t",
    'SELECT "DELETE FROM t" AS s',
    'SELECT "x; DROP TABLE t" AS s',
    "SELECT 1 # ; DELETE FROM t",
    "SELECT 1 #DROP TABLE t\n",
    "SELECT 1 -- comment",
    "SELECT 1 --\tcomment",
    "SELECT 1 --\n",
    "SELECT 1 /* a /* b */",
    "SHOW TABLES",
    "SHOW PROCESSLIST",
    "DESCRIBE t",
    "DESC t",
    "EXPLAIN FORMAT=JSON SELECT 1",
    "SELECT N'; DELETE FROM t; ' AS s",
]

REFUSED_PG = [
    # A quoted identifier naming a listed function, then "(" (in MySQL "..."
    # is a string, so these two are PostgreSQL-only).
    ('SELECT "pg_sleep"(1)', (CONSTRUCT,)),
    ('SELECT pg_catalog."pg_sleep"(1)', (CONSTRUCT,)),
    ('SELECT U&"d\\0061ta" FROM t', EITHER),
    ('SELECT u&"x" FROM t', EITHER),
    ('SELECT "abc', EITHER),
    ("SELECT $$abc", EITHER),
    ("SELECT $t$abc$x$", EITHER),
    ("SELECT 1 /* /* */", EITHER),
    # ``#`` is an operator in PostgreSQL, not a comment.
    ("SELECT 1 # ; DELETE FROM t", EITHER),
    # ``$`` inside an identifier does not open a dollar quote.
    ("SELECT a$b$ ; DELETE FROM t ; $b$", EITHER),
    ("SELECT 1 AS x$$ ; DELETE FROM t ; $$", EITHER),
    ('SELECT "x" ; DELETE FROM t', EITHER),
]

REFUSED_MY = [
    ("SELECT 1 /*! , SLEEP(5) */", EITHER),
    ("SELECT 1 /*!50000 UNION SELECT 2 */", EITHER),
    ("SELECT 1 /*M! , 2 */", EITHER),
    ("SELECT /*!*/ 1", EITHER),
    ("SELECT /*!50000 1 */", EITHER),
    # ``--`` not followed by whitespace is arithmetic in MySQL, not a comment.
    ("SELECT 1 --1", EITHER),
    ("SELECT 5--2", EITHER),
    ("SELECT 1 --DELETE FROM t", EITHER),
    # MySQL comments do not nest: the first */ ends this one.
    ("SELECT 1 /* outer /* inner */ ; DELETE FROM t */", EITHER),
    # Not a dollar-quoted string in MySQL.
    ("SELECT $$ ; DELETE FROM t $$", EITHER),
    ("SELECT 1 #\n; DELETE FROM t", EITHER),
    ('SELECT "a\\" ; DELETE FROM t; -- "', EITHER),
    ("SELECT `abc", EITHER),
    ('SELECT "abc', EITHER),
    ("SELECT `sleep`(1)", EITHER),
    ("SELECT `a``b` ; DELETE FROM t", EITHER),
]


def _refused_cases():
    for sql in REFUSED_WORDS + REFUSED_MULTI + REFUSED_FUNCTIONS:
        for dialect in BOTH:
            yield pytest.param(dialect, sql, (CONSTRUCT,), id=f"{dialect}:{sql[:48]!r}")
    for sql in REFUSED_INVALID:
        for dialect in BOTH:
            yield pytest.param(dialect, sql, (INVALID,), id=f"{dialect}:{sql[:48]!r}")
    for sql in REFUSED_BACKSLASH:
        for dialect in BOTH:
            yield pytest.param(dialect, sql, EITHER, id=f"{dialect}:{sql[:48]!r}")
    for dialect, cases in ((PG, REFUSED_PG), (MY, REFUSED_MY)):
        for sql, codes in cases:
            yield pytest.param(dialect, sql, codes, id=f"{dialect}:{sql[:48]!r}")


def _accepted_cases():
    for sql in ACCEPTED_BOTH:
        for dialect in BOTH:
            yield pytest.param(dialect, sql, id=f"{dialect}:{sql[:48]!r}")
    for dialect, cases in ((PG, ACCEPTED_PG), (MY, ACCEPTED_MY)):
        for sql in cases:
            yield pytest.param(dialect, sql, id=f"{dialect}:{sql[:48]!r}")


REFUSED = list(_refused_cases())
ACCEPTED = list(_accepted_cases())


def test_case_lists_have_the_lengths_they_claim():
    assert len("SELECT '" + "a" * 19_992 + "'") == 20_001
    assert len("SELECT '" + "a" * 19_986 + "' AS s") == 20_000


def test_statement_problem_is_the_module_level_seam():
    # The integration test monkeypatches this function (⑥); its verdicts
    # agree with the tool's on a sample of each list.
    for dialect, sql, _codes in (p.values for p in REFUSED[::25]):
        assert _problem(sql, dialect) is not None, (dialect, sql)
    for dialect, sql in (p.values for p in ACCEPTED[::10]):
        assert _problem(sql, dialect) is None, (dialect, sql)


@pytest.mark.parametrize(("dialect", "sql", "codes"), REFUSED)
def test_refused_statements_never_reach_the_database(monkeypatch, dialect, sql, codes):
    tools = make_tools(monkeypatch, dialect)
    outcome = tools.query(sql)
    refused_before_request(outcome, tools.driver, *codes)


@pytest.mark.parametrize(("dialect", "sql"), ACCEPTED)
def test_accepted_statements_are_sent_verbatim_once(monkeypatch, dialect, sql):
    tools = make_tools(monkeypatch, dialect)
    outcome = tools.query(sql)
    assert outcome.status == "ok", view(outcome)
    sent = [s.statement for s in tools.driver.sent]
    assert sent.count(sql) == 1, sent


@pytest.mark.parametrize(
    ("dialect", "sql", "word"),
    [
        (PG, "DELETE FROM t", "delete"),
        (MY, "SELECT * FROM t FOR UPDATE", "for update"),
        (PG, "SELECT pg_catalog.pg_sleep(1)", "pg_sleep"),
        (MY, "SELECT SLEEP(5)", "sleep"),
        (PG, "WITH x AS (DELETE FROM t RETURNING *) SELECT * FROM x", "delete"),
        (MY, "SELECT 1 INTO OUTFILE '/tmp/x'", "into"),
        (PG, "SELECT lo_export(1, '/tmp/x')", "lo_export"),
    ],
)
def test_notes_name_the_code_and_the_hit_word(monkeypatch, dialect, sql, word):
    tools = make_tools(monkeypatch, dialect)
    outcome = tools.query(sql)
    refused_before_request(outcome, tools.driver, CONSTRUCT)
    first = notes(outcome)[0]
    assert word in re.sub(r"\s+", " ", first.lower()), first


@pytest.mark.parametrize("dialect", BOTH)
@pytest.mark.parametrize("sql", [None, 1, ["SELECT 1"], {"q": "SELECT 1"}])
def test_non_string_sql_is_refused(monkeypatch, dialect, sql):
    tools = make_tools(monkeypatch, dialect)
    outcome = tools.run("database_sql_query", {"sql": sql})
    assert outcome.status in ("error", "denied"), view(outcome)
    assert tools.driver.sent == []


def test_missing_sql_is_refused(monkeypatch):
    tools = make_tools(monkeypatch, PG)
    outcome = tools.run("database_sql_query", {})
    assert outcome.status in ("error", "denied"), view(outcome)
    assert tools.driver.sent == []
