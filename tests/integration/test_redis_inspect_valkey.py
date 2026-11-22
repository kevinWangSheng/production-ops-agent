"""Real-server integration tests for the Redis/Valkey tools (#773, R17):
the six tools against a real Valkey 9.0.x, through the public tool path. No
model is called.

Runs only in the CI job ``db-backends`` (``OPSPILOT_DB_BACKENDS=1``);
skipped elsewhere. Written by the independent test author from the #773
contract, without reading the implementation.

The fixtures, not the product, write: the administrator (the ``default``
user, ``requirepass`` = the CI admin password) creates the ``r773:*`` keys
and the read-only account ``r773_ro`` with exactly the R4 lab/CI grant. The
tool gets the read-only account except where an assertion is about the
administrator. Whether a command reached the server is read from ``INFO
commandstats`` (per-command call counters), with a positive control.

CI environment (ASSUMPTION: names chosen by the test author; the CI step
must provide them): ``C6_DB_HOST`` (127.0.0.1), ``C6_VALKEY_PORT`` (53207),
``C6_ADMIN_PASSWORD`` (``changeme570``, CI-only low entropy) and
``C6_UNREACHABLE_PORT`` (53209). The container runs
``valkey-server --requirepass "$C6_ADMIN_PASSWORD"`` published on
``127.0.0.1:53207``; the tool reaches ``redis://<host>:<port>`` with
``TLS=disable``.
"""

from __future__ import annotations

import json
import os
import socket
from collections.abc import Mapping
from typing import Any

import pytest

from opspilot.tools import ToolRequest
from opspilot.tools.profiles import PROFILE_ENV
from tests.integration.test_tool_expansion_c6a_sql import (
    _INDEX,
    FRAME,
    _assert,
    _executor,
    _rows,
    _view,
)
from tests.test_m1_otel_demo_contract import TARGET_ID

pytestmark = pytest.mark.skipif(
    os.environ.get("OPSPILOT_DB_BACKENDS") != "1",
    reason="real databases run only in the CI db-backends job",
)

INFO_TOOL = "redis_info"
CONFIG_TOOL = "redis_config_get"
SCAN_TOOL = "redis_scan_keys"
KEY_INFO_TOOL = "redis_key_info"
GET_VALUE_TOOL = "redis_get_value"
SLOWLOG_TOOL = "redis_slowlog"

HOST = os.environ.get("C6_DB_HOST", "127.0.0.1")
PORT = int(os.environ.get("C6_VALKEY_PORT", "53207"))
UNREACHABLE_PORT = int(os.environ.get("C6_UNREACHABLE_PORT", "53209"))
ADMIN = "default"
ADMIN_PASSWORD = os.environ.get("C6_ADMIN_PASSWORD", "changeme570")
RO = "r773_ro"
RO_PASSWORD = "ro-" + "pw-773"  # low entropy, CI only
CANARY = "cccccccc773"
#: R4 lab/CI grant, verbatim (may only be narrowed).
GRANT = (
    "reset on >{password} ~* resetchannels -@all +@read +info +ping +hello +auth "
    "+select +client|setname +config|get +slowlog|get +slowlog|len +acl|whoami "
    "+acl|getuser"
)
BIG = 1000
LONG_CHARS = 5000


# -- engineer side: a minimal RESP2 client (never the product) --------------------


class Resp:
    def __init__(self, user: str | None = ADMIN, password: str | None = ADMIN_PASSWORD):
        self.sock = socket.create_connection((HOST, PORT), timeout=10)
        self.file = self.sock.makefile("rb")
        if user is not None:
            assert self.call("AUTH", user, password) == "OK"

    def call(self, *argv: object) -> Any:
        out = b"*%d\r\n" % len(argv)
        for arg in argv:
            data = arg if isinstance(arg, bytes) else str(arg).encode()
            out += b"$%d\r\n%s\r\n" % (len(data), data)
        self.sock.sendall(out)
        return self._read()

    def _read(self) -> Any:
        line = self.file.readline()[:-2]
        kind, rest = line[:1], line[1:]
        if kind == b"+":
            return rest.decode()
        if kind == b"-":
            return RespError(rest.decode())
        if kind == b":":
            return int(rest)
        if kind == b"$":
            size = int(rest)
            return None if size < 0 else self.file.read(size + 2)[:-2]
        if kind == b"*":
            size = int(rest)
            return None if size < 0 else [self._read() for _ in range(size)]
        raise AssertionError(f"bad reply {line!r}")

    def close(self) -> None:
        self.file.close()
        self.sock.close()


class RespError(str):
    pass


def _admin_call(*argv: object) -> Any:
    client = Resp()
    try:
        return client.call(*argv)
    finally:
        client.close()


