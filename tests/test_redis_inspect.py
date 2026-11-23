"""Implementer unit tests for the Redis/Valkey tools (#773). The independent
contract tests are the acceptance suite; these pin the implementer's own
choices through the public profile path with a recording fake connector, plus
the RESP2 client against an in-process socket server. No real server, no
model. Credentials are low-entropy placeholders.
"""

from __future__ import annotations

import json
import shutil
import socket
import threading
import time
from collections.abc import Sequence
from datetime import timedelta
from typing import Any

import pytest

from opspilot.tools import ToolRequest
from opspilot.tools import otel_demo_ext as ext
from opspilot.tools import redis_inspect as ri
from opspilot.tools.profiles import select_profile
from opspilot.tools.registry import ToolContractError
from opspilot.tracing import check_lab_target
from tests.m1_tool_support import FakeClock, RecordingSink
from tests.test_m1_otel_demo_contract import NOW, TARGET_ID, FakeStore, _lease
from tests.test_tool_expansion_c5a_contract import FRAME, _base_env

PASSWORD = "readonly" + "773"
URL = "redis://127.0.0.1:56379"
CANARY = "canary-" + "773-" + "value"

GRANT = (
    "-@all +@read +info +ping +hello +auth +select +client|setname +config|get "
    "+slowlog|get +slowlog|len +acl|whoami +acl|getuser"
)


def _getuser(commands: str = GRANT, **extra: Any) -> list[Any]:
    fields: dict[str, Any] = {
        "flags": [b"on", b"sanitize-payload"],
        "passwords": [CANARY.encode()],
        "commands": None if commands is None else commands.encode(),
        "keys": b"~*",
        "channels": b"",
        "selectors": [],
        **extra,
    }
    flat: list[Any] = []
    for name, value in fields.items():
        if value is not None:
            flat += [name.encode(), value]
    return flat


class Script:
    def __init__(self) -> None:
        self.sent: list[list[str]] = []
        self.getuser: Any = _getuser()
        self.keys: dict[str, tuple[str, Any]] = {}
        self.fail: dict[str, Exception] = {}
        self.scan_forever = False
        self.timeouts: list[float] = []

    def connector(self, config: ri.RedisConfig, timeout: float) -> Any:
        script = self
        assert config.password == PASSWORD
        script.timeouts.append(timeout)

        class Conn:
            def command(self, argv: Sequence[str]) -> Any:
                argv = list(argv)
                script.sent.append(argv)
                name = argv[0].upper()
                if name in script.fail:
                    raise script.fail[name]
                if name in ("AUTH", "SELECT") or argv[:2] == ["CLIENT", "SETNAME"]:
                    return "OK"
                if argv[:2] == ["ACL", "WHOAMI"]:
                    return b"opspilot_ro"
                if argv[:2] == ["ACL", "GETUSER"]:
                    return script.getuser
                if name == "INFO":
                    return b"# Memory\r\nused_memory:1024\r\n# Clients\r\nconnected_clients:3\r\n"
                if name == "CONFIG":
                    return [
                        b"requirepass",
                        b"",
                        b"maxmemory",
                        b"0",
                        b"masteruser",
                        b"x",
                        b"primaryuser",
                        b"x",
                        b"primaryauth",
                        b"",
                        b"tls-key-file-pass",
                        CANARY.encode(),
                    ]
                if name == "SCAN":
                    if script.scan_forever:
                        return [b"7", [f"k{len(script.sent)}".encode()]]
                    return [b"0", [k.encode() for k in script.keys]]
                key = argv[1] if len(argv) > 1 else ""
                kind, value = script.keys.get(key, ("none", None))
                if name == "TYPE":
                    return kind
                if name == "PTTL":
                    return -1 if kind != "none" else -2
                if argv[:2] == ["OBJECT", "ENCODING"]:
                    return b"listpack"
                if argv[:2] == ["MEMORY", "USAGE"]:
                    return 72
                if name in ("STRLEN", "LLEN", "HLEN", "SCARD", "ZCARD", "XLEN"):
                    return len(value)
                if name == "GETRANGE":
                    return value[: int(argv[3]) + 1]
                if name == "LRANGE":
                    return value[: int(argv[3]) + 1]
                if name == "HSCAN":
                    flat: list[Any] = []
                    for field, item in value.items():
                        flat += [field.encode(), item]
                    return [b"0", flat]
                if name == "SLOWLOG":
                    return [
                        [
                            3,
                            1763769600,
                            15000,
                            [b"AUTH", b"opspilot_ro", CANARY.encode()],
                            b"127.0.0.1:5000",
                            b"app",
                        ],
                        [2, 1763769600, 12000, [b"KEYS", b"*"], b"127.0.0.1:5001", b""],
                    ][: int(argv[2])]
                raise AssertionError(argv)

            def cancel(self) -> None:
                script.sent.append(["<cancel>"])

            def close(self) -> None:
                script.sent.append(["<close>"])

        return Conn()


