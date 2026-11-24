"""Implementer real-server tests for the Redis/Valkey tools (#773): the six
``redis_*`` tools against a real Valkey 9.0 through the public tool path. No
model is called.

Runs only in the CI job ``db-backends`` (``OPSPILOT_DB_BACKENDS=1``), which
starts ``c6-valkey`` with an admin password for the ``default`` user; skipped
everywhere else. The fixtures, not the product, write: the admin connection
seeds database 7 and creates the read-only account with the documented lab
grant. The tool uses the read-only account except where a test configures
another one on purpose.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from typing import Any

import pytest

from opspilot.tools import redis_inspect as ri
from tests.test_redis_inspect import _execute, _view
from tests.test_tool_expansion_c5a_contract import _base_env

pytestmark = pytest.mark.skipif(
    os.environ.get("OPSPILOT_DB_BACKENDS") != "1",
    reason="real databases run only in the CI db-backends job",
)

HOST = os.environ.get("C6_DB_HOST", "127.0.0.1")
PORT = int(os.environ.get("C6_VALKEY_PORT", "53207"))
ADMIN_PASSWORD = os.environ.get("C6_ADMIN_PASSWORD", "")
UNREACHABLE = int(os.environ.get("C6_UNREACHABLE_PORT", "53209"))
DB = 7
RO = "c773_impl_ro"
PASSWORD = "ro" + "only" + "773i"
SECRET = "plain-" + "secret-" + "value-773i"
GRANT = (
    "reset on >{pw} ~* resetchannels -@all +@read +info +ping +hello +auth "
    "+select +client|setname +config|get +slowlog|get +slowlog|len "
    "+acl|whoami +acl|getuser"
)


def _admin() -> Any:
    config = ri.RedisConfig(
        url=f"redis://{HOST}:{PORT}",
        username="default",
        password=ADMIN_PASSWORD,
        tls="disable",
    )
    conn = ri.default_connector(config, 5.0)
    conn.command(["AUTH", "default", ADMIN_PASSWORD])
    conn.command(["SELECT", str(DB)])
    return conn


def _admin_run(*argv: str) -> Any:
    conn = _admin()
    try:
        return conn.command(list(argv))
    finally:
        conn.close()


@pytest.fixture(scope="module", autouse=True)
def seeded() -> Iterator[None]:
    conn = _admin()
    try:
        conn.command(["FLUSHDB"])
        conn.command(["ACL", "SETUSER", RO, *GRANT.format(pw=PASSWORD).split()])
        conn.command(["SET", "c773:plain", "hello"])
        conn.command(["SET", "c773:long", "L" * 9000])
        conn.command(["SET", "c773:api_token", SECRET])
        conn.command(["HSET", "c773:user", "name", "ann", "password", SECRET])
        conn.command(["RPUSH", "c773:list", *[str(i) for i in range(600)]])
        conn.command(["SADD", "c773:set", "a", "b"])
        conn.command(["ZADD", "c773:z", "1.5", "m1", "2", "m2"])
        conn.command(["XADD", "c773:stream", "*", "event", "paid"])
        conn.command(["CONFIG", "SET", "slowlog-log-slower-than", "0"])
        conn.command(["PEXPIRE", "c773:plain", "600000"])
    finally:
        conn.close()
    yield
    _admin_run("CONFIG", "SET", "slowlog-log-slower-than", "10000")
    _admin_run("FLUSHDB")
    _admin_run("ACL", "DELUSER", RO)


@pytest.fixture(autouse=True)
def ca_file(monkeypatch, tmp_path) -> None:
    ca = tmp_path / "ca.pem"
    ca.write_text("synthetic ca placeholder\n")
    monkeypatch.setenv("C5A_TEST_K8S_CA_FILE", str(ca))


def _env(user: str = RO, password: str = PASSWORD, port: int = PORT) -> dict[str, str]:
    return {
        **_base_env(),
        ri.URL_ENV: f"redis://{HOST}:{port}",
        ri.USERNAME_ENV: user,
        ri.PASSWORD_ENV: password,
        ri.DATABASE_ENV: str(DB),
        ri.TLS_ENV: "disable",
    }


def test_six_tools_return_real_data_and_change_nothing() -> None:
    before = _admin_run("DBSIZE")
    info = _view(_execute(ri.INFO_TOOL, {"sections": "memory,keyspace"}, _env()))
    assert {row["section"] for row in info["content"]} >= {"memory", "keyspace"}
    config = _view(_execute(ri.CONFIG_TOOL, {"pattern": "*"}, _env()))
    values = {row["name"]: row["value"] for row in config["content"]}
    assert values["requirepass"] == "[REDACTED_CREDENTIAL]" and "maxmemory" in values
    keys = _view(_execute(ri.SCAN_TOOL, {"pattern": "c773:*"}, _env()))
    assert len(keys["content"]) == 8
    hashes = _view(
        _execute(ri.SCAN_TOOL, {"pattern": "c773:*", "type": "hash"}, _env())
    )
    assert hashes["content"] == [{"key": "c773:user"}]
    meta = _view(_execute(ri.KEY_INFO_TOOL, {"key": "c773:list"}, _env()))
    assert meta["content"][0]["length"] == 600 and meta["content"][0]["type"] == "list"
    plain = _view(_execute(ri.KEY_INFO_TOOL, {"key": "c773:plain"}, _env()))
    assert 0 < plain["content"][0]["ttl_ms"] <= 600000
    for key, unit_rows in (
        ("c773:plain", [{"value": "hello"}]),
        ("c773:set", None),
        ("c773:z", [{"member": "m1", "score": 1.5}, {"member": "m2", "score": 2.0}]),
    ):
        view = _view(_execute(ri.VALUE_TOOL, {"key": key}, _env()))
        if unit_rows is not None:
            assert view["content"] == unit_rows, view
    stream = _view(_execute(ri.VALUE_TOOL, {"key": "c773:stream"}, _env()))
    assert stream["content"][0]["fields"] == {"event": "paid"}
    slow = _view(_execute(ri.SLOWLOG_TOOL, {"limit": 5}, _env()))
    assert slow["content"] and slow["content"][0]["time"].endswith("Z")
    assert _admin_run("DBSIZE") == before


def test_redaction_and_caps_on_real_values() -> None:
    secret = _view(_execute(ri.VALUE_TOOL, {"key": "c773:api_token"}, _env()))
    user = _view(_execute(ri.VALUE_TOOL, {"key": "c773:user"}, _env()))
    assert SECRET not in json.dumps(secret) and SECRET not in json.dumps(user)
    long = _view(_execute(ri.VALUE_TOOL, {"key": "c773:long"}, _env()))
    assert long["content"][0]["_truncated_fields"] == ["value"]
    listed = _execute(ri.VALUE_TOOL, {"key": "c773:list", "limit": 500}, _env())
    assert len(_view(listed)["content"]) == 500 and _view(listed)["incomplete_reason"]


def test_read_only_account_is_refused_writes_by_the_server() -> None:
    config = ri.RedisConfig(
        url=f"redis://{HOST}:{PORT}", username=RO, password=PASSWORD, tls="disable"
    )
    conn = ri.default_connector(config, 5.0)
    try:
        conn.command(["AUTH", RO, PASSWORD])
        for argv in (
            ["SET", "c773:x", "1"],
            ["DEL", "c773:plain"],
            ["FLUSHALL"],
            ["CONFIG", "SET", "maxmemory", "1"],
        ):
            with pytest.raises(ri.RedisReplyError) as error:
                conn.command(argv)
            assert error.value.word == "NOPERM", argv
    finally:
        conn.close()


def test_admin_account_and_bad_login_are_refused() -> None:
    admin = _execute(ri.SCAN_TOOL, {}, _env("default", ADMIN_PASSWORD))
    assert admin.reason == "SOURCE_FORBIDDEN"
    assert "REDIS_ACCOUNT_NOT_READ_ONLY" in json.dumps(_view(admin))
    wrong = _execute(ri.SCAN_TOOL, {}, _env(password="wrong" + "773i"))
    assert wrong.reason == "SOURCE_FORBIDDEN"
    assert "REDIS_AUTH_FAILED" in json.dumps(_view(wrong))
    missing = _execute(ri.VALUE_TOOL, {"key": "c773:absent"}, _env())
    assert missing.status == "no_data" and "KEY_NOT_FOUND" in json.dumps(_view(missing))
    down = _execute(ri.SCAN_TOOL, {}, _env(port=UNREACHABLE))
    assert down.reason == "SOURCE_UNAVAILABLE"
