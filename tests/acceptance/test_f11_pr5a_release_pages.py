"""Independent acceptance tests for F11 PR-5a without PostgreSQL: the release
observation pages and controls as an HTTP surface (routes, authentication,
Origin, navigation, F7 classification).

Written from the F11 PR-5 contract pre-review
(``~/.agent/handoffs/opspilot-prereview-f11-pr5-2026-10-24.md``: D1, D2, D6,
D11, D12 and section 4) and C3 section 9, without reading the PR-5a
implementation. The store-backed behaviour (pages over real records, the
control semantics, sampling after pause/resume/cancel, the incident back
link) is in ``tests/integration/test_f11_pr5a_release_pages_postgres.py``.

Every request here reaches the app with a release store that answers like
an unavailable database and records each call: a request refused for its
credentials or Origin must be refused before storage, so ``calls`` stays
empty.

Assumed names (the contract fixes the behaviour, not these spellings):

* N1 (D1) the three routes are exactly ``GET /version-changes``,
  ``GET /version-changes/{observation_id}`` and
  ``POST /version-changes/{observation_id}/control`` (path parameter name
  included, as F7 compares route templates).
* N2 the control form fields are ``action``, ``expected_control_generation``
  and ``idempotency_key`` (D11, lead brief).
* N3 the release store reaches the app through ``create_app(releases=...)``
  and/or the ``Workbench`` field whose name contains ``release``
  (``tests.f11_pr3_support.build_app``).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest

from opspilot.web import create_app
from tests.acceptance.test_f7_step1_entry_points import (
    EXPECTED_ROUTES,
    MUTATION_WORDS,
    _auth,
    _routes,
    _Untouchable,
)
from tests.f11_pr3_support import (
    ALERT_TOKEN,
    OTHER_TOKEN,
    RELEASE_TOKEN,
    RecordingReleaseStore,
    bearer_for,
    build_app,
    release_payload,
)
from tests.m1_web_support import (
    ORIGIN,
    basic,
    build_workbench,
    call,
    post_form,
    same_origin,
)
from tests.m1_web_support import post_json as _post_json

LIST = "/version-changes"
DETAIL = "/version-changes/{observation_id}"
CONTROL = "/version-changes/{observation_id}/control"
NEW_ROUTES = {("GET", LIST), ("GET", DETAIL), ("POST", CONTROL)}
JSON = {"accept": "application/json"}
HTML = {"accept": "text/html"}


def _app() -> tuple[Any, RecordingReleaseStore]:
    store = RecordingReleaseStore()
    app, _ = build_app(store)
    return app, store


def _detail(oid: Any = None) -> str:
    return DETAIL.format(observation_id=oid or uuid4())


def _control(oid: Any = None) -> str:
    return CONTROL.format(observation_id=oid or uuid4())


def _fields(action: str = "pause") -> dict[str, str]:
    return {
        "action": action,
        "expected_control_generation": "0",
        "idempotency_key": f"pr5a-{uuid4().hex}",
    }


def _missing(response: Any) -> None:
    """Red on main: the route is not there at all (feature missing)."""
    assert response.status not in (404, 405), (
        f"release observation route missing: {response.status} {response.text[:200]!r}"
    )


# -- D1 / F7: the route set ---------------------------------------------------


def test_d1_the_three_release_routes_are_registered_without_touching_dependencies():
    app = create_app(_Untouchable("workbench"), _auth(), _Untouchable("clock"))

    assert NEW_ROUTES <= _routes(app)


def test_d1_the_f7_allow_list_classifies_the_three_release_routes():
    assert NEW_ROUTES <= set(EXPECTED_ROUTES)
    assert EXPECTED_ROUTES[("POST", CONTROL)].startswith("write: OpsPilot ")


@pytest.mark.parametrize(("method", "path"), sorted(NEW_ROUTES))
def test_d1_no_release_route_names_a_target_mutation_or_release_gate(
    method: str, path: str
):
    for word in MUTATION_WORDS:
        assert word not in path.lower(), (method, path, word)


@pytest.mark.parametrize(
    "path",
    [
        "/version-changes/{oid}/approve",
        "/version-changes/{oid}/block",
        "/version-changes/{oid}/rollback",
        "/version-changes/{oid}/promote",
        "/version-changes/{oid}/deploy",
        "/version-changes/{oid}/authorize",
    ],
)
def test_c3_9_no_release_gate_sub_route_exists(path: str):
    app, store = _app()

    response = post_form(
        app,
        path.format(oid=uuid4()),
        _fields(),
        headers={**basic(), **same_origin(), **JSON},
    )

    assert response.status in (404, 405), (path, response.status, response.text)
    assert store.calls == []


# -- D12: Basic Auth only; no event token, no release-deployer ---------------


#: A fixed id keeps the parametrized test ids stable.
_OID = UUID("00000000-0000-4000-8000-0000000005a0")


def _requests() -> list[tuple[str, str]]:
    oid = _OID
    return [("GET", LIST), ("GET", _detail(oid)), ("POST", _control(oid))]


def _send(app: Any, method: str, path: str, headers: dict[str, str]) -> Any:
    if method == "GET":
        return call(app, method, path, headers={**JSON, **headers})
    return post_form(app, path, _fields(), headers={**JSON, **headers})


@pytest.mark.parametrize(("method", "path"), _requests())
def test_d12_a_request_without_credentials_is_401_before_storage(
    method: str, path: str
):
    app, store = _app()

    response = _send(app, method, path, same_origin())

    _missing(response)
    assert response.status == 401, response.text
    assert store.calls == []


@pytest.mark.parametrize("token", [ALERT_TOKEN, OTHER_TOKEN])
@pytest.mark.parametrize(("method", "path"), _requests())
def test_d12_an_event_token_is_401_before_storage(method: str, path: str, token: str):
    app, store = _app()

    response = _send(app, method, path, {**bearer_for(token), **same_origin()})

    _missing(response)
    assert response.status == 401, response.text
    assert store.calls == []


@pytest.mark.parametrize(("method", "path"), _requests())
def test_d12_the_release_deployer_token_gets_no_ui_capability(method: str, path: str):
    app, store = _app()

    response = _send(app, method, path, {**bearer_for(RELEASE_TOKEN), **same_origin()})

    _missing(response)
    assert response.status in (401, 403), response.text
    assert store.calls == []


@pytest.mark.parametrize(("method", "path"), _requests())
def test_d12_wrong_basic_credentials_are_401_before_storage(method: str, path: str):
    app, store = _app()

    response = _send(app, method, path, {**basic(password="wrong"), **same_origin()})

    _missing(response)
    assert response.status == 401, response.text
    assert store.calls == []


def test_d12_a_ui_user_cannot_post_a_release_intake():
    """r5 isolation in the other direction: the intake stays the deployer's."""
    app, store = _app()

    response = _post_json(
        app,
        "/intake/version-changes",
        release_payload(),
        headers={**basic(), **same_origin()},
    )

    assert response.status in (401, 403), response.text
    assert store.calls == []


# -- section 4 step 4.2: Origin on the control --------------------------------


@pytest.mark.parametrize(
    "origin",
    [None, "https://evil.example.test", "null", ORIGIN.replace("https", "http")],
)
def test_control_without_a_same_site_origin_is_refused_before_storage(
    origin: str | None,
):
    app, store = _app()
    headers = {**basic(), **JSON}
    if origin is not None:
        headers["origin"] = origin

    response = post_form(app, _control(), _fields(), headers=headers)

    _missing(response)
    assert response.status == 403, response.text
    assert store.calls == []


# -- D6: the entry point from the existing pages -----------------------------


def test_d6_the_workbench_pages_link_to_the_release_list():
    """``/controls`` needs the durable suspension state, which the in-memory
    incident store does not have; it is checked in the PostgreSQL module."""
    app, _, _ = build_workbench()

    response = call(app, "GET", "/", headers={**basic(), **HTML})

    assert response.status == 200, response.text
    assert 'href="/version-changes"' in response.text, response.text[:2000]