def _commandstats() -> dict[str, int]:
    text = _admin_call("INFO", "commandstats").decode()
    calls: dict[str, int] = {}
    for line in text.splitlines():
        if line.startswith("cmdstat_"):
            name, _, rest = line.partition(":")
            fields = dict(item.split("=", 1) for item in rest.split(","))
            calls[name[len("cmdstat_") :]] = int(fields["calls"])
    return calls


def _snapshot() -> tuple[int, dict[bytes, bytes], int]:
    """(DBSIZE, DUMP of every key, rdb_changes_since_last_save)."""
    client = Resp()
    try:
        keys: list[bytes] = []
        cursor = b"0"
        while True:
            cursor, page = client.call("SCAN", cursor, "COUNT", 1000)
            keys += page
            if cursor == b"0":
                break
        dumps = {k: client.call("DUMP", k) for k in sorted(keys)}
        persistence = client.call("INFO", "persistence").decode()
        changes = next(
            int(line.split(":", 1)[1])
            for line in persistence.splitlines()
            if line.startswith("rdb_changes_since_last_save:")
        )
        return client.call("DBSIZE"), dumps, changes
    finally:
        client.close()


@pytest.fixture(scope="module", autouse=True)
def _fixtures():
    client = Resp()
    try:
        assert client.call("CONFIG", "SET", "save", "") == "OK"
        assert client.call("CONFIG", "SET", "slowlog-log-slower-than", "0") == "OK"
        client.call("FLUSHALL")
        client.call("SET", "r773:greeting", "hello")
        client.call("SET", "r773:long", "L" * LONG_CHARS)
        client.call("SET", "r773:binary", b"\xff\xfeok")
        client.call("HSET", "r773:user:1", "name", "alice", "password", CANARY)
        client.call(
            "HSET", "r773:bighash", *[x for n in range(BIG) for x in (f"f{n}", "v")]
        )
        client.call("RPUSH", "r773:biglist", *[f"item-{n}" for n in range(BIG)])
        client.call("SADD", "r773:set", *[f"m{n}" for n in range(10)])
        client.call("ZADD", "r773:zset", *[x for n in range(10) for x in (n, f"z{n}")])
        for n in range(5):
            client.call("XADD", "r773:stream", "*", "event", f"e{n}")
        password = GRANT.format(password=RO_PASSWORD)
        assert client.call("ACL", "SETUSER", RO, *password.split()) == "OK"
    finally:
        client.close()
    yield
    _admin_call("ACL", "DELUSER", RO)


@pytest.fixture(scope="module")
def k8s_ca(tmp_path_factory) -> str:
    path = tmp_path_factory.mktemp("r773-k8s") / "synthetic-ca.pem"
    path.write_text("synthetic ca placeholder\n")
    return str(path)


def _env(
    k8s_ca: str, *, user: str = RO, password: str = RO_PASSWORD, port: int = PORT
) -> dict[str, str]:
    return {
        PROFILE_ENV: "otel-demo-ext",
        "OPSPILOT_K8S_API_URL": "https://127.0.0.1:16443",
        "OPSPILOT_K8S_TOKEN": "dddddddd773",
        "OPSPILOT_K8S_CA_FILE": k8s_ca,
        "OPSPILOT_K8S_NAMESPACE": "otel-demo",
        "OPSPILOT_OTEL_PROMETHEUS_URL": "http://127.0.0.1:19090",
        "OPSPILOT_OTEL_JAEGER_URL": "http://127.0.0.1:16686/jaeger/ui",
        "OPSPILOT_REDIS_URL": f"redis://{HOST}:{port}",
        "OPSPILOT_REDIS_USERNAME": user,
        "OPSPILOT_REDIS_PASSWORD": password,
        "OPSPILOT_REDIS_TLS": "disable",
    }


def _run(env: Mapping[str, str], tool: str, params: Mapping[str, object]):
    return _executor(env).execute(
        ToolRequest(
            step_id="r773-live",
            tool_index=next(_INDEX),
            tool_name=tool,
            target_ref=TARGET_ID,
            params=dict(params),
            window=FRAME.as_json(),
        )
    )


def _text(outcome) -> str:
    return json.dumps(_view(outcome), ensure_ascii=False, default=str)


# ASSUMPTION: parameter names as in tests/redis_support.py.
CALLS = {
    INFO_TOOL: {"sections": "memory,clients"},
    CONFIG_TOOL: {"pattern": "maxmemory*"},
    SCAN_TOOL: {"pattern": "r773:*"},
    KEY_INFO_TOOL: {"key": "r773:biglist"},
    GET_VALUE_TOOL: {"key": "r773:greeting"},
    SLOWLOG_TOOL: {"limit": 10},
}
EXPECT = {
    INFO_TOOL: ("used_memory", "connected_clients"),
    CONFIG_TOOL: ("maxmemory-policy",),
    SCAN_TOOL: ("r773:greeting", "r773:biglist", "r773:stream"),
    KEY_INFO_TOOL: ("list", str(BIG)),
    GET_VALUE_TOOL: ("hello",),
    SLOWLOG_TOOL: (),
}


