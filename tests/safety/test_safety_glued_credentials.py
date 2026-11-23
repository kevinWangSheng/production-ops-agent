"""Safety: a credential right after a short or glued credential value is
still redacted (#803; ruling 2026-11-22: fail closed, fix direction left to
the implementer).

When a credential's value ends early -- at whitespace, at a separator, or by
running into the next key's quotes -- the pair that follows must still be
examined. Each leak case asserts only what the ruling fixes, not a chosen
output shape:

* no canary value survives anywhere in the output;
* redacting the output again changes nothing;
* ordinary text before and after the shape stays visible.

The no-false-positive cases pin exact output: non-credential pairs next to a
credential keep their values. Shapes marked ``flag`` use a credential-named
CLI flag, whose own value is redacted only once #802 lands; they are the
#802 review's "flag value followed by a glued ``name :value``" shapes.

Values are synthetic, low entropy and built at runtime (no scanner-visible
literal). Mutation entry for this guard: not written here -- an entry's
``old`` text must quote the implementation, which does not exist yet; the
implementer adds M60.
"""

from __future__ import annotations

import json
import time

import pytest

from opspilot.redaction import REDACTED_CREDENTIAL, redact_credentials, redact_payload

#: Canary values; each appears only as a credential value in the inputs.
A = "".join(("qz", "A", "71"))
B = "".join(("qz", "B", "82"))
C = "".join(("qz", "C", "93"))
CANARIES = (A, B, C)

#: Ordinary text around every leak shape; it must stay visible.
BEFORE = "level=info user=alice "
AFTER = " status=200 path=/login"

SEPARATORS = ["&", "|", ";", ",", " "]


def _leak_cases() -> list[pytest.param]:
    cases = [
        # Issue #803 table.
        pytest.param(f'token: "password": "{B}"', id="yaml-name-then-quoted-pair"),
        pytest.param(f'"password":{A}&"secret": "{B}"', id="issue-amp-quoted-pair"),
        pytest.param(f'"sessionid":{A}&"secret": "{B}"', id="issue-sessionid"),
        # Quote-glued values: the first value's closing quote touches the
        # next name.
        pytest.param(f'password: "{A}"token: "{B}"', id="quote-glued-yaml"),
        pytest.param(f'"password":"{A}""token":"{B}"', id="quote-glued-json"),
        pytest.param(f'"password":{A}"token":"{B}"', id="bare-then-quoted-name"),
        pytest.param(f'"password":"{A}"&"secret":"{B}"', id="quoted-amp-quoted"),
        # Whitespace-terminated value followed by a quoted credential pair.
        pytest.param(f'password: {A} "secret": "{B}"', id="ws-then-quoted-pair"),
        pytest.param(f'password={A} "token":"{B}"', id="ws-eq-then-quoted-pair"),
        # YAML-ish ``a: b c: d`` chains with credential names.
        pytest.param(f"password: {A} token: {B}", id="yaml-chain-2"),
        pytest.param(f"password: {A} token: {B} api_key: {C}", id="yaml-chain-3"),
        pytest.param(f"token: password: {B}", id="yaml-name-then-bare-pair"),
        # ``name :value`` (space before the colon).
        pytest.param(f"password :{A} token :{B}", id="space-colon-chain"),
        pytest.param(f"password={A}|db-password :{B}", id="pipe-then-space-colon"),
        pytest.param(f'"password":{A}&token :{B}&"secret": "{C}"', id="mixed-chain-3"),
        # Credential-named CLI flag value followed by a glued ``name :value``.
        pytest.param(f"--password={A}|token :{B}", id="flag-eq-pipe-space-colon"),
        pytest.param(f"--password {A}|token :{B}", id="flag-ws-pipe-space-colon"),
        pytest.param(f"--db-password={A}&secret :{B}", id="flag-amp-space-colon"),
    ]
    for sep in SEPARATORS:
        name = {" ": "space"}.get(sep, sep)
        cases += [
            pytest.param(
                f'"password":{A}{sep}"secret": "{B}"', id=f"quoted-pair[{name}]"
            ),
            pytest.param(f"password={A}{sep}token :{B}", id=f"space-colon[{name}]"),
            pytest.param(
                f'"password":{A}{sep}"token":{B}{sep}"secret": "{C}"',
                id=f"chain-3[{name}]",
            ),
            pytest.param(f"secret={A}{sep}password={B}", id=f"eq-chain-2[{name}]"),
        ]
    return cases


