"""Safety: URL userinfo is redacted in encoded spellings of ``//`` (#359).

Ruled contract: docs/tasks/2026-11-22-next-batches-b1-prereview.md B1-U4 (a)
and B1-D7. Besides the literal ``//`` and the JSON-escaped ``\\/`` (#319),
three encoded families of ``//user:pass@host`` are redacted, each only when an
encoded ``//`` precedes the userinfo: percent-encoding (``%2F%2F``, ``@`` may
be ``%40``), any depth of backslash escaping (``\\\\\\/``), and HTML entities
(``&#x2F;``/``&#47;``/``&sol;``, ``@`` may be ``&#64;``/``&#x40;``/
``&commat;``). Output has #319's shape: the prefix as written, the marker, a
``@`` as written (``%40``, ``&#64;`` ... kept, lead decision replacing
B1-D7), then the host. Ordinary encoded text (an e-mail in a query
string, a redirect URL, HTML paths) is left alone. Mixed spellings
(``%2F/``), double percent-encoding (``%252F``) and base64 are not decided
and are not pinned here.

Only the public entry points are driven; secrets are low-entropy fakes.
"""

from __future__ import annotations

import time

import pytest

from opspilot.redaction import REDACTED_CREDENTIAL, redact_credentials, redact_payload

M = REDACTED_CREDENTIAL
BS3 = "\\" * 3  # ``\\\/``: one JSON escape of ``\/`` (three backslashes)
BS7 = "\\" * 7  # one level deeper

# (input, exact expected output) -- at least one per family, plus the
# pre-existing literal and JSON-escaped forms, which must not change.
EXACT = [
    # percent-encoding
    (
        "https:%2F%2Fuser:s3cret@db.example.com",
        f"https:%2F%2F{M}@db.example.com",
    ),
    (
        "https%3A%2F%2Fuser%3As3cret%40db.example.com",
        f"https%3A%2F%2F{M}%40db.example.com",
    ),
    # backslash-escaped, any depth
    (
        f"{BS3}/{BS3}/user:s3cret@db.example.com",
        f"{BS3}/{BS3}/{M}@db.example.com",
    ),
    (
        f"{BS7}/{BS7}/user:s3cret@db.example.com",
        f"{BS7}/{BS7}/{M}@db.example.com",
    ),
    # HTML entities
    (
        "&#x2F;&#x2F;user:s3cret@db.example.com",
        f"&#x2F;&#x2F;{M}@db.example.com",
    ),
    (
        "&#47;&#47;user:s3cret&#64;db.example.com",
        f"&#47;&#47;{M}&#64;db.example.com",
    ),
    ("&sol;&sol;default:pw@redis", f"&sol;&sol;{M}@redis"),
    # pre-existing behaviour (#319 and before), unchanged
    ("https://user:pw@host/x", f"https://{M}@host/x"),
    ("redis://default:pw@redis:6379", f"redis://{M}@redis:6379"),
    ("https:\\/\\/user:pw@host", f"https:\\/\\/{M}@host"),
]

# Other spellings in each family: secret gone, host kept.
COVERED = [
    ("%2F%2Fuser:s3cret%40db.example.com", "db.example.com"),
    ("dsn=postgres:%2F%2Fapp%3As3cret%40db.example.com:5432", "db.example.com"),
    (f"url={BS3}/{BS3}/user:s3cret@db.example.com/path", "db.example.com"),
    ("\\" * 15 + "/" + "\\" * 15 + "/user:s3cret@db.example.com", "db.example.com"),
    ("&#x2F;&#x2F;user:s3cret&#x40;db.example.com", "db.example.com"),
    ("&#x2F;&#x2F;user:s3cret&commat;db.example.com", "db.example.com"),
    ("&#47;&#47;user:s3cret@db.example.com", "db.example.com"),
    ('<a href="https:&#x2F;&#x2F;user:s3cret@db.example.com">', "db.example.com"),
    ("&sol;&sol;default:s3cret&#64;redis", "redis"),
]

UNCHANGED = [
    "/search?q=a%40b.com&page=2",
    "email=alice%40corp.com",
    "redirect=https%3A%2F%2Fexample.com%3Fnext%3Dbob%40x.com",
    "next=%2F%2Fcdn.example.com%2Fa%40b.png",
    '<a href="&#x2F;docs&#x2F;x">',
    "&#x2F;&#x2F;cdn.example.com&#x2F;img.png",
    "mailto:bob@example.com",
    "git@github.com:org/repo.git",
    "https://example.com/a@b",
]


def _id(value):
    return value if len(value) <= 40 else f"{value[:36]}...len{len(value)}"


@pytest.mark.parametrize(("text", "expected"), EXACT, ids=[_id(t) for t, _ in EXACT])
def test_encoded_userinfo_exact_output(text, expected):
    once = redact_credentials(text)
    assert once == expected
    assert redact_credentials(once) == once


@pytest.mark.parametrize(("text", "host"), COVERED, ids=[_id(t) for t, _ in COVERED])
def test_encoded_userinfo_secret_hidden(text, host):
    once = redact_credentials(text)
    assert "s3cret" not in once
    assert any(
        f"{M}{at}{host}" in once for at in ("@", "%40", "&#64;", "&#x40;", "&commat;")
    )
    assert redact_credentials(once) == once


@pytest.mark.parametrize("text", UNCHANGED, ids=_id)
def test_ordinary_encoded_text_unchanged(text):
    assert redact_credentials(text) == text


def test_payload_string_fields_use_the_same_rule():
    payload = {
        "url": EXACT[1][0],
        "items": [{"log": f"connect {EXACT[5][0]} failed"}, UNCHANGED[2]],
    }
    expected = {
        "url": EXACT[1][1],
        "items": [{"log": f"connect {EXACT[5][1]} failed"}, UNCHANGED[2]],
    }
    once = redact_payload(payload)
    assert once == expected
    assert redact_payload(once) == once


# Linear time: 512 KiB pathological runs of each family, followed by a real
# encoded userinfo that must still be redacted. A linear pass is far below
# the bound; the bound leaves room for a slow CI runner.
SIZE = 512 * 1024
BOUND_SECONDS = 5.0
RUNS = ["%2F%2F", "%2F%2Fu:p", "&#x2F;&#x2F;", "&#x2F;&#x2F;u:p", "\\", "\\\\/"]
TAIL_IN, TAIL_OUT = EXACT[1]


@pytest.mark.serial
@pytest.mark.parametrize("unit", RUNS)
def test_pathological_encoded_runs_are_linear(unit):
    run = unit * (SIZE // len(unit))
    text = f"{run} {TAIL_IN}"
    started = time.perf_counter()
    redacted = redact_credentials(text)
    elapsed = time.perf_counter() - started
    assert elapsed < BOUND_SECONDS, f"{unit!r} x {SIZE} bytes took {elapsed:.1f}s"
    assert redacted == f"{run} {TAIL_OUT}"