@pytest.fixture
def fake(monkeypatch, tmp_path) -> Script:
    ca = tmp_path / "ca.pem"
    ca.write_text("synthetic ca placeholder\n")
    monkeypatch.setenv("C5A_TEST_K8S_CA_FILE", str(ca))
    script = Script()
    monkeypatch.setattr(ri, "default_connector", script.connector)
    return script


def _env(**extra: str) -> dict[str, str]:
    return {
        **_base_env(),
        ri.URL_ENV: URL,
        ri.USERNAME_ENV: "opspilot_ro",
        ri.PASSWORD_ENV: PASSWORD,
        ri.TLS_ENV: "disable",
        **extra,
    }


def _execute(tool: str, params: dict[str, object], env: dict[str, str] | None = None):
    if shutil.which("jq") is None or shutil.which("python3") is None:
        pytest.skip("the extension worker needs jq and python3 on PATH")
    clock = FakeClock(start=NOW)
    profile = select_profile(env or _env())
    lease = _lease()
    deadline = NOW + timedelta(seconds=600)
    factory = profile.executor_factory(
        FakeStore(lease, deadline=deadline), RecordingSink(), clock
    )
    model_input = profile.face(clock).input_for(
        run_id=str(lease.run_id),
        question="Why is checkout slow?",
        target_id=TARGET_ID,
        deadline=deadline,
        model_requests=12,
    )
    return factory(lease, model_input).execute(
        ToolRequest(
            step_id="redis-step",
            tool_index=0,
            tool_name=tool,
            target_ref=TARGET_ID,
            params=params,
            window=FRAME.as_json(),
        )
    )


def _view(outcome) -> dict[str, Any]:
    assert outcome.model_view is not None
    return dict(outcome.model_view)


HANDSHAKE = [
    ["AUTH", "opspilot_ro", PASSWORD],
    ["CLIENT", "SETNAME", "opspilot-redis-tool"],
    ["ACL", "WHOAMI"],
    ["ACL", "GETUSER", "opspilot_ro"],
]


# -- account check ------------------------------------------------------------------


def test_account_check_accepts_the_lab_grant() -> None:
    assert ri.account_problem(_getuser()) is None


@pytest.mark.parametrize(
    "reply",
    [
        _getuser("+@all"),
        _getuser("-@all +@read +@write"),
        _getuser("-@all +@read +@keyspace"),
        _getuser("-@all +@read +@connection"),
        _getuser("-@all +@read +config|set"),
        _getuser("-@all +@read +acl|setuser"),
        _getuser("-@all +@read +slowlog|reset"),
        _getuser("-@all +@read +get"),
        _getuser(flags=[b"on", b"allcommands"]),
        _getuser(selectors=[[b"commands", b"+@all"]]),
        _getuser(selectors=b"x"),
        _getuser(commands=None),
        _getuser("read"),
        _getuser(""),
        [b"flags"],
        None,
        b"OK",
    ],
)
def test_account_check_refuses(reply) -> None:
    problem = ri.account_problem(reply)
    assert problem is not None and problem.startswith("REDIS_ACCOUNT_NOT_READ_ONLY")
    assert CANARY not in problem


def test_account_refusal_sends_no_tool_command(fake) -> None:
    fake.getuser = _getuser("+@all")
    outcome = _execute(ri.SCAN_TOOL, {})
    assert outcome.reason == "SOURCE_FORBIDDEN", _view(outcome)
    assert "REDIS_ACCOUNT_NOT_READ_ONLY" in json.dumps(_view(outcome))
    assert fake.sent == [*HANDSHAKE, ["<close>"]]