# == real data from all six tools; data unchanged =====================================


def test_all_six_tools_return_real_data_and_change_nothing(k8s_ca):
    before = _snapshot()
    for tool, params in CALLS.items():
        outcome = _run(_env(k8s_ca), tool, params)
        _assert(outcome, "ok", None)
        text = _text(outcome)
        for word in EXPECT[tool]:
            assert word in text, (tool, word)
        assert _rows(outcome), tool
    for kind_key in ("r773:bighash", "r773:set", "r773:zset", "r773:stream"):
        _assert(_run(_env(k8s_ca), GET_VALUE_TOOL, {"key": kind_key}), "ok", None)
    assert _snapshot() == before


def test_large_collections_are_truncated_and_long_strings_cut(k8s_ca):
    for key, unit in (("r773:biglist", "elements"), ("r773:bighash", "fields")):
        outcome = _run(_env(k8s_ca), GET_VALUE_TOOL, {"key": key})
        _assert(outcome, "ok", None)
        view = _view(outcome)
        assert view["incomplete"] is True, key
        assert view[f"backend_{unit}_returned"] == 100, key
    long = _run(_env(k8s_ca), GET_VALUE_TOOL, {"key": "r773:long"})
    _assert(long, "ok", None)
    assert "_truncated_fields" in _text(long)
    assert "L" * 2001 not in _text(long)
    binary = _run(_env(k8s_ca), GET_VALUE_TOOL, {"key": "r773:binary"})
    assert "0xfffe6f6b" in _text(binary).lower()


def test_secrets_are_redacted_on_a_real_server(k8s_ca):
    user = _run(_env(k8s_ca), GET_VALUE_TOOL, {"key": "r773:user:1"})
    _assert(user, "ok", None)
    assert "alice" in _text(user) and CANARY not in _text(user)
    config = _run(_env(k8s_ca), CONFIG_TOOL, {"pattern": "requirepass"})
    _assert(config, "ok", None)
    assert "[REDACTED_CREDENTIAL]" in _text(config)
    assert ADMIN_PASSWORD not in _text(config)
    slowlog = _run(_env(k8s_ca), SLOWLOG_TOOL, {"limit": 128})
    assert ADMIN_PASSWORD not in _text(slowlog) and RO_PASSWORD not in _text(slowlog)


def test_a_missing_key_is_no_data(k8s_ca):
    outcome = _run(_env(k8s_ca), KEY_INFO_TOOL, {"key": "r773:absent"})
    assert outcome.status == "no_data", _view(outcome)


# == the read-only account and the account check ======================================


@pytest.mark.parametrize(
    "argv",
    [
        ("SET", "r773:greeting", "x"),
        ("DEL", "r773:greeting"),
        ("FLUSHALL",),
        ("CONFIG", "SET", "maxmemory", "1"),
    ],
)
def test_the_read_only_account_gets_noperm_on_writes(argv):
    client = Resp(RO, RO_PASSWORD)
    try:
        reply = client.call(*argv)
    finally:
        client.close()
    assert isinstance(reply, RespError) and reply.startswith("NOPERM"), reply
    assert _admin_call("GET", "r773:greeting") == b"hello"


def test_an_admin_account_is_refused_before_any_data_command(k8s_ca):
    # Positive control: commandstats does count what the tool sends.
    start = _commandstats()
    _assert(_run(_env(k8s_ca), SCAN_TOOL, {"pattern": "r773:*"}), "ok", None)
    assert _commandstats().get("scan", 0) > start.get("scan", 0)

    before = _commandstats()
    admin = _env(k8s_ca, user=ADMIN, password=ADMIN_PASSWORD)
    for tool, params in CALLS.items():
        outcome = _run(admin, tool, params)
        _assert(outcome, "error", "SOURCE_FORBIDDEN", "REDIS_ACCOUNT_NOT_READ_ONLY")
        assert ADMIN_PASSWORD not in _text(outcome)
    after = _commandstats()
    for command in ("config|get", "scan", "type", "getrange", "slowlog|get", "pttl"):
        assert after.get(command, 0) == before.get(command, 0), command


def test_a_password_required_server_answers_noauth_and_bad_passwords_fail(k8s_ca):
    anonymous = Resp(None, None)
    try:
        reply = anonymous.call("SCAN", "0")
    finally:
        anonymous.close()
    assert isinstance(reply, RespError) and reply.startswith("NOAUTH"), reply
    wrong = _run(_env(k8s_ca, password="wrong-" + "pw-773"), INFO_TOOL, {})
    _assert(wrong, "error", "SOURCE_FORBIDDEN", "REDIS_AUTH_FAILED")


def test_an_unreachable_port_is_source_unavailable(k8s_ca):
    outcome = _run(_env(k8s_ca, port=UNREACHABLE_PORT), INFO_TOOL, {})
    _assert(outcome, "error", "SOURCE_UNAVAILABLE")
