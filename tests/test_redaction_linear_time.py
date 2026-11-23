"""Independent tests for issue #365: credential redaction stays linear.

``redact_credentials`` promises a single linear pass over pathological input
(512 KiB). Whitespace-free ``name:value`` runs such as ``a:b`` repeated
(``a:ba:ba:b...``) used to take time quadratic in their length. These tests
drive only the public entry points and check output bytes plus a generous
wall-clock bound; they do not inspect which internal rule does the work.
"""

from __future__ import annotations

import time

import pytest

from opspilot.redaction import (
    REDACTED_CREDENTIAL,
    redact_credentials,
    redact_payload,
)

SIZE = 512 * 1024
# A correct linear pass takes well under a second here; the quadratic one
# took about 150 s. The bound leaves room for a slow, busy CI runner.
BOUND_SECONDS = 5.0

# Issue #365 shapes (``a:b``, ``/a:b``, ``ua:b``, ``fa:b`` were quadratic on
# main); the quoted ``"a:b"`` form is kept as a guard against regressing it.
SHAPES = ["a:b", "/a:b", "ua:b", "fa:b", '"a:b"']

SECRET = '"password":"hunter2"'
REDACTED_SECRET = f'"password":"{REDACTED_CREDENTIAL}"'


def _run(unit: str) -> str:
    return unit * (SIZE // len(unit))


def _short_id(value: str) -> str:
    return value if len(value) <= 32 else f"{value[:8]}...len{len(value)}"


def _timed(fn, arg):
    started = time.perf_counter()
    result = fn(arg)
    return result, time.perf_counter() - started


@pytest.mark.serial
@pytest.mark.parametrize("unit", SHAPES)
def test_whitespace_free_name_value_run_is_linear(unit: str) -> None:
    # The credential sits right after the run, so the pass must still reach
    # the end of the input and redact it.
    text = _run(unit) + SECRET
    redacted, elapsed = _timed(redact_credentials, text)
    assert elapsed < BOUND_SECONDS, f"{unit!r} x {SIZE} bytes took {elapsed:.1f}s"
    assert redacted == _run(unit) + REDACTED_SECRET


@pytest.mark.serial
def test_payload_string_with_name_value_run_is_linear() -> None:
    payload = {"log": _run("a:b") + " " + SECRET}
    redacted, elapsed = _timed(redact_payload, payload)
    assert elapsed < BOUND_SECONDS, f"payload took {elapsed:.1f}s"
    assert redacted == {"log": _run("a:b") + " " + REDACTED_SECRET}


# #802 review of #817: many credential flags inside one ``# comment`` line,
# then a long run of YAML bullets or blank lines before the value. Each flag
# resumes after the same comment; re-walking the run per flag was quadratic
# (about 5 s at 64 KiB).
COMMENT_FLAG_TAILS = ["- \n", " \n"]


@pytest.mark.serial
@pytest.mark.parametrize("tail", COMMENT_FLAG_TAILS, ids=repr)
def test_flags_in_a_comment_before_a_long_separator_are_linear(tail: str) -> None:
    head = "--password # " * (SIZE // 2 // len("--password # "))
    run = tail * (SIZE // 2 // len(tail))
    text = head + "\n" + run + "hunter2"
    redacted, elapsed = _timed(redact_payload, {"log": text})
    assert elapsed < BOUND_SECONDS, f"{tail!r} took {elapsed:.1f}s"
    assert redacted == {"log": head + "\n" + run + REDACTED_CREDENTIAL}


# #803: a credential pair glued to a credential value is read by walking
# back from its separator to the name; that walk stays inside the span just
# read, so a long chain of glued pairs, a long glued name, and a long run
# that ends in a separator but no credential name are each read once.
# Each run ends in a glued pair whose value is ``hunter2``; in the last two
# the glued name is not a credential, so that value stays visible.
GLUED_RUNS = [
    ("password=x|", "token :x|", "token :hunter2", True),
    ('"password":x&', '"secret":x&', '"secret": "hunter2"', True),
    ("password=", "db-token-", " :hunter2", True),
    ("password=", "ab-", " :hunter2", False),
    ("password=", "a:", " :hunter2", False),
]


@pytest.mark.serial
@pytest.mark.parametrize(("head", "unit", "tail", "hidden"), GLUED_RUNS, ids=repr)
def test_glued_credential_pairs_are_linear(
    head: str, unit: str, tail: str, hidden: bool
) -> None:
    text = head + _run(unit) + tail + " " + SECRET
    redacted, elapsed = _timed(redact_credentials, text)
    assert elapsed < BOUND_SECONDS, f"{unit!r} took {elapsed:.1f}s"
    assert ("hunter2" not in redacted) is hidden
    assert redacted.endswith(REDACTED_SECRET)


# Review of #821: long glued runs that end in a credential flag or an auth
# scheme word were re-walked once per pair (quadratic, ~20 s at 4k pairs).
GLUED_FALLBACK_RUNS = [
    ("password=x|token :", "--password=hunter2"),
    ("password=x|token :", "x|bearer hunter2"),
    ('"password":x&"secret":', "--password=hunter2"),
    ('token: "password": ', "--password=hunter2"),
    ("token=a|token= ", "--password=hunter2"),
    ("token=a|token= ", "x|Bearer hunter2"),
]


@pytest.mark.serial
@pytest.mark.parametrize(("unit", "tail"), GLUED_FALLBACK_RUNS, ids=repr)
def test_glued_runs_ending_in_a_flag_or_scheme_are_linear(unit: str, tail: str) -> None:
    text = _run(unit) + tail
    redacted, elapsed = _timed(redact_credentials, text)
    assert elapsed < BOUND_SECONDS, f"{unit!r} took {elapsed:.1f}s"
    assert "hunter2" not in redacted


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('"password":"hunter2"', f'"password":"{REDACTED_CREDENTIAL}"'),
        ('{"token": "abc123"}', f'{{"token": "{REDACTED_CREDENTIAL}"}}'),
        ('"api_key":12345', f'"api_key":{REDACTED_CREDENTIAL}'),
        (
            'x "password": "s3cr3t", y',
            f'x "password": "{REDACTED_CREDENTIAL}", y',
        ),
        ("password=hunter2", f"password={REDACTED_CREDENTIAL}"),
        ("key: value", "key: value"),
        ("author: bob", "author: bob"),
        ("image: ghcr.io/x:1.11.1", "image: ghcr.io/x:1.11.1"),
        ('"a:b"', '"a:b"'),
        ("a:b" * 200, "a:b" * 200),
        ('"a:b"' * 200, '"a:b"' * 200),
        ("/a:b" * 200, "/a:b" * 200),
        ("ua:b" * 200, "ua:b" * 200),
    ],
    ids=_short_id,
)
def test_ordinary_outputs_unchanged_and_idempotent(text: str, expected: str) -> None:
    once = redact_credentials(text)
    assert once == expected
    assert redact_credentials(once) == once
