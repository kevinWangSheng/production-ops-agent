"""Redis tool follow-ups from the #782 review (#783 items 1-3; rulings
B1-D10..D12 in ``docs/tasks/2026-11-22-next-batches-b1-prereview.md``).

Written by the independent test author without the implementation. Every call
goes through the shipped worker to ``FakeRedis`` (``tests/redis_support.py``)
with the default connector, so the real socket client and transport run.

* D10: CONFIG GET never returns the value of ``primaryuser`` (Valkey's name
  for ``masteruser``) or ``primaryauth``.
* D11: host name resolution is bounded by the connect timeout; a hanging DNS
  lookup ends as the connect-failure outcome (``REDIS_UNAVAILABLE`` inside the
  transport, ``SOURCE_UNAVAILABLE`` on the outcome, like a refused connect).
* D12: a text value cut by the 8 KiB read inside a multi-byte character is
  returned as text (marked truncated), so redaction sees it; a binary value
  still comes back as ``0x`` hex.
"""

from __future__ import annotations

import socket
import threading
import time

import pytest

from tests.redis_support import (
    CANARY,
    CONFIG_TOOL,
    GET_VALUE_TOOL,
    INFO_TOOL,
    free_port,
    make_tools,
)
from tests.safety.gateway_support import everything_seen


@pytest.fixture(autouse=True)
def _environment(tmp_path, monkeypatch):
    import os

    for name in list(os.environ):
        if name.startswith("OPSPILOT_REDIS"):
            monkeypatch.delenv(name, raising=False)
    ca_file = tmp_path / "synthetic-k8s-ca.pem"
    ca_file.write_text("synthetic ca placeholder\n")
    monkeypatch.setenv("C6C_TEST_K8S_CA_FILE", str(ca_file))


# -- D10: CONFIG GET denylist ---------------------------------------------------------


def test_config_get_hides_primaryuser_and_primaryauth(monkeypatch):
    tools = make_tools(monkeypatch)
    tools.server.config.update(
        {"primaryuser": CANARY + "4", "primaryauth": CANARY + "5"}
    )
    outcome = tools.run(CONFIG_TOOL, {"pattern": "*"})
    assert outcome.status == "ok", outcome.model_view
    rows = {row["name"]: row["value"] for row in outcome.model_view["content"]}
    assert rows["primaryuser"] == "[REDACTED_CREDENTIAL]"
    assert rows["primaryauth"] == "[REDACTED_CREDENTIAL]"
    assert rows["maxmemory-policy"] == "noeviction"  # positive control
    assert CANARY not in everything_seen(outcome, tools.sinks[-1])


# -- D11: DNS bounded by the connect timeout -------------------------------------------

HANGING_HOST = "redis-dns-hangs.example.test"
# request timeout 3 s -> connect timeout max(1, min(3 - 2, 10)) = 1 s
REQUEST_TIMEOUT = 3.0
# a hang shows up red after this long instead of blocking the suite
HARD_LIMIT = 8.0


def _run_bounded(tools, tool):
    box: dict[str, object] = {}

    def target() -> None:
        try:
            box["outcome"] = tools.run(tool)
        except BaseException as error:  # noqa: BLE001 - reported below
            box["error"] = error

    started = time.monotonic()
    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(HARD_LIMIT)
    elapsed = time.monotonic() - started
    assert not thread.is_alive(), f"the call still hangs after {HARD_LIMIT}s"
    assert "error" not in box, box.get("error")
    return box["outcome"], elapsed


def test_hanging_dns_fails_like_a_refused_connect_within_the_connect_timeout(
    monkeypatch,
):
    # control: a refused connect on loopback is the unavailable outcome
    refused = make_tools(
        monkeypatch,
        request_timeout_seconds=REQUEST_TIMEOUT,
        OPSPILOT_REDIS_URL=f"redis://127.0.0.1:{free_port()}",
    )
    control, _ = _run_bounded(refused, INFO_TOOL)
    assert (control.status, control.reason) == ("error", "SOURCE_UNAVAILABLE")

    release = threading.Event()
    real = socket.getaddrinfo
    lookups: list[str] = []

    def hanging(host, *args, **kwargs):
        if host == HANGING_HOST:
            lookups.append(host)
            release.wait(60)
            raise socket.gaierror(socket.EAI_AGAIN, "released by the test")
        return real(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", hanging)
    try:
        tools = make_tools(
            monkeypatch,
            request_timeout_seconds=REQUEST_TIMEOUT,
            OPSPILOT_REDIS_URL=f"redis://{HANGING_HOST}:6379",
        )
        outcome, elapsed = _run_bounded(tools, INFO_TOOL)
    finally:
        release.set()
    assert lookups, "the hostname was never resolved"
    assert elapsed < REQUEST_TIMEOUT, elapsed  # ~1 s connect timeout, not 30 s
    assert (outcome.status, outcome.reason) == (control.status, control.reason)


# -- D12: 8 KiB cut inside a multi-byte character ------------------------------------

READ_BYTES = 8192


def _text_cut_mid_char(char: str, prefix: bytes) -> bytes:
    """``prefix`` then ``char`` repeated, past 8 KiB, with byte 8192 landing
    strictly inside one ``char``."""
    width = len(char.encode())
    pad = 0
    while (READ_BYTES - len(prefix) - pad) % width == 0:
        pad += 1
    body = prefix + b"x" * pad + char.encode() * (READ_BYTES // width + 10)
    assert len(body) > READ_BYTES
    assert (READ_BYTES - len(prefix) - pad) % width != 0
    return body


@pytest.mark.parametrize("char", ["é", "€", "\U0001f600"])
def test_long_text_cut_mid_character_is_text_and_redacted(monkeypatch, char):
    tools = make_tools(monkeypatch)
    secret = b"password=" + CANARY.encode() + b" "
    tools.server.data["note:1"] = ("string", _text_cut_mid_char(char, secret))
    tools.server.data["note:short"] = ("string", secret + char.encode() * 5)
    # control: the same assignment in a short value is redacted
    short = tools.run(GET_VALUE_TOOL, {"key": "note:short"})
    assert short.status == "ok", short.model_view
    assert CANARY not in everything_seen(short, tools.sinks[-1])
    outcome = tools.run(GET_VALUE_TOOL, {"key": "note:1"})
    assert outcome.status == "ok", outcome.model_view
    row = outcome.model_view["content"][0]
    value = row["value"]
    assert isinstance(value, str) and not value.startswith("0x"), value[:80]
    assert char * 5 in value
    assert row["_truncated_fields"] == ["value"]
    assert CANARY not in everything_seen(outcome, tools.sinks[-1])


def test_long_binary_value_is_still_hex(monkeypatch):
    tools = make_tools(monkeypatch)
    tools.server.data["blob:1"] = ("string", b"\xff\xfe\x00" * 3000)
    outcome = tools.run(GET_VALUE_TOOL, {"key": "blob:1"})
    assert outcome.status == "ok", outcome.model_view
    row = outcome.model_view["content"][0]
    assert row["value"].startswith("0xfffe00"), row["value"][:80]
    assert row["_truncated_fields"] == ["value"]