# -- the command table ----------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["FLUSHALL"],
        ["SET", "a", "b"],
        ["GET", "a"],
        ["KEYS", "*"],
        ["CONFIG", "SET", "maxmemory", "0"],
        ["CLIENT", "KILL", "ID", "1"],
        ["ACL", "SETUSER", "x"],
        ["SLOWLOG", "RESET"],
        ["CONFIG"],
        [],
    ],
)
def test_send_refuses_commands_outside_the_table(argv) -> None:
    sent: list[Any] = []

    class Conn:
        def command(self, argv: Sequence[str]) -> Any:
            sent.append(argv)

        def cancel(self) -> None: ...

        def close(self) -> None: ...

    with pytest.raises(ri.RedisCommandRefused):
        ri._Wire(Conn())._send(argv)
    assert sent == []


def test_every_sent_command_is_in_the_table(fake) -> None:
    fake.keys = {
        "s": ("string", b"v"),
        "h": ("hash", {"a": b"1"}),
        "l": ("list", [b"1", b"2"]),
    }
    for tool, params in (
        (ri.INFO_TOOL, {"sections": "memory,clients"}),
        (ri.CONFIG_TOOL, {"pattern": "*"}),
        (ri.SCAN_TOOL, {"pattern": "*", "type": "hash"}),
        (ri.KEY_INFO_TOOL, {"key": "h"}),
        (ri.VALUE_TOOL, {"key": "s"}),
        (ri.VALUE_TOOL, {"key": "h"}),
        (ri.VALUE_TOOL, {"key": "l"}),
        (ri.SLOWLOG_TOOL, {}),
    ):
        fake.sent.clear()
        outcome = _execute(tool, params)
        assert outcome.status == "ok", (tool, _view(outcome))
        assert fake.sent[:4] == HANDSHAKE and fake.sent[-1] == ["<close>"]
        assert fake.sent.count(["<close>"]) == 1
        for argv in fake.sent[:-1]:
            assert ri._command_allowed(argv), argv
    assert fake.timeouts and max(fake.timeouts) <= 10.0


# -- tool shapes and redaction -----------------------------------------------------------


def test_config_denylist_redacts_even_empty_values(fake) -> None:
    view = _view(_execute(ri.CONFIG_TOOL, {}))
    rows = {row["name"]: row["value"] for row in view["content"]}
    assert rows["requirepass"] == "[REDACTED_CREDENTIAL]"
    assert rows["masteruser"] == "[REDACTED_CREDENTIAL]"
    # #783: Valkey's names for masteruser / masterauth
    assert rows["primaryuser"] == "[REDACTED_CREDENTIAL]"
    assert rows["primaryauth"] == "[REDACTED_CREDENTIAL]"
    assert rows["tls-key-file-pass"] == "[REDACTED_CREDENTIAL]"
    assert rows["maxmemory"] == "0"
    assert CANARY not in json.dumps(view)


def test_string_and_hash_values(fake) -> None:
    fake.keys = {
        "long": ("string", b"x" * 9000),
        "api_token": ("string", CANARY.encode()),
        "bin": ("string", b"\xff\x00"),
        "user:1": ("hash", {"name": b"ann", "password": CANARY.encode()}),
    }
    view = _view(_execute(ri.VALUE_TOOL, {"key": "long"}))
    assert ["GETRANGE", "long", "0", "8191"] in fake.sent
    assert len(view["content"][0]["value"]) == ri.MAX_VALUE_CHARS
    assert view["content"][0]["_truncated_fields"] == ["value"]
    view = _view(_execute(ri.VALUE_TOOL, {"key": "api_token"}))
    assert CANARY not in json.dumps(view)
    view = _view(_execute(ri.VALUE_TOOL, {"key": "bin"}))
    assert view["content"][0]["value"] == "0xff00"
    view = _view(_execute(ri.VALUE_TOOL, {"key": "user:1"}))
    assert {"name": "ann"} in view["content"] and CANARY not in json.dumps(view)
    assert not any(argv[0] in ("GET", "HGETALL") for argv in fake.sent)


def test_capped_string_keeps_text_cut_mid_character(fake) -> None:
    # #783: the 8 KiB read ends inside a 3-byte character; the partial
    # character is dropped so the value stays text (and visible to redaction)
    fake.keys = {
        "utf8": ("string", ("é" * 1000).encode() + b"a" * 6190 + "€".encode()),
        "blob": ("string", b"\xff" * 9000),
    }
    view = _view(_execute(ri.VALUE_TOOL, {"key": "utf8"}))
    row = view["content"][0]
    assert row["value"] == "é" * 1000 + "a" * (ri.MAX_VALUE_CHARS - 1000)
    assert row["_truncated_fields"] == ["value"]
    view = _view(_execute(ri.VALUE_TOOL, {"key": "blob"}))
    assert view["content"][0]["value"].startswith("0xffff")
    assert view["content"][0]["_truncated_fields"] == ["value"]