def _assert_fail_closed(text: str, once: str) -> None:
    leaked = [c for c in CANARIES if c in once]
    assert not leaked, f"{leaked} visible in {once!r}"
    assert REDACTED_CREDENTIAL in once
    assert redact_credentials(once) == once, "not idempotent"
    assert once.startswith(BEFORE), once
    assert once.endswith(AFTER), once


@pytest.mark.parametrize("shape", _leak_cases())
def test_credential_after_short_or_glued_value_is_redacted(shape: str) -> None:
    text = BEFORE + shape + AFTER
    _assert_fail_closed(text, redact_credentials(text))


@pytest.mark.parametrize(
    "shape",
    [
        pytest.param(f'"password":{A}&"secret": "{B}"', id="amp-quoted-pair"),
        pytest.param(f"password={A}|db-password :{B}", id="pipe-space-colon"),
    ],
)
def test_payload_string_with_glued_credentials_is_redacted(shape: str) -> None:
    payload = {"log": BEFORE + shape + AFTER, "user": "alice", "items": [shape]}
    once = redact_payload(payload)
    dumped = json.dumps(once)
    assert not [c for c in CANARIES if c in dumped], dumped
    assert redact_payload(once) == once
    assert once["user"] == "alice"
    assert once["log"].startswith(BEFORE) and once["log"].endswith(AFTER)


# -- no false positives ------------------------------------------------------

R = REDACTED_CREDENTIAL

VISIBLE = [
    # Already-redacted text keeps the following ordinary pair.
    (f"password: {R} user: alice", f"password: {R} user: alice"),
    (f'"password": "{R}", "user": "alice"', f'"password": "{R}", "user": "alice"'),
    (f"password={R}&user=alice", f"password={R}&user=alice"),
    # A credential followed by ordinary pairs.
    (f"password: {A} user: alice", f"password: {R} user: alice"),
    (f"password={A}&user=alice", f"password={R}&user=alice"),
    (f"password: {A}; user: alice", f"password: {R}; user: alice"),
    (f'"password":"{A}","user":"alice"', f'"password":"{R}","user":"alice"'),
    # Ordinary pairs glued like the leak shapes stay as they are.
    ('"user":alice&"host": "db1"', '"user":alice&"host": "db1"'),
    ("user: alice host: db1 port: 5432", "user: alice host: db1 port: 5432"),
    ("mode :fast level :info", "mode :fast level :info"),
]


@pytest.mark.parametrize(("text", "expected"), VISIBLE)
def test_ordinary_pairs_next_to_a_credential_stay_visible(text, expected):
    once = redact_credentials(text)
    assert once == expected
    assert redact_credentials(once) == once


def test_json_object_keeps_non_credential_values():
    obj = {"user": "alice", "password": A, "host": "db1", "port": 5432, "tls": True}
    text = json.dumps(obj)
    once = redact_credentials(text)
    assert json.loads(once) == {**obj, "password": R}
    assert redact_credentials(once) == once

    redacted = redact_payload(obj)
    assert redacted["password"] != A and A not in json.dumps(redacted)
    assert {k: v for k, v in redacted.items() if k != "password"} == {
        k: v for k, v in obj.items() if k != "password"
    }


# -- linear time (convention: tests/test_redaction_linear_time.py) -----------

SIZE = 512 * 1024
# A linear pass is well under a second; the bound leaves room for slow CI.
BOUND_SECONDS = 5.0
UNITS = ['"password":x&', '"secret":x|"', "token :x;", 'password: "x"token: ']


@pytest.mark.serial
@pytest.mark.parametrize("unit", UNITS)
def test_long_glued_chain_is_linear_and_redacted(unit: str) -> None:
    text = BEFORE + unit * (SIZE // len(unit)) + f'"secret": "{B}"' + AFTER
    started = time.perf_counter()
    once = redact_credentials(text)
    elapsed = time.perf_counter() - started
    assert elapsed < BOUND_SECONDS, f"{unit!r} x {SIZE} bytes took {elapsed:.1f}s"
    assert B not in once
    assert once.startswith(BEFORE) and once.endswith(AFTER)
