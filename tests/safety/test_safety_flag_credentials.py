"""Safety: a credential passed as a command-line flag is redacted (#802,
user ruling R2-1 in docs/tasks/2026-11-22-next-batches.md).

* ``--name=value`` and ``--name value`` (also single-dash ``-name``) hide the
  value when ``name`` matches the existing credential-name rule
  (``password``, ``db-password``, ``api-key``, ``token``, ...).
* Space-separated: the next token is hidden unless it starts with ``-``
  (fail closed; occasional over-hiding is accepted). ``--password --verbose``
  hides nothing and keeps ``--verbose``.
* In a structured ``args``/``command`` list, the flag and its value are
  separate elements: the element after a credential flag is the value.
* A quoted value (``--password "a b"``) is hidden whole, as the existing
  quoted-value rule does for ``password="a b"``.
* Ordinary flags stay visible, and so does a count under a ``*tokens`` name
  (``--max-tokens 4000``), by the existing #366 count exemption.

The flag name stays readable; only the value is replaced. Values are
synthetic, low entropy.
"""

from __future__ import annotations

import json
import re

import pytest

from opspilot.redaction import REDACTED_CREDENTIAL, redact_credentials, redact_payload
from tests.safety.gateway_support import everything_seen, gateway

FAKE = "-".join(("canary", "flag", "71"))
FAKE2 = "-".join(("canary", "flag", "72"))

# -- one command string ------------------------------------------------------

HIDDEN_TEXT = [
    "--password={v}",
    "--db-password={v}",
    "--api-key={v}",
    "--token={v}",
    "--password {v}",
    "--db-password {v}",
    "--api-key {v}",
    "-password {v}",
    "-token={v}",
    # inside a longer command line, with flags on both sides
    "exec /app/orders --port 8080 --db-password {v} --log-level=debug",
    "sh -c 'redis-cli -h cache --token {v} ping'",
    # quoted value: hidden whole (both words), fail closed
    '--password "{v} {v2}"',
    "--password='{v} {v2}'",
    # JSON text of a Pod spec (e.g. last-applied-configuration annotation)
    '{{"args":["--password={v}"]}}',
    '{{"args":["--password","{v}"]}}',
]


@pytest.mark.parametrize("template", HIDDEN_TEXT)
def test_flag_credential_values_are_hidden_in_text(template):
    text = template.format(v=FAKE, v2=FAKE2)
    redacted = redact_credentials(text)
    assert FAKE not in redacted and FAKE2 not in redacted, redacted
    assert REDACTED_CREDENTIAL in redacted
    # The flag name itself stays readable for the operator.
    flag = re.search(r"-{1,2}[a-z-]+(?=[=\s\"',]*\{v\})", template).group()
    assert flag in redacted, (flag, redacted)
    assert redact_credentials(redacted) == redacted


VISIBLE_TEXT = [
    "--port 8080",
    "--port=8080",
    "--log-level=debug",
    "--log-level debug",
    "--config /etc/orders/app.yaml",
    "--author alice",
    "--session-timeout 30",
    "--max-tokens 4000",
    "--max-tokens=4000",
    "-v --replicas 3",
    "exec /app/orders --port 8080 --log-level=info --workers 4",
]


@pytest.mark.parametrize("text", VISIBLE_TEXT)
def test_ordinary_flags_stay_visible(text):
    assert redact_credentials(text) == text


def test_flag_followed_by_a_flag_hides_nothing():
    # The next token starts with ``-``: it is a flag, not the value.
    text = "--password --verbose --port 8080"
    assert redact_credentials(text) == text


# -- structured args / command lists -----------------------------------------


def test_container_args_and_command_lists():
    container = {
        "name": "orders",
        "command": ["sh", "-c", f"exec /app/orders --db-password {FAKE} --port 8080"],
        "args": [
            "--password",
            FAKE,
            "--port",
            "8080",
            f"--api-key={FAKE}",
            "--token",
            "--verbose",
            "-password",
            FAKE2,
            "--max-tokens",
            "4000",
        ],
    }
    out = redact_payload({"spec": {"containers": [container]}})
    got = out["spec"]["containers"][0]
    assert FAKE not in json.dumps(got) and FAKE2 not in json.dumps(got)
    args = got["args"]
    assert len(args) == len(container["args"])
    assert args[0] == "--password" and args[1] == REDACTED_CREDENTIAL
    assert args[2:4] == ["--port", "8080"]
    assert args[4].startswith("--api-key")
    # ``--token`` followed by a flag: that flag stays.
    assert args[5:7] == ["--token", "--verbose"]
    assert args[7] == "-password" and args[8] == REDACTED_CREDENTIAL
    assert args[9:] == ["--max-tokens", "4000"]
    command = got["command"]
    assert command[:2] == ["sh", "-c"]
    assert "--db-password" in command[2] and "--port 8080" in command[2]


def test_ordinary_args_list_is_returned_unchanged():
    args = ["--port", "8080", "--log-level", "debug", "-v", "--author", "alice"]
    payload = {"args": args}
    assert redact_payload(payload) is payload


# -- end to end: what the model sees -----------------------------------------


def test_flag_credentials_never_reach_the_model(monkeypatch, tmp_path):
    pod = {
        "metadata": {"name": "orders-0", "namespace": "otel-demo"},
        "spec": {
            "containers": [
                {
                    "name": "orders",
                    "args": ["--db-password", FAKE, "--port", "8080"],
                    "command": ["sh", "-c", f"orders --token={FAKE2} --workers 4"],
                }
            ]
        },
    }
    body = json.dumps({"kind": "PodList", "items": [pod]}).encode()
    gw = gateway(monkeypatch, tmp_path, body=body)
    outcome = gw.call("kubernetes_jq_query", {"kind": "pods", "jq_expr": ".items[]"})
    assert gw.source.requests, "the call must reach the source"
    assert outcome.status == "ok", outcome.model_view
    seen = everything_seen(outcome, gw.evidence)
    assert FAKE not in seen and FAKE2 not in seen
    for visible in ("--db-password", "8080", "--workers 4"):
        assert visible in seen, visible