@pytest.mark.parametrize(
    "data, shown",
    [
        (b"ab\xe2\x82", "ab"),  # cut inside a 3-byte character
        (b"ab\xf0\x9f\x98", "ab"),  # cut inside a 4-byte character
        (b"ab\xc3", "ab"),
        ("ab€".encode(), "ab€"),
        (b"ab\xff", "0x6162ff"),  # invalid, not a cut character
        (b"\xffab\xe2\x82", "0xff6162e282"),
        (b"ab\xe2\x41", "0x6162e241"),
    ],
)
def test_prefix_value_drops_only_a_cut_trailing_character(data, shown) -> None:
    assert ri._prefix_value(data) == shown


def test_limit_marks_incomplete_only_past_it(fake) -> None:
    fake.keys = {"l": ("list", [str(i).encode() for i in range(3)])}
    full = _execute(ri.VALUE_TOOL, {"key": "l", "limit": 3})
    assert ["LRANGE", "l", "0", "3"] in fake.sent
    assert full.status == "ok" and not _view(full).get("incomplete_reason")
    cut = _execute(ri.VALUE_TOOL, {"key": "l", "limit": 2})
    assert len(_view(cut)["content"]) == 2 and _view(cut)["incomplete_reason"]


def test_scan_stops_after_the_iteration_budget(fake) -> None:
    fake.scan_forever = True
    view = _view(_execute(ri.SCAN_TOOL, {"pattern": "k*", "limit": 500}))
    scans = [argv for argv in fake.sent if argv[0] == "SCAN"]
    assert len(scans) == ri.MAX_SCAN_ITERATIONS
    assert scans[0] == ["SCAN", "0", "MATCH", "k*", "COUNT", "1000"]
    assert "50 iterations" in view["incomplete_reason"]


def test_slowlog_redacts_auth_arguments(fake) -> None:
    view = _view(_execute(ri.SLOWLOG_TOOL, {"limit": 1}))
    assert ["SLOWLOG", "GET", "2"] in fake.sent
    row = view["content"][0]
    assert row["command"][:2] == ["AUTH", "[REDACTED_CREDENTIAL]"]
    assert row["time"] == "2025-11-22T00:00:00Z"
    assert view["incomplete_reason"] and CANARY not in json.dumps(view)


def test_missing_key_is_no_data(fake) -> None:
    outcome = _execute(ri.KEY_INFO_TOOL, {"key": "absent"})
    assert outcome.status == "no_data"
    assert "KEY_NOT_FOUND" in json.dumps(_view(outcome))


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (ri.RedisReplyError("NOPERM no"), "SOURCE_FORBIDDEN"),
        (ri.RedisReplyError("NOAUTH Authentication required."), "SOURCE_FORBIDDEN"),
        (ri.RedisReplyError("MOVED 1 10.0.0.1:6379"), "INVALID_PARAMS"),
        (ri.RedisReplyError("LOADING loading"), "SOURCE_UNAVAILABLE"),
        (ri.RedisReplyError("BUSY script"), "SOURCE_UNAVAILABLE"),
        (ri.RedisReplyError("ERR " + CANARY), "SOURCE_ERROR"),
        (ConnectionResetError("reset"), "SOURCE_UNAVAILABLE"),
    ],
)
def test_reply_errors(fake, error, reason) -> None:
    fake.fail = {"SCAN": error}
    outcome = _execute(ri.SCAN_TOOL, {})
    assert outcome.reason == reason, _view(outcome)
    assert CANARY not in json.dumps(_view(outcome))


@pytest.mark.parametrize(
    ("fail", "code"),
    [
        ({"AUTH": ri.RedisReplyError("WRONGPASS invalid")}, "REDIS_AUTH_FAILED"),
        (
            {"AUTH": ri.RedisReplyError("ERR wrong number of arguments for 'auth'")},
            "REDIS_ACCOUNT_UNSUPPORTED",
        ),
        (
            {"ACL": ri.RedisReplyError("ERR unknown command 'ACL'")},
            "REDIS_ACCOUNT_UNSUPPORTED",
        ),
    ],
)
def test_handshake_errors(fake, fail, code) -> None:
    fake.fail = fail
    outcome = _execute(ri.SCAN_TOOL, {})
    assert outcome.reason == "SOURCE_FORBIDDEN" and code in json.dumps(_view(outcome))
    assert not any(argv[0] == "SCAN" for argv in fake.sent)


