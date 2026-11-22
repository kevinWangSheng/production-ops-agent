"""Safety guards of the read-only Redis/Valkey tools (#773, R3/R4/R9).

Written by the independent test author from the #773 contract, without
reading the implementation. Each test guards one layer of the read-only and
credential promise and is the kill target of one R18 mutation
(``tests/mutation/mutations.py``):

* the command allowlist asserted in the single ``_send(argv)`` (M44);
* the account check ``account_problem`` (M45);
* the tool-side CONFIG GET denylist (M46): ``requirepass``, ``masterauth``
  and ``masteruser`` are names the shared redaction does not flag, so only
  the denylist hides them.

Calls go through the shipped worker to ``FakeRedis``, a recording RESP2
server on 127.0.0.1 (``tests/redis_support.py``).
"""

from __future__ import annotations

import inspect

import pytest

from tests.redis_support import (
    ALLOWED,
    CANARY,
    CONFIG_TOOL,
    GET_VALUE_TOOL,
    INFO_TOOL,
    LAB_GRANT,
    getuser_reply,
    make_tools,
    shape,
)
from tests.safety.gateway_support import everything_seen

#: Commands R3 says are never sent, one argv each.
FORBIDDEN_ARGV = (
    ("FLUSHALL",),
    ("FLUSHDB",),
    ("SET", "orders:1", "x"),
    ("DEL", "orders:1"),
    ("GET", "orders:1"),
    ("HGETALL", "cart:9"),
    ("SMEMBERS", "s"),
    ("KEYS", "*"),
    ("GETDEL", "orders:1"),
    ("CONFIG", "SET", "save", ""),
    ("CONFIG", "REWRITE"),
    ("CONFIG", "RESETSTAT"),
    ("SLOWLOG", "RESET"),
    ("ACL", "SETUSER", "x", "on"),
    ("ACL", "LOG"),
    ("CLIENT", "KILL", "ID", "1"),
    ("CLIENT", "LIST"),
    ("EVAL", "return 1", "0"),
    ("SCRIPT", "FLUSH"),
    ("MODULE", "LIST"),
    ("DEBUG", "SLEEP", "0"),
    ("MONITOR",),
    ("REPLICAOF", "NO", "ONE"),
    ("SHUTDOWN",),
    ("DUMP", "orders:1"),
    ("PUBLISH", "c", "m"),
    ("BLPOP", "l", "0"),
)


@pytest.fixture(autouse=True)
def _environment(tmp_path, monkeypatch):
    import os

    for name in list(os.environ):
        if name.startswith("OPSPILOT_REDIS"):
            monkeypatch.delenv(name, raising=False)
    ca_file = tmp_path / "synthetic-k8s-ca.pem"
    ca_file.write_text("synthetic ca placeholder\n")
    monkeypatch.setenv("C6C_TEST_K8S_CA_FILE", str(ca_file))


def _send_owner():
    """The class whose ``_send`` writes every command (R3).

    # ASSUMPTION: the contract fixes the name ``_send(argv)``; it is a method
    # of one class in ``opspilot.tools.redis_inspect``.
    """
    from opspilot.tools import redis_inspect

    owners = [
        obj
        for _, obj in inspect.getmembers(redis_inspect, inspect.isclass)
        if obj.__module__ == redis_inspect.__name__ and "_send" in vars(obj)
    ]
    assert len(owners) == 1, owners
    return owners[0]


def _like(argv, words: tuple[str, ...]):
    """``words`` in the container and element type ``argv`` uses."""
    first = argv[0] if argv else ""
    items = [w.encode() for w in words] if isinstance(first, bytes) else list(words)
    return tuple(items) if isinstance(argv, tuple) else items


def test_send_refuses_every_command_outside_the_allowlist(monkeypatch):
    owner = _send_owner()
    original = owner._send
    outcomes: list[tuple[tuple[str, ...], str]] = []

    def probing(self, argv, *args, **kwargs):
        if not outcomes:
            for words in FORBIDDEN_ARGV:
                try:
                    original(self, _like(argv, words), *args, **kwargs)
                except Exception:  # noqa: BLE001 - any refusal will do
                    outcomes.append((words, "refused"))
                else:
                    outcomes.append((words, "sent"))
        return original(self, argv, *args, **kwargs)

    monkeypatch.setattr(owner, "_send", probing)
    tools = make_tools(monkeypatch)
    tools.run(INFO_TOOL)
    assert len(outcomes) == len(FORBIDDEN_ARGV), "_send was never called"
    assert [w for w, result in outcomes if result != "refused"] == []
    on_wire = {shape(c) for c in tools.server.commands}
    assert on_wire <= ALLOWED, on_wire - ALLOWED


@pytest.mark.parametrize("extra", ["+@all", "+@write", "+config|set", "+flushall"])
def test_a_write_capable_account_is_refused_before_any_tool_command(monkeypatch, extra):
    tools = make_tools(monkeypatch)
    tools.server.getuser = getuser_reply(f"{LAB_GRANT} {extra}")
    outcome = tools.run(GET_VALUE_TOOL, {"key": "orders:1"})
    assert (outcome.status, outcome.reason) == ("error", "SOURCE_FORBIDDEN")
    assert str((outcome.model_view or {}).get("notes", [""])[0]).startswith(
        "REDIS_ACCOUNT_NOT_READ_ONLY"
    )
    assert tools.server.tool_commands() == [], tools.server.commands


def test_config_get_hides_credential_parameters_the_shared_policy_misses(
    monkeypatch,
):
    tools = make_tools(monkeypatch)
    tools.server.config.update(
        {
            "requirepass": CANARY + "1",
            "masterauth": CANARY + "2",
            "masteruser": CANARY + "3",
        }
    )
    outcome = tools.run(CONFIG_TOOL, {"pattern": "*"})
    assert outcome.status == "ok", outcome.model_view
    seen = everything_seen(outcome, tools.sinks[-1])
    assert CANARY not in seen
    assert "[REDACTED_CREDENTIAL]" in seen
    assert "noeviction" in seen  # positive control: other parameters shown
