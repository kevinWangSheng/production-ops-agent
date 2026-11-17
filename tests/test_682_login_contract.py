"""Independent #682 contract tests, revision r1 decided 2026-11-16.

Credentials and clocks use the existing in-memory workbench support. Sessions
are obtained only by submitting the public form; their wire format is opaque.
Startup tests replace storage/listeners, retaining the real entry point, auth,
environment parsing and HTTP app. No database, Docker or model is needed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser
from http.cookies import SimpleCookie
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

from opspilot.web import AuthConfig, Authenticator, create_app, hash_password
from tests.m1_web_support import (
    ORIGIN,
    UI_PASSWORD,
    UI_USER,
    basic,
    build_workbench,
    call,
    post_form,
    same_origin,
    stream,
)

SECRET = "test-session-secret-0000"
BOB_PASSWORD = "test-bob-password"
MISSING_ID = "00000000-0000-0000-0000-000000000682"
HTML = {"accept": "text/html"}
JSON = {"accept": "application/json"}


@dataclass
class _Element:
    tag: str
    attrs: dict
    parents: tuple
    text: str = ""
    inputs: list = field(default_factory=list)


class _Page(HTMLParser):
    """Inspect semantic HTML without depending on templates or CSS classes."""

    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.elements = []
        self.stack = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        element = _Element(tag, dict(attrs), tuple(self.stack))
        self.elements.append(element)
        if tag == "input":
            for parent in self.stack:
                if parent.tag == "form":
                    parent.inputs.append(element)
        if tag not in {
            "area",
            "base",
            "br",
            "col",
            "embed",
            "hr",
            "img",
            "input",
            "link",
            "meta",
            "param",
            "source",
            "track",
            "wbr",
        }:
            self.stack.append(element)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        for element in self.stack:
            element.text += data

    def tags(self, tag):
        return [element for element in self.elements if element.tag == tag]


@pytest.fixture(scope="module")
def users():
    return {
        UI_USER: hash_password(UI_PASSWORD, salt=b"fixed-salt-16byt"),
        "bob": hash_password(BOB_PASSWORD, salt=b"other-salt-16byt"),
    }


@pytest.fixture
def app_factory(users):
    def make(
        *,
        clock=None,
        revision="contract-682",
        origins=(ORIGIN,),
        configured_users=None,
        secret=SECRET,
    ):
        _, workbench, clock = build_workbench(clock=clock)
        # The shared pre-suspension double omits this read-only store method.
        workbench.incidents.suspension_state = lambda: {
            "global": {"suspended": False, "generation": 0},
            "targets": {},
        }
        auth = Authenticator(
            AuthConfig(
                ui_users=users if configured_users is None else configured_users,
                event_tokens={},
                auth_revision=revision,
                allowed_origins=frozenset(origins),
                session_secret=secret.encode(),
            )
        )
        return create_app(workbench, auth, clock, sse_idle_seconds=0.01), clock

    return make


def _assert_html(response, status=200):
    assert response.status == status, response.text
    assert response.headers.get("content-type", "").startswith("text/html")
    return _Page(response.text)


def _login(app, *, user=UI_USER, password=UI_PASSWORD, next_path=None, headers=None):
    path = "/login"
    if next_path is not None:
        path += "?" + urlencode({"next": next_path})
    page = _assert_html(call(app, "GET", path, headers=HTML))
    forms = page.tags("form")
    assert len(forms) == 1
    form = forms[0]
    assert form.attrs.get("method", "get").lower() == "post"
    assert form.attrs.get("action") == "/login"
    fields = {
        i.attrs["name"]: i.attrs.get("value", "")
        for i in form.inputs
        if i.attrs.get("name")
    }
    passwords = [i for i in form.inputs if i.attrs.get("type") == "password"]
    names = [i for i in form.inputs if i.attrs.get("type", "text") == "text"]
    assert len(passwords) == len(names) == 1
    fields[names[0].attrs["name"]] = user
    fields[passwords[0].attrs["name"]] = password
    if next_path is not None:
        # Exercise POST validation even if GET already sanitizes the hidden value.
        fields["next"] = next_path
    return post_form(
        app,
        "/login",
        fields,
        headers={**HTML, **(same_origin() if headers is None else headers)},
    )


def _cookie(response):
    cookie = SimpleCookie()
    cookie.load(response.headers.get("set-cookie", ""))
    assert "opspilot_session" in cookie, response.headers
    return cookie["opspilot_session"]


def _session(app, **kwargs):
    response = _login(app, **kwargs)
    assert response.status == 303, response.text
    return {"cookie": "opspilot_session=" + _cookie(response).coded_value}


def _assert_unauthorized(response):
    assert response.status == 401, response.text
    assert response.headers.get("content-type", "").startswith("application/json")
    assert isinstance(response.json()["code"], str)
    assert response.headers.get("www-authenticate", "").startswith("Basic")
    assert "location" not in response.headers


@pytest.mark.parametrize(
    ("user", "password"), [(UI_USER, UI_PASSWORD), ("bob", BOB_PASSWORD)]
)
def test_public_form_logs_in_each_configured_pbkdf2_user(app_factory, user, password):
    app, _ = app_factory()
    response = _login(app, user=user, password=password)
    assert response.status == 303
    assert response.headers["location"] == "/"
    session = {"cookie": "opspilot_session=" + _cookie(response).coded_value}
    page = call(app, "GET", "/", headers={**HTML, **session})
    _assert_html(page)
    document = _Page(page.text)
    assert any(
        f"Signed in as {user}" in " ".join(e.text.split())
        for e in document.elements
        if e.tag in {"header", "nav"}
    )


@pytest.mark.parametrize(
    ("origins", "secure"),
    [
        ((ORIGIN,), True),
        ((ORIGIN, "https://other.example.test"), True),
        (("http://localhost:8080",), False),
        ((ORIGIN, "http://localhost:8080"), False),
    ],
)
def test_cookie_attributes_follow_all_allowed_origins(app_factory, origins, secure):
    app, _ = app_factory(origins=origins)
    response = _login(app, headers={"origin": origins[0]})
    assert response.status == 303
    cookie = _cookie(response)
    assert cookie.value
    assert cookie["httponly"]
    assert cookie["samesite"].lower() == "lax"
    assert cookie["path"] == "/"
    assert int(cookie["max-age"]) == 12 * 60 * 60
    assert bool(cookie["secure"]) is secure


def test_bad_credentials_are_indistinguishable_and_never_echoed(app_factory):
    app, _ = app_factory()
    password = "wrong-password-682-<sensitive>"
    unknown = "unknown-user-682-<sensitive>"
    wrong = _login(app, password=password)
    missing = _login(app, user=unknown, password=password)
    for response in (wrong, missing):
        _assert_html(response, 401)
        assert "Wrong user name or password" in response.text
        assert "www-authenticate" not in response.headers
        assert "set-cookie" not in response.headers
        for value in (UI_USER, UI_PASSWORD, unknown, password):
            assert value not in unescape(response.text)
    assert wrong.body == missing.body


@pytest.mark.parametrize(
    "next_path",
    [
        "/",
        "/controls",
        "/incidents/example?tab=evidence&filter=5xx",
        "/?q=a%20b",
    ],
)
def test_same_site_next_is_preserved(app_factory, next_path):
    app, _ = app_factory()
    response = _login(app, next_path=next_path)
    assert response.status == 303
    assert response.headers["location"] == next_path


@pytest.mark.parametrize(
    "next_path",
    [
        "",
        "relative",
        "https://evil.example/",
        "//evil.example/",
        "///evil.example/",
        "/\\evil.example/",
        "/safe\\evil",
        "\\evil",
        "javascript:alert(1)",
        "/bad\x00path",
        "/bad\tpath",
        "/bad\npath",
        "/bad\rpath",
        "/bad\x1fpath",
        "/bad\x7fpath",
    ],
)
def test_unsafe_next_falls_back_to_root(app_factory, next_path):
    app, _ = app_factory()
    response = _login(app, next_path=next_path)
    assert response.status == 303
    assert response.headers["location"] == "/"


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/controls?tab=targets&filter=checkout%20prod",
        f"/incidents/{MISSING_ID}?tab=evidence",
        "/version-changes",
        f"/version-changes/{MISSING_ID}",
        f"/knowledge/{MISSING_ID}",
        f"/postmortems/{MISSING_ID}/versions/1",
        "/logout",
    ],
)
def test_unauthenticated_html_pages_redirect_with_original_path_and_query(
    app_factory, path
):
    app, _ = app_factory()
    response = call(app, "GET", path, headers=HTML)
    assert response.status == 302, response.headers
    target = urlsplit(response.headers["location"])
    assert (target.scheme, target.netloc, target.path, target.fragment) == (
        "",
        "",
        "/login",
        "",
    )
    assert parse_qs(target.query) == {"next": [path]}
    assert "www-authenticate" not in response.headers


@pytest.mark.parametrize(
    ("method", "path", "headers"),
    [
        ("GET", "/", {}),  # urllib's default: no Accept header.
        ("GET", "/", {"accept": "*/*"}),  # curl's default.
        ("GET", "/", JSON),
        ("POST", "/intake/ui", same_origin()),
        ("POST", "/intake/ui", {"accept": "*/*", **same_origin()}),
        ("POST", "/intake/ui", {**JSON, **same_origin()}),
        ("GET", f"/incidents/{MISSING_ID}/events", {"accept": "text/event-stream"}),
    ],
)
def test_non_html_requests_keep_json_basic_challenge(
    app_factory, method, path, headers
):
    app, _ = app_factory()
    _assert_unauthorized(call(app, method, path, headers=headers))


@pytest.mark.parametrize("session_state", ["missing", "expired"])
def test_unauthenticated_browser_post_offers_sign_in_without_basic_popup(
    app_factory, session_state
):
    app, clock = app_factory()
    session = {}
    if session_state == "expired":
        session = _session(app)
        clock.advance(12 * 3600 + 1)
    response = post_form(
        app,
        "/intake/ui",
        {
            "target_id": "checkout-prod",
            "question": "Why are test requests failing?",
            "idempotency_key": "expired-browser-form-682",
        },
        headers={**HTML, **same_origin(), **session},
    )
    page = _assert_html(response, 401)
    assert "www-authenticate" not in response.headers
    assert any(
        link.attrs.get("href") == "/login" and "Sign in" in link.text
        for link in page.tags("a")
    )


@pytest.mark.parametrize(
    ("elapsed", "accepted"),
    [
        (0, True),
        (12 * 3600 - 1, True),
        (12 * 3600 + 1, False),
        (-60, True),
        (-61, False),
    ],
)
def test_session_age_uses_injected_clock_and_future_tolerance(
    app_factory, elapsed, accepted
):
    app, clock = app_factory()
    session = _session(app)
    clock.advance(elapsed)
    response = call(app, "GET", "/", headers={**JSON, **session})
    if accepted:
        assert response.status == 200, response.text
    else:
        _assert_unauthorized(response)


@pytest.mark.parametrize("change", ["revision", "removed-user", "wrong-signing-key"])
def test_session_is_rejected_after_auth_configuration_changes(
    app_factory, users, change
):
    original, clock = app_factory()
    session = _session(original)
    overrides = {
        "revision": {"revision": "contract-682-changed"},
        "removed-user": {"configured_users": {"bob": users["bob"]}},
        "wrong-signing-key": {"secret": "test-other-session-secret-0000"},
    }[change]
    changed, _ = app_factory(clock=clock, **overrides)
    _assert_unauthorized(call(changed, "GET", "/", headers={**JSON, **session}))


def test_modified_session_cookie_is_rejected_without_knowing_its_format(app_factory):
    app, _ = app_factory()
    session = _session(app)
    forged = session["cookie"] + "tampered"
    _assert_unauthorized(call(app, "GET", "/", headers={**JSON, "cookie": forged}))


def _intake(app, headers, key):
    response = post_form(
        app,
        "/intake/ui",
        {
            "target_id": "checkout-prod",
            "question": "Why are test requests failing?",
            "idempotency_key": key,
        },
        headers={**JSON, **same_origin(), **headers},
    )
    assert response.status == 201, response.text
    return response.json()["incident_id"]


def test_basic_and_session_have_the_same_principal_in_public_sse_audit(app_factory):
    app, _ = app_factory()
    session = _session(app)
    principals = []
    for key, headers in (("basic-682", basic()), ("session-682", session)):
        incident = _intake(app, headers, key)
        response = stream(
            app,
            f"/incidents/{incident}/events",
            headers={**headers, "accept": "text/event-stream"},
            until_events=1,
        )
        assert response.status == 200, response.text
        intake = [
            payload
            for _, kind, payload in response.sse_events()
            if kind == "intake_accepted"
        ]
        assert len(intake) == 1
        principals.append({name: intake[0][name] for name in ("actor_id", "channel")})
    assert principals == [{"actor_id": UI_USER, "channel": "ui_basic"}] * 2


@pytest.mark.parametrize("path", ["/", "/controls", "/logout"])
def test_basic_remains_accepted_on_ui_pages(app_factory, path):
    app, _ = app_factory()
    _assert_html(call(app, "GET", path, headers={**HTML, **basic()}))


@pytest.mark.parametrize("path", ["/login", "/logout"])
@pytest.mark.parametrize(
    "origin_headers",
    [
        {},
        {"origin": "https://foreign.example.test"},
        {"origin": "https://foreign.example.test", "sec-fetch-site": "same-origin"},
    ],
)
def test_session_posts_enforce_existing_origin_check(app_factory, path, origin_headers):
    app, _ = app_factory()
    session = _session(app)
    if path == "/login":
        response = _login(app, headers=origin_headers)
    else:
        response = post_form(
            app, path, {}, headers={**session, **origin_headers, **JSON}
        )
    assert response.status == 403, response.text
    assert "set-cookie" not in response.headers
    # A refused logout must leave the browser's existing session usable.
    assert call(app, "GET", "/", headers=session).status == 200


@pytest.mark.parametrize("path", ["/login", "/logout"])
def test_same_origin_fetch_metadata_is_still_accepted(app_factory, path):
    app, _ = app_factory()
    if path == "/login":
        response = _login(app, headers={"sec-fetch-site": "same-origin"})
    else:
        response = post_form(
            app,
            path,
            {},
            headers={
                **_session(app),
                "sec-fetch-site": "same-origin",
            },
        )
    assert response.status == 303, response.text


def test_logout_confirmation_is_one_post_form_and_post_clears_browser_cookie(
    app_factory,
):
    app, _ = app_factory()
    session = _session(app)
    response = call(app, "GET", "/logout", headers={**HTML, **session})
    page = _assert_html(response)
    forms = page.tags("form")
    assert len(forms) == 1
    assert forms[0].attrs.get("method", "get").lower() == "post"
    assert forms[0].attrs.get("action") == "/logout"
    assert "set-cookie" not in response.headers
    assert call(app, "GET", "/", headers=session).status == 200
    logged_out = post_form(app, "/logout", {}, headers={**session, **same_origin()})
    assert logged_out.status == 303
    assert logged_out.headers["location"] == "/login"
    cleared = _cookie(logged_out)
    assert cleared.value == ""
    assert cleared["max-age"] == "0"
    assert cleared["path"] == "/"
    # Simulate applying Set-Cookie; stateless logout need not revoke a copied token.
    _assert_unauthorized(call(app, "GET", "/", headers=JSON))


@pytest.mark.parametrize("auth_kind", ["basic", "session"])
def test_shell_shows_actor_and_logout_link_without_adding_forms(app_factory, auth_kind):
    app, _ = app_factory()
    headers = basic() if auth_kind == "basic" else _session(app)
    incident = _intake(app, headers, "shell-682")
    for path in ("/", "/controls", f"/incidents/{incident}", "/logout"):
        response = call(app, "GET", path, headers={**HTML, **headers})
        page = _assert_html(response)
        assert any(
            f"Signed in as {UI_USER}" in " ".join(e.text.split())
            for e in page.elements
            if e.tag in {"header", "nav"}
        )
        links = [e for e in page.tags("a") if e.attrs.get("href") == "/logout"]
        assert len(links) == 1
        assert "Log out" in links[0].text
        assert not any(p.tag == "form" for p in links[0].parents)
        if path == "/logout":
            # The confirmation card owns one form; the shell adds none.
            assert len(page.tags("form")) == 1
            assert page.tags("form")[0].attrs.get("action") == "/logout"
        else:
            assert not any(
                e.attrs.get("action") == "/logout" for e in page.tags("form")
            )
        assert not any(
            p.tag in {"header", "nav"} for e in page.tags("form") for p in e.parents
        )
        if path == "/":
            assert len(page.tags("form")) == 1  # The existing incident-intake form.


@pytest.mark.parametrize("path", [f"/incidents/{MISSING_ID}", "/incidents/not-a-uuid"])
def test_html_refusals_explain_failure_and_keep_json_code_secondary(app_factory, path):
    app, _ = app_factory()
    json_response = call(app, "GET", path, headers={**JSON, **basic()})
    assert json_response.status == 404
    assert set(json_response.json()) == {"code"}
    code = json_response.json()["code"]
    response = call(app, "GET", path, headers={**HTML, **basic()})
    page = _assert_html(response, 404)
    assert any(e.attrs.get("href") == "/" for e in page.tags("a"))
    assert code in response.text
    assert all(code not in heading.text for heading in page.tags("h1"))
    # A separate paragraph must explain the refusal in ordinary words.
    assert any(
        len(p.text.strip().split()) >= 3 and code not in p.text for p in page.tags("p")
    ), response.text


@pytest.fixture
def serve(monkeypatch, users):
    """Exercise real CLI/env/auth wiring while replacing external I/O only."""
    import uvicorn

    from opspilot.web import __main__ as entry

    _, workbench, clock = build_workbench()

    class Storage:
        def __init__(self, *args, **kwargs):
            pass

        def __getattr__(self, name):
            return lambda *args, **kwargs: None

    for name in (
        "DurableStore",
        "DurableEventLog",
        "DurableEvidenceStore",
        "DurableWebLedger",
        "DurableIncidentStore",
        "KnowledgeStore",
        "PostmortemReview",
    ):
        monkeypatch.setattr(entry, name, Storage)
    monkeypatch.setattr(entry, "DurableClock", lambda *args, **kwargs: clock)
    monkeypatch.setattr(entry, "Workbench", lambda *args, **kwargs: workbench)
    monkeypatch.setattr(
        entry,
        "select_profile",
        lambda env: SimpleNamespace(
            versions=lambda: {},
            face=lambda clock: None,
        ),
    )
    monkeypatch.setattr(entry, "_start_health", lambda store: None)
    monkeypatch.setattr(entry, "_target_registry", lambda: None)
    monkeypatch.setattr(entry, "_health_profile", lambda: None)
    monkeypatch.setattr(entry.logs, "configure", lambda *args, **kwargs: None)
    monkeypatch.setenv("OPSPILOT_WEB_DSN", "host=unused-test-storage")
    monkeypatch.setenv(
        "OPSPILOT_UI_USERS", ",".join(f"{u}={h}" for u, h in users.items())
    )
    monkeypatch.setenv("OPSPILOT_EVENT_TOKENS", "")
    monkeypatch.setenv("OPSPILOT_ALLOWED_ORIGINS", ORIGIN)
    monkeypatch.setenv("OPSPILOT_AUTH_REVISION", "contract-682")
    monkeypatch.setenv("OPSPILOT_SELF_HEAL_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("OPSPILOT_TOOL_PROFILE", "fixture")
    monkeypatch.setenv("OPSPILOT_TRACE", "0")
    apps = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: apps.append(app))

    def start(secret):
        if secret is None:
            monkeypatch.delenv("OPSPILOT_SESSION_SECRET", raising=False)
        else:
            monkeypatch.setenv("OPSPILOT_SESSION_SECRET", secret)
        result = entry.main(["opspilot.web", "serve"])
        assert result == 0
        assert apps
        return apps[-1]

    return start, apps


@pytest.mark.parametrize("secret", [None, ""], ids=["unset", "empty"])
def test_serve_without_secret_warns_and_uses_an_ephemeral_key(serve, caplog, secret):
    start, _ = serve
    with caplog.at_level(logging.WARNING):
        first = start(secret)
    assert any(
        record.levelno >= logging.WARNING
        and "OPSPILOT_SESSION_SECRET" in record.getMessage()
        for record in caplog.records
    )
    session = _session(first)
    assert call(first, "GET", "/", headers=session).status == 200
    restarted = start(secret)
    _assert_unauthorized(call(restarted, "GET", "/", headers={**JSON, **session}))
    assert call(restarted, "GET", "/", headers=_session(restarted)).status == 200


@pytest.mark.parametrize("secret", [SECRET, "test-secret-0000", "test-密密密密"])
def test_explicit_secret_and_env_pbkdf2_users_survive_serve_restart(serve, secret):
    start, _ = serve
    first = start(secret)
    session = _session(first, user="bob", password=BOB_PASSWORD)
    restarted = start(secret)
    response = call(restarted, "GET", "/", headers={**HTML, **session})
    _assert_html(response)
    assert any(
        "Signed in as bob" in " ".join(e.text.split())
        for e in _Page(response.text).elements
        if e.tag in {"header", "nav"}
    )


@pytest.mark.parametrize(
    "secret", ["x", "test-short-0000", "123456789012345", "test-密密密"]
)
def test_serve_refuses_secrets_shorter_than_sixteen_bytes(serve, secret):
    start, apps = serve
    with pytest.raises((SystemExit, ValueError)) as refused:
        start(secret)
    assert "OPSPILOT_SESSION_SECRET" in str(refused.value)
    assert apps == []