def test_bad_input_is_refused_before_connecting(fake) -> None:
    for tool, params in (
        (ri.SCAN_TOOL, {"limit": 501}),
        (ri.SCAN_TOOL, {"type": "json"}),
        (ri.SLOWLOG_TOOL, {"limit": 129}),
        (ri.KEY_INFO_TOOL, {"key": ""}),
        (ri.KEY_INFO_TOOL, {"key": "a\nb"}),
        (ri.VALUE_TOOL, {"key": "k" * 1025}),
        (ri.INFO_TOOL, {"sections": "secrets"}),
    ):
        outcome = _execute(tool, params)
        assert outcome.reason == "INVALID_PARAMS", (tool, params, _view(outcome))
    assert fake.sent == []


# -- configuration and profile ----------------------------------------------------------


@pytest.mark.parametrize(
    ("env", "code"),
    [
        ({ri.URL_ENV: URL}, "REDIS_CONFIG_MISSING"),
        ({ri.URL_ENV: URL, ri.USERNAME_ENV: "u"}, "REDIS_CONFIG_MISSING"),
        ({ri.USERNAME_ENV: "u"}, "REDIS_CONFIG_INVALID"),
        (
            {ri.URL_ENV: "rediss://h", ri.USERNAME_ENV: "u", ri.PASSWORD_ENV: "p"},
            "REDIS_CONFIG_INVALID",
        ),
        (
            {ri.URL_ENV: "redis://u:p@h", ri.USERNAME_ENV: "u", ri.PASSWORD_ENV: "p"},
            "REDIS_CONFIG_INVALID",
        ),
        (
            {ri.URL_ENV: "redis://h/0", ri.USERNAME_ENV: "u", ri.PASSWORD_ENV: "p"},
            "REDIS_CONFIG_INVALID",
        ),
        (
            {ri.URL_ENV: "redis://h?db=1", ri.USERNAME_ENV: "u", ri.PASSWORD_ENV: "p"},
            "REDIS_CONFIG_INVALID",
        ),
        (
            {ri.URL_ENV: "redis://h1,h2", ri.USERNAME_ENV: "u", ri.PASSWORD_ENV: "p"},
            "REDIS_CONFIG_INVALID",
        ),
        (
            {
                ri.URL_ENV: "redis://h",
                ri.USERNAME_ENV: "u",
                ri.PASSWORD_ENV: "p",
                ri.DATABASE_ENV: "x",
            },
            "REDIS_CONFIG_INVALID",
        ),
        (
            {
                ri.URL_ENV: "redis://h",
                ri.USERNAME_ENV: "u",
                ri.PASSWORD_ENV: "p",
                ri.TLS_ENV: "prefer",
            },
            "REDIS_CONFIG_INVALID",
        ),
        (
            {
                ri.URL_ENV: "redis://h",
                ri.USERNAME_ENV: "u",
                ri.PASSWORD_ENV: "p",
                ri.TLS_ENV: "disable",
                ri.CA_FILE_ENV: "/etc/hosts",
            },
            "REDIS_CONFIG_INVALID",
        ),
        (
            {
                ri.URL_ENV: "redis://127.0.0.1:5432",
                ri.USERNAME_ENV: "u",
                ri.PASSWORD_ENV: "p",
                "OPSPILOT_WORKER_DSN": "host=localhost port=5432",
            },
            "REDIS_TARGETS_OPSPILOT_DB",
        ),
    ],
)
def test_config_errors(env, code) -> None:
    with pytest.raises(ToolContractError, match=code):
        ri.RedisConfig.from_env(env)


