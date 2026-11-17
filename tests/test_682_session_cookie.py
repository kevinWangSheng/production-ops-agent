"""Signed session cookie of the workbench login form (#682).

Unit tests of ``Authenticator.issue_session`` / ``session_principal`` and of
``safe_next``; the HTTP behaviour is covered by the independent tests.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

import pytest

from opspilot.web.app import safe_next
from opspilot.web.auth import AuthConfig, Authenticator, AuthError, hash_password

SECRET = b"test-session-secret"
NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
USERS = {"alice": hash_password("pw", salt=b"fixed-salt-16byt")}


def _auth(**overrides) -> Authenticator:
    fields = {
        "ui_users": USERS,
        "event_tokens": {},
        "auth_revision": "rev-1",
        "session_secret": SECRET,
        **overrides,
    }
    return Authenticator(AuthConfig(**fields))


def _cookie(auth: Authenticator | None = None, at: datetime = NOW) -> str:
    auth = auth or _auth()
    return auth.issue_session(auth.login("alice", "pw"), at)


def test_a_fresh_session_names_the_same_principal_as_basic():
    auth = _auth()
    principal = auth.session_principal(_cookie(auth), NOW + timedelta(hours=1))
    assert principal == auth.login("alice", "pw")
    assert principal.channel == "ui_basic" and principal.auth_revision == "rev-1"


def test_the_cookie_holds_no_password_or_hash():
    body = _cookie().split(".")[0]
    claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    assert claims == {"a": "alice", "t": int(NOW.timestamp()), "r": "rev-1"}


@pytest.mark.parametrize(
    "age", [timedelta(hours=12), timedelta(hours=13), timedelta(seconds=-61)]
)
def test_expired_or_future_sessions_are_refused(age):
    with pytest.raises(AuthError) as refused:
        _auth().session_principal(_cookie(), NOW + age)
    assert refused.value.code == "INVALID_SESSION"


def test_just_under_twelve_hours_is_accepted():
    _auth().session_principal(
        _cookie(), NOW + timedelta(hours=12) - timedelta(seconds=1)
    )


def test_a_changed_revision_secret_or_user_set_ends_the_session():
    value = _cookie()
    for other in (
        _auth(auth_revision="rev-2"),
        _auth(session_secret=b"other-session-secret"),
        _auth(ui_users={"bob": USERS["alice"]}),
    ):
        with pytest.raises(AuthError):
            other.session_principal(value, NOW)


@pytest.mark.parametrize(
    "mangle",
    [
        lambda v: v[:-2] + ("AA" if not v.endswith("AA") else "BB"),
        lambda v: "x" + v,
        lambda v: v.split(".")[0],
        lambda v: "",
        lambda v: v + "é",
        lambda v: "." + v.split(".")[1],
        lambda v: "a" * 5000,
    ],
)
def test_tampered_cookies_are_refused(mangle):
    with pytest.raises(AuthError) as refused:
        _auth().session_principal(mangle(_cookie()), NOW)
    assert refused.value.code == "INVALID_SESSION"


def test_without_a_secret_no_session_is_issued_or_accepted():
    value = _cookie()
    plain = _auth(session_secret=None)
    assert not plain.sessions_enabled
    with pytest.raises(AuthError):
        plain.session_principal(value, NOW)
    with pytest.raises(AuthError):
        plain.issue_session(plain.login("alice", "pw"), NOW)


def test_a_short_secret_is_refused():
    with pytest.raises(ValueError):
        _auth(session_secret=b"short")


def test_secure_flag_only_when_every_origin_is_https():
    assert _auth(allowed_origins=frozenset({"https://ops.example"})).secure_cookie
    assert not _auth(
        allowed_origins=frozenset({"https://ops.example", "http://127.0.0.1:8080"})
    ).secure_cookie
    assert not _auth().secure_cookie


def test_login_refuses_unknown_empty_and_wrong_alike():
    auth = _auth()
    for user, password in (("mallory", "pw"), ("", "pw"), ("alice", "nope")):
        with pytest.raises(AuthError) as refused:
            auth.login(user, password)
        assert refused.value.code == "INVALID_CREDENTIALS"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, "/"),
        ("", "/"),
        ("/incidents/x?y=1", "/incidents/x?y=1"),
        ("https://evil.example/", "/"),
        ("//evil.example", "/"),
        ("/\\evil.example", "/"),
        ("/\t/evil.example", "/"),
        ("/a\\b", "/"),
        ("incidents", "/"),
    ],
)
def test_safe_next(raw, expected):
    assert safe_next(raw) == expected


def test_serve_generates_an_ephemeral_key_when_unset(monkeypatch, caplog):
    from opspilot.web import __main__ as web_main

    monkeypatch.delenv("OPSPILOT_SESSION_SECRET", raising=False)
    with caplog.at_level("WARNING"):
        first, second = web_main._session_secret(), web_main._session_secret()
    assert len(first) == 32 and first != second
    assert "OPSPILOT_SESSION_SECRET not set" in caplog.text
    monkeypatch.setenv("OPSPILOT_SESSION_SECRET", "test-session-secret")
    assert web_main._session_secret() == b"test-session-secret"
    monkeypatch.setenv("OPSPILOT_SESSION_SECRET", "short")
    with pytest.raises(SystemExit):
        web_main._session_secret()


# -- review fixes (#682 codex review) ------------------------------------------


def _web_app():
    from opspilot.web.app import create_app
    from tests import m1_web_support as support

    _, workbench, clock = support.build_workbench()
    users = {
        support.UI_USER: hash_password(support.UI_PASSWORD, salt=b"fixed-salt-16byt"),
        "bob": hash_password("bob-pw", salt=b"fixed-salt-16byt"),
    }
    config = AuthConfig(
        ui_users=users,
        event_tokens={},
        auth_revision="auth-rev-1",
        allowed_origins=frozenset({support.ORIGIN}),
        session_secret=SECRET,
    )
    return create_app(workbench, Authenticator(config), clock), support


HTML = {"accept": "text/html,application/xhtml+xml"}


def _login(app, support, user, password):
    from urllib.parse import urlencode

    response = support.call(
        app,
        "POST",
        "/login",
        headers={
            "content-type": "application/x-www-form-urlencoded",
            **HTML,
            **support.same_origin(),
        },
        body=urlencode({"username": user, "password": password}).encode(),
    )
    assert response.status == 303
    return {"cookie": response.headers["set-cookie"].split(";")[0]}


def test_a_valid_session_wins_over_a_cached_basic_header():
    app, support = _web_app()
    bob = _login(app, support, "bob", "bob-pw")
    page = support.call(app, "GET", "/", headers={**HTML, **support.basic(), **bob})
    assert page.status == 200
    assert "Signed in as <code>bob</code>" in page.text
    assert f"<code>{support.UI_USER}</code>" not in page.text


def test_basic_still_works_beside_a_stale_cookie_and_alone():
    app, support = _web_app()
    stale = {"cookie": "opspilot_session=garbage"}
    for headers in ({**support.basic(), **stale}, support.basic()):
        page = support.call(app, "GET", "/", headers={**HTML, **headers})
        assert page.status == 200
        assert f"Signed in as <code>{support.UI_USER}</code>" in page.text


@pytest.mark.parametrize(
    ("method", "path", "status"),
    [("GET", "/no-such-page", 404), ("GET", "/intake/ui", 405)],
)
def test_framework_errors_render_html_for_page_gets_and_json_otherwise(
    method, path, status
):
    app, support = _web_app()
    page = support.call(app, method, path, headers=HTML)
    assert page.status == status
    assert page.headers["content-type"].startswith("text/html")
    assert "Back to Incidents" in page.text
    plain = support.call(app, method, path, headers={"accept": "application/json"})
    assert plain.status == status
    assert "detail" in plain.json()


@pytest.mark.parametrize("value", ["   ", " test-session-secret "])
def test_the_secret_is_used_byte_for_byte(monkeypatch, value):
    from opspilot.web import __main__ as web_main

    monkeypatch.setenv("OPSPILOT_SESSION_SECRET", value)
    if len(value) < 16:
        with pytest.raises(SystemExit):
            web_main._session_secret()
    else:
        assert web_main._session_secret() == value.encode("utf-8")


def test_an_empty_secret_counts_as_unset(monkeypatch):
    from opspilot.web import __main__ as web_main

    monkeypatch.setenv("OPSPILOT_SESSION_SECRET", "")
    assert len(web_main._session_secret()) == 32