def test_profile_and_lab_gate(fake) -> None:
    assert ext.tool_schema_revision() == "otel-demo-ext-b90c54386594"
    assert ext.REDIS_TOOL_SCHEMA_REVISION != ext.TOOL_SCHEMA_REVISION
    assert ext.REDIS_TOOL_SCHEMA_REVISION != ext.MONGODB_TOOL_SCHEMA_REVISION
    profile = select_profile(_env())
    assert profile.versions()["tool_schema_revision"] == ext.REDIS_TOOL_SCHEMA_REVISION
    face = profile.face(FakeClock(start=NOW))
    names = [s["function"]["name"] for s in face.tool_schemas]
    assert names[-6:] == list(ri.TOOL_NAMES)
    assert ext.REDIS_TOOL_NAMES[-6:] == ri.TOOL_NAMES
    assert "REDIS_NOT_LAB" in check_lab_target(
        {ri.URL_ENV: "redis://cache.example.com"}
    )
    assert "REDIS_NOT_LAB" not in check_lab_target({ri.URL_ENV: URL})
    # the web reads the address only
    web = select_profile({**_base_env(), ri.URL_ENV: URL})
    assert web.versions() == profile.versions()


# -- the RESP2 client against a local socket -------------------------------------------


def _serve(replies: list[bytes], hold: threading.Event | None = None):
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    received: list[bytes] = []

    def run() -> None:
        conn, _ = server.accept()
        with conn:
            for reply in replies:
                received.append(conn.recv(65536))
                conn.sendall(reply)
            if hold is not None:
                conn.recv(65536)
                hold.wait(5)
        server.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return server.getsockname()[1], received, thread


def test_socket_client_parses_resp2() -> None:
    port, received, thread = _serve(
        [b"+OK\r\n", b"*3\r\n$1\r\na\r\n:5\r\n*-1\r\n", b"-NOPERM nope\r\n"]
    )
    config = ri.RedisConfig(
        url=f"redis://127.0.0.1:{port}", username="u", password="p", tls="disable"
    )
    conn = ri.default_connector(config, 2.0)
    try:
        assert conn.command(["PING"]) == "OK"
        assert conn.command(["X"]) == [b"a", 5, None]
        with pytest.raises(ri.RedisReplyError) as error:
            conn.command(["Y"])
        assert error.value.word == "NOPERM"
    finally:
        conn.close()
    thread.join(2)
    assert received[0] == b"*1\r\n$4\r\nPING\r\n"


def test_watchdog_closes_the_socket_at_the_deadline() -> None:
    hold = threading.Event()
    replies = [
        b"+OK\r\n",
        b"+OK\r\n",
        b"$11\r\nopspilot_ro\r\n",
        b"*" + str(len(_getuser())).encode() + b"\r\n",
    ]
    # a GETUSER reply that never finishes: the server holds the socket
    port, _, thread = _serve(replies, hold)
    config = ri.RedisConfig(
        url=f"redis://127.0.0.1:{port}", username="u", password="p", tls="disable"
    )
    transport = ri.RedisTransport(config, request_timeout_seconds=1.5)
    from opspilot.tools.executor import TransportTimeout

    with pytest.raises(TransportTimeout):
        transport._session(lambda send, legacy: pytest.fail("no tool command"))
    hold.set()
    thread.join(2)


def test_refused_port_is_unavailable() -> None:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    config = ri.RedisConfig(
        url=f"redis://127.0.0.1:{port}", username="u", password="p", tls="disable"
    )
    from opspilot.tools.executor import TransportUnavailable

    with pytest.raises(TransportUnavailable):
        ri.RedisTransport(config)._session(lambda send, legacy: pytest.fail("no"))


def test_stuck_dns_is_unavailable_within_the_connect_timeout(monkeypatch) -> None:
    # #783: the lookup runs under the connect timeout, not before the watchdog
    release = threading.Event()
    real = socket.getaddrinfo

    def stuck(host: Any, *args: Any, **kwargs: Any) -> Any:
        if host == "redis.stuck.example":
            release.wait(10)
            raise socket.gaierror("released")
        return real(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", stuck)
    config = ri.RedisConfig(
        url="redis://redis.stuck.example:6379",
        username="u",
        password="p",
        tls="disable",
    )
    from opspilot.tools.executor import TransportUnavailable

    started = time.monotonic()
    try:
        with pytest.raises(TransportUnavailable) as error:
            ri.RedisTransport(config, request_timeout_seconds=3.5)._session(
                lambda send, legacy: pytest.fail("no tool command")
            )
        assert time.monotonic() - started < 2.5  # connect timeout is 1.5 s
        assert error.value.args == ("REDIS_UNAVAILABLE",)
    finally:
        release.set()
