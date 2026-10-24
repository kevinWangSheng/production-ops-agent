"""Independent PostgreSQL acceptance tests for F11 PR-5a: release observation
pages and the pause/resume/cancel controls over real records.

Written from the F11 PR-5 contract pre-review
(``~/.agent/handoffs/opspilot-prereview-f11-pr5-2026-10-24.md``: D1-D6,
D9-D12 and the section 4 behaviours that belong to PR-5a; U-1..U-4 decided
as recommended) and C3 section 9, without reading the PR-5a implementation.
PR-5a has no automatic authorization (D7 is PR-5b), so a release becomes
``observing`` here through the PR-2 store primitive, as the PR-4b scenarios
did; everything a person does is driven over HTTP.

Entry points are public only: the web app (HTTP), ``ReleaseStore`` methods,
``Workbench.release_anomaly_pass`` and the Observer's ``poll_once`` against a
fake Prometheus (``tests.f11_comparison_support.ComparisonWorld``). SQL only
reads the tables the earlier F11 contracts name.

Assumed names, in addition to N1-N3 of
``tests/acceptance/test_f11_pr5a_release_pages.py``:

* N4 page format: ``Accept: text/html`` gives HTML, any other accept JSON
  (D1: "按 ``wants_html`` 切换"). The JSON shape is not fixed, so values are
  searched anywhere in the document (strings exactly; timestamps as
  instants, since the record keeps an instant and not the payload text).
* N5 a successful control answers 200 with a JSON body; a refusal answers
  ``{"code": ...}`` like every other workbench refusal (D2).
* N6 the projection label of a release is a string value in its list entry
  and on its page: ``degraded`` for a stored ``anomalous``, otherwise the
  stored status (D5); the terminal labels are absent while pending.
* N7 the fixed disclaimer text of D4 contains
  ``不批准、不阻断、不执行发布``.
"""

# ruff: noqa: F811 - pytest fixtures imported from the PR-2a module
from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator, Mapping
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from opspilot.observation import ObservationStore
from opspilot.observation.releases import ReleaseStore
from opspilot.observer.loop import ObserverLoop
from opspilot.persistence import DurableStore
from opspilot.tools.profiles import select_profile
from opspilot.web import (
    AuthConfig,
    Authenticator,
    DurableClock,
    DurableEventLog,
    DurableEvidenceStore,
    DurableIncidentStore,
    DurableWebLedger,
    Workbench,
    create_app,
    hash_password,
    token_digest,
)
from opspilot.web.store import MappingTargetRegistry
from tests.f11_pr3_support import RELEASE_ACTOR, RELEASE_TOKEN, bearer_for, iso
from tests.f11_release_support import (
    AFTER_HASH,
    BEFORE_HASH,
    PROFILE,
    EvidenceBuilder,
    digest,
    planned_texts,
    pods_for,
    policy_content,
    policy_entry,
)
from tests.integration.test_f11_pr4a_comparison_postgres import _anomalous, _world
from tests.integration.test_f11_pr4b_consume_postgres import (
    _created_case,
    _link,
    _registry_entry,
)
from tests.integration.test_f11_release_observation_postgres import (  # noqa: F401 - fixtures
    AFTER_IMAGES,
    BEFORE_IMAGES,
    POOL,
    Catalog,
    Observing,
    _claim,
    _claims,
    _db_now,
    _expiring,
    _make_due,
    _observing,
    _record_release,
    _rows,
    _set_authorized_at,
    _state,
    _wait_past,
    catalog,
    history,
    observer,
    observer_dsn,
    owner,
    reader,
    web,
    web_dsn,
    worker_dsn,
)
from tests.m1_web_support import (
    EVENT_TOKEN,
    ORIGIN,
    UI_PASSWORD,
    UI_USER,
    basic,
    call,
    post_form,
    same_origin,
)
from tests.target_support import IDENTITY, WORKLOAD

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1",
    reason="explicit PG opt-in required",
)

LIST = "/version-changes"
JSON_ACCEPT = {"accept": "application/json"}
HTML_ACCEPT = {"accept": "text/html"}
TERMINAL_LABELS = {"healthy", "degraded", "unknown", "cancelled"}
DISCLAIMER = "不批准、不阻断、不执行发布"
#: Words that would be a release gate or an authorization, never a control.
NOT_CONTROLS = (
    "approve",
    "block",
    "rollback",
    "promote",
    "deploy",
    "authorize",
    "approve_release",
    "block_release",
    "restart",
    "scale",
    "PAUSE",
    "",
)


# ===========================================================================
# the app under the web role
# ===========================================================================


@pytest.fixture
def bench(web_dsn: str) -> Iterator[Callable[..., Workbench]]:
    """A Workbench under ``opspilot_web`` whose registry lists exactly the
    given ``{resource_uid: entry}``, with the shipped checkout profile (the
    release intake needs it) and a web-role ``ReleaseStore`` (N3)."""
    stores: list[DurableStore] = []

    def build(entries: Mapping[str, Mapping[str, str]] | None = None) -> Workbench:
        store = DurableStore(web_dsn, pool=POOL)
        store.install()
        stores.append(store)
        events = DurableEventLog(store)
        events.install()
        evidence = DurableEvidenceStore(store)
        evidence.install()
        ledger = DurableWebLedger(store)
        ledger.install()
        profile = select_profile({})
        clock = DurableClock(store)
        return Workbench(
            incidents=DurableIncidentStore(store),
            events=events,
            evidence=evidence,
            ledger=ledger,
            run_versions=profile.versions(),
            tool_face=profile.face(clock),
            run_seconds=600,
            health_profile=PROFILE,
            targets=MappingTargetRegistry(
                {k: dict(v) for k, v in (entries or {}).items()}
            ),
            releases=ReleaseStore(web_dsn, pool=POOL),
        )

    yield build
    for store in stores:
        store.close()


def _app(workbench: Workbench, owner: DurableStore, releases: ReleaseStore) -> Any:
    auth = AuthConfig(
        ui_users={UI_USER: hash_password(UI_PASSWORD, salt=b"fixed-salt-16byt")},
        event_tokens={
            token_digest(EVENT_TOKEN): "alertmanager",
            token_digest(RELEASE_TOKEN): RELEASE_ACTOR,
        },
        auth_revision="f11-pr5a-acceptance",
        allowed_origins=frozenset({ORIGIN}),
    )
    return create_app(
        workbench, Authenticator(auth), DurableClock(owner), releases=releases
    )


@pytest.fixture
def app(bench: Callable[..., Workbench], owner: DurableStore, web: ReleaseStore) -> Any:
    return _app(bench({}), owner, web)


# ===========================================================================
# page helpers (N4, N6)
# ===========================================================================


def _detail_path(oid: UUID) -> str:
    return f"/version-changes/{oid}"


def _control_path(oid: UUID) -> str:
    return f"/version-changes/{oid}/control"


def _get_json(app: Any, path: str) -> Any:
    response = call(app, "GET", path, headers={**basic(), **JSON_ACCEPT})
    assert response.status == 200, (path, response.status, response.text[:500])
    return response.json()


def _get_html(app: Any, path: str) -> str:
    response = call(app, "GET", path, headers={**basic(), **HTML_ACCEPT})
    assert response.status == 200, (path, response.status, response.text[:500])
    assert response.headers.get("content-type", "").startswith("text/html")
    return response.text


def _strings(document: Any) -> list[str]:
    """Every string value (and key) anywhere in a JSON document."""
    found: list[str] = []
    if isinstance(document, str):
        found.append(document)
    elif isinstance(document, Mapping):
        for key, value in document.items():
            found.append(str(key))
            found.extend(_strings(value))
    elif isinstance(document, (list, tuple)):
        for value in document:
            found.extend(_strings(value))
    return found


def _instant(text: str) -> datetime | None:
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def _has_instant(document: Any, moment: datetime) -> bool:
    return any(_instant(s) == moment for s in _strings(document))


def _entry_for(document: Any, oid: UUID) -> Mapping[str, Any]:
    """The innermost object of a list document that names ``oid`` directly."""
    wanted = str(oid)
    best: list[Mapping[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, Mapping):
            if wanted in [v for v in node.values() if isinstance(v, str)]:
                best.append(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, (list, tuple)):
            for value in node:
                walk(value)

    walk(document)
    assert best, f"release {oid} is not in the list document"
    return best[-1]


def _labels(document: Any) -> set[str]:
    return set(_strings(document)) & TERMINAL_LABELS


# ===========================================================================
# control helpers (N2, N5)
# ===========================================================================


def _post_control(
    app: Any,
    oid: UUID,
    action: str,
    expected: int,
    *,
    key: str | None = None,
    headers: Mapping[str, str] | None = None,
) -> Any:
    return post_form(
        app,
        _control_path(oid),
        {
            "action": action,
            "expected_control_generation": str(expected),
            "idempotency_key": key or f"pr5a-{uuid4().hex}",
        },
        headers={**basic(), **same_origin(), **JSON_ACCEPT, **(headers or {})},
    )


def _applied(response: Any) -> Any:
    assert response.status == 200, (response.status, response.text[:500])
    return response.json()


def _refused(response: Any, status: int, code: str) -> None:
    assert response.status == status, (response.status, response.text[:500])
    assert response.json()["code"] == code, response.text[:500]


def _audit(reader: ReleaseStore, oid: UUID) -> tuple[dict[str, Any], ...]:
    return reader.release_audit(oid)


def _samples(oid: UUID) -> int:
    return int(
        _rows(
            "SELECT count(*) AS n FROM opspilot_observation_samples s "
            "JOIN opspilot_observation_sessions o ON o.session_id=s.session_id "
            "WHERE o.release_observation_id=%s",
            (oid,),
        )[0]["n"]
    )


def _sessions(oid: UUID) -> list[dict[str, Any]]:
    return _rows(
        "SELECT session_id,state,deadline_at,observation_generation "
        "FROM opspilot_observation_sessions WHERE release_observation_id=%s "
        "ORDER BY observation_generation,session_id",
        (oid,),
    )


def _authorized(oid: UUID) -> list[dict[str, Any]]:
    return [s for s in _sessions(oid) if s["state"] == "authorized"]


def _poll_finds_nothing_for(observer: ObservationStore, oid: UUID) -> None:
    """Every session of the release made due, one Observer tick against a
    fake Prometheus: no sample of this release, no lease on it."""
    for session in _sessions(oid):
        _make_due(session["session_id"], expire_lease=True)
    before = _samples(oid)
    mine = {s["session_id"] for s in _sessions(oid)}
    results = ObserverLoop(store=observer, source=_world(), batch=50).poll_once()
    assert not [sid for sid, _ in results if sid in mine], results
    assert _samples(oid) == before
    assert not [lease for sid in mine for lease in _claims(observer, sid)]


# ===========================================================================
# D4 / step 1.3: the page keeps the exact before/after versions and identity
# ===========================================================================


def _payload(uid: str, release_id: str, released_at: datetime) -> dict[str, Any]:
    return {
        "format_version": 1,
        "source": "kind-harness-pr5a",
        "source_release_id": release_id,
        "released_at": iso(released_at),
        "target": {**IDENTITY, "resource_uid": uid, "workload": WORKLOAD},
        "before": {
            "images": {"checkout": digest(f"before-image-{release_id}")},
            "pod_template_digest": digest(f"before-template-{release_id}"),
        },
        "after": {
            "images": {"checkout": digest(f"after-image-{release_id}")},
            "pod_template_digest": digest(f"after-template-{release_id}"),
        },
        "auxiliary": {
            "deployment_uid": f"dep-{release_id}",
            "before_replicaset_uid": f"rs-before-{release_id}",
            "after_replicaset_uid": f"rs-after-{release_id}",
            "before_pod_template_hash": BEFORE_HASH,
            "after_pod_template_hash": AFTER_HASH,
            "deployment_generation": 7,
        },
    }


def _exact_values(payload: Mapping[str, Any]) -> list[str]:
    """Values the page must show byte for byte (step 1.3)."""
    values = [payload["source"], payload["source_release_id"]]
    values += [payload["target"][k] for k in sorted(payload["target"])]
    for side in ("before", "after"):
        values += sorted(payload[side]["images"].values())
        values.append(payload[side]["pod_template_digest"])
    return values


def test_step1_3_the_page_shows_the_delivered_versions_and_identity_exactly(
    bench: Callable[..., Workbench], owner: DurableStore, web: ReleaseStore
) -> None:
    uid = f"deployment/checkout-pr5a-{uuid4().hex[:10]}"
    workbench = bench({uid: _registry_entry()})
    app = _app(workbench, owner, web)
    released_at = _db_now() - timedelta(minutes=5)
    payload = _payload(uid, f"pr5a-{uuid4().hex[:12]}", released_at)
    body = json.dumps(payload, sort_keys=True).encode()
    delivered = call(
        app,
        "POST",
        "/intake/version-changes",
        headers={"content-type": "application/json", **bearer_for(RELEASE_TOKEN)},
        body=body,
    )
    assert delivered.status == 201, delivered.text
    oid = UUID(delivered.json()["observation_id"])
    record = web.get_release(oid)

    document = _get_json(app, _detail_path(oid))
    page = _get_html(app, _detail_path(oid))

    strings = _strings(document)
    for value in _exact_values(payload):
        assert value in strings, (value, document)
        assert value in page, value
    assert _has_instant(document, record.released_at), document
    assert _has_instant(document, record.deadline_at), document
    assert str(oid) in strings
    assert DISCLAIMER in page
    # D4: the auxiliary identity is shown too
    for value in (BEFORE_HASH, AFTER_HASH, f"rs-after-{payload['source_release_id']}"):
        assert value in strings, value
        assert value in page, value
    # pending is not a verdict (D5)
    assert _labels(_entry_for(_get_json(app, LIST), oid)) == set()


def test_d4_an_observed_release_page_shows_its_frozen_policy_and_control_form(
    app: Any, owner: DurableStore, web: ReleaseStore, catalog: Catalog
) -> None:
    observing = _observing(owner, web, catalog)

    document = _get_json(app, _detail_path(observing.oid))
    page = _get_html(app, _detail_path(observing.oid))

    assert observing.revision in _strings(document)
    assert observing.revision in page
    # D11: the page posts the control with its generation and a fresh key
    assert _control_path(observing.oid) in page
    assert "expected_control_generation" in page
    assert "idempotency_key" in page
    assert _labels(_entry_for(_get_json(app, LIST), observing.oid)) == set()


def test_d1_an_unknown_or_malformed_release_id_is_404(app: Any) -> None:
    for path in (_detail_path(uuid4()), "/version-changes/not-a-uuid"):
        response = call(app, "GET", path, headers={**basic(), **JSON_ACCEPT})
        assert response.status == 404, (path, response.status, response.text)
    response = _post_control(app, uuid4(), "pause", 0)
    assert response.status == 404, response.text


# ===========================================================================
# D6: the list, newest first
# ===========================================================================


def test_d6_the_list_shows_releases_newest_received_first(
    app: Any, owner: DurableStore, web: ReleaseStore
) -> None:
    older = _record_release(owner, web)
    newer = _record_release(owner, web)
    assert newer.record.received_at > older.record.received_at

    document = _get_json(app, LIST)
    page = _get_html(app, LIST)

    text = json.dumps(document)
    assert text.index(str(newer.oid)) < text.index(str(older.oid))
    assert page.index(str(newer.oid)) < page.index(str(older.oid))
    assert _detail_path(newer.oid) in page
    assert newer.record.source_release_id in page


# ===========================================================================
# D5: projection labels
# ===========================================================================


def _healthy(
    owner: DurableStore, web: ReleaseStore, observer: ObservationStore, catalog: Catalog
) -> Observing:
    """Three contiguous sufficient samples (as the PR-2a/PR-4b tests do)."""
    healthy = _observing(owner, web, catalog, released_ago=2000)
    authorized_at = healthy.release.record.released_at + timedelta(seconds=500)
    _set_authorized_at(healthy.session_id, authorized_at)
    pods = pods_for(AFTER_IMAGES)
    receipt = None
    for offset in (300, 600, 900):
        window_end = authorized_at + timedelta(seconds=offset)
        lease = _claim(observer, healthy.session_id)
        context = observer.release_context(healthy.session_id)
        texts = planned_texts(context, window_end, [p.uid for p in pods])
        evidence = (
            EvidenceBuilder(window_end=window_end, texts=texts)
            .sufficient(pods)
            .evidence()
        )
        receipt = observer.submit_release_sample(lease, evidence)
    assert receipt is not None and receipt.release_status == "healthy", receipt
    return healthy


def _assert_label(app: Any, oid: UUID, label: str, *extra: str) -> None:
    entry = _entry_for(_get_json(app, LIST), oid)
    assert _labels(entry) == {label}, entry
    document = _get_json(app, _detail_path(oid))
    assert label in _strings(document), document
    page = _get_html(app, _detail_path(oid))
    assert label in page
    for value in extra:
        assert value in _strings(document), (value, document)
        assert value in page, value


def test_d5_healthy_is_shown_healthy(
    app: Any,
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
) -> None:
    healthy = _healthy(owner, web, observer, catalog)

    _assert_label(app, healthy.oid, "healthy")


def test_d5_anomalous_is_shown_degraded(
    app: Any,
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
) -> None:
    anomaly = _anomalous(owner, web, observer, catalog)
    assert web.get_release(anomaly.oid).status == "anomalous"  # stored value kept

    _assert_label(app, anomaly.oid, "degraded")


def test_d5_an_expired_release_is_unknown_with_its_reason(
    app: Any, owner: DurableStore, web: ReleaseStore, observer: ObservationStore
) -> None:
    expiring = _expiring(owner, web, seconds=3.0)
    _wait_past(expiring.record.deadline_at)
    observer.sweep_expired_releases(limit=200)
    assert web.get_release(expiring.oid).status == "unknown"

    _assert_label(app, expiring.oid, "unknown", "deadline_expired")


def test_d5_a_cancelled_release_is_shown_cancelled(
    app: Any, owner: DurableStore, web: ReleaseStore, catalog: Catalog
) -> None:
    observing = _observing(owner, web, catalog)
    _applied(_post_control(app, observing.oid, "cancel", 0))

    _assert_label(app, observing.oid, "cancelled")


# ===========================================================================
# step 4.2 / D2: the HTTP control
# ===========================================================================


def test_pause_over_http_stops_sampling_and_is_audited_as_the_operator(
    app: Any,
    owner: DurableStore,
    web: ReleaseStore,
    reader: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
) -> None:
    observing = _observing(owner, web, catalog)
    _claim(observer, observing.session_id)  # positive control: sampling is due

    _applied(_post_control(app, observing.oid, "pause", 0))

    record = web.get_release(observing.oid)
    assert record.paused is True
    assert record.status == "observing"
    assert record.control_generation == 1
    assert _authorized(observing.oid) == []
    last = _audit(reader, observing.oid)[-1]
    assert (last["action"], last["outcome"]) == ("pause", "applied"), last
    assert (last["actor"], last["principal_kind"]) == (UI_USER, "basic_auth"), last
    _poll_finds_nothing_for(observer, observing.oid)


def test_resume_over_http_opens_a_new_session_with_the_same_deadline(
    app: Any,
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
) -> None:
    observing = _observing(owner, web, catalog)
    deadline = web.get_release(observing.oid).deadline_at
    _applied(_post_control(app, observing.oid, "pause", 0))
    _poll_finds_nothing_for(observer, observing.oid)

    # D3: the page names no revision; the frozen one is used
    _applied(_post_control(app, observing.oid, "resume", 1))

    record = web.get_release(observing.oid)
    assert (record.status, record.paused) == ("observing", False)
    assert record.deadline_at == deadline
    assert record.calibrated_policy_revision == observing.revision
    (session,) = _authorized(observing.oid)
    assert session["session_id"] != observing.session_id
    assert session["deadline_at"] == deadline
    assert len(_sessions(observing.oid)) == 2
    _claim(observer, session["session_id"])  # sampling is due again


def test_cancel_over_http_ends_the_release_and_sampling(
    app: Any,
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
) -> None:
    observing = _observing(owner, web, catalog)

    _applied(_post_control(app, observing.oid, "cancel", 0))

    record = web.get_release(observing.oid)
    assert (record.status, record.ended_reason) == ("cancelled", "cancelled")
    assert _authorized(observing.oid) == []
    _poll_finds_nothing_for(observer, observing.oid)


def test_a_stale_generation_is_409_control_conflict_and_changes_nothing(
    app: Any, owner: DurableStore, web: ReleaseStore, catalog: Catalog
) -> None:
    observing = _observing(owner, web, catalog)
    before = web.get_release(observing.oid)

    _refused(_post_control(app, observing.oid, "pause", 3), 409, "CONTROL_CONFLICT")

    assert _state(web.get_release(observing.oid)) == _state(before)
    assert len(_authorized(observing.oid)) == 1


def test_the_same_key_and_request_replays_the_original_result(
    app: Any,
    owner: DurableStore,
    web: ReleaseStore,
    reader: ReleaseStore,
    catalog: Catalog,
) -> None:
    observing = _observing(owner, web, catalog)
    key = f"pr5a-replay-{uuid4().hex}"
    first = _applied(_post_control(app, observing.oid, "pause", 0, key=key))
    after_first = web.get_release(observing.oid)
    audit = len(_audit(reader, observing.oid))

    again = _applied(_post_control(app, observing.oid, "pause", 0, key=key))

    strip = {"replayed"}
    assert {k: v for k, v in again.items() if k not in strip} == {
        k: v for k, v in first.items() if k not in strip
    }
    assert _state(web.get_release(observing.oid)) == _state(after_first)
    assert len(_audit(reader, observing.oid)) == audit


def test_the_same_key_with_another_request_is_409_and_changes_nothing(
    app: Any,
    owner: DurableStore,
    web: ReleaseStore,
    reader: ReleaseStore,
    catalog: Catalog,
) -> None:
    observing = _observing(owner, web, catalog)
    key = f"pr5a-conflict-{uuid4().hex}"
    _applied(_post_control(app, observing.oid, "pause", 0, key=key))
    before = web.get_release(observing.oid)
    audit = len(_audit(reader, observing.oid))

    _refused(
        _post_control(app, observing.oid, "cancel", 1, key=key),
        409,
        "IDEMPOTENCY_CONFLICT",
    )

    assert _state(web.get_release(observing.oid)) == _state(before)
    assert len(_audit(reader, observing.oid)) == audit


@pytest.mark.parametrize("action", ["pause", "resume", "cancel"])
def test_a_terminal_release_is_409_release_terminal(
    app: Any,
    owner: DurableStore,
    web: ReleaseStore,
    catalog: Catalog,
    action: str,
) -> None:
    observing = _observing(owner, web, catalog)
    _applied(_post_control(app, observing.oid, "cancel", 0))
    before = web.get_release(observing.oid)

    _refused(
        _post_control(app, observing.oid, action, before.control_generation),
        409,
        "RELEASE_TERMINAL",
    )

    assert _state(web.get_release(observing.oid)) == _state(before)


@pytest.mark.parametrize("action", NOT_CONTROLS)
def test_any_action_but_pause_resume_cancel_is_400_and_writes_nothing(
    app: Any,
    owner: DurableStore,
    web: ReleaseStore,
    reader: ReleaseStore,
    catalog: Catalog,
    action: str,
) -> None:
    observing = _observing(owner, web, catalog)
    before = web.get_release(observing.oid)
    audit = len(_audit(reader, observing.oid))
    sessions = _sessions(observing.oid)

    _refused(_post_control(app, observing.oid, action, 0), 400, "INVALID_INPUT")

    assert _state(web.get_release(observing.oid)) == _state(before)
    assert len(_audit(reader, observing.oid)) == audit
    assert _sessions(observing.oid) == sessions


@pytest.mark.parametrize(
    "headers",
    [{"origin": "https://evil.example.test"}, {"origin": ""}],
    ids=["cross-site", "empty"],
)
def test_a_control_without_the_workbench_origin_writes_nothing(
    app: Any,
    owner: DurableStore,
    web: ReleaseStore,
    reader: ReleaseStore,
    catalog: Catalog,
    headers: dict[str, str],
) -> None:
    observing = _observing(owner, web, catalog)
    before = web.get_release(observing.oid)
    audit = len(_audit(reader, observing.oid))

    response = _post_control(app, observing.oid, "cancel", 0, headers=headers)

    assert response.status == 403, response.text
    assert _state(web.get_release(observing.oid)) == _state(before)
    assert len(_audit(reader, observing.oid)) == audit


def test_d12_event_tokens_cannot_read_or_control_a_real_release(
    app: Any, owner: DurableStore, web: ReleaseStore, catalog: Catalog
) -> None:
    observing = _observing(owner, web, catalog)
    before = web.get_release(observing.oid)
    for token, statuses in ((EVENT_TOKEN, {401}), (RELEASE_TOKEN, {401, 403})):
        auth = {**bearer_for(token), **same_origin(), **JSON_ACCEPT}
        for path in (LIST, _detail_path(observing.oid)):
            response = call(app, "GET", path, headers=auth)
            assert response.status in statuses, (token, path, response.status)
            assert str(observing.oid) not in response.text
        response = post_form(
            app,
            _control_path(observing.oid),
            {
                "action": "cancel",
                "expected_control_generation": "0",
                "idempotency_key": f"pr5a-{uuid4().hex}",
            },
            headers=auth,
        )
        assert response.status in statuses, (token, response.status)
    assert _state(web.get_release(observing.oid)) == _state(before)


# ===========================================================================
# D3: resume of a release paused before it was ever authorized
# ===========================================================================


def _pending_entry(release: Any, **overrides: Any) -> dict[str, Any]:
    return policy_entry(
        policy_content(
            release.identity,
            min_tracking_seconds=release.record.min_tracking_seconds,
            **overrides,
        )
    )


def test_d3_resume_of_a_never_authorized_release_uses_the_unique_policy(
    app: Any, owner: DurableStore, web: ReleaseStore, catalog: Catalog
) -> None:
    pending = _record_release(owner, web)
    _applied(_post_control(app, pending.oid, "pause", 0))
    entry = _pending_entry(pending)
    catalog.add(entry)

    _applied(_post_control(app, pending.oid, "resume", 1))

    record = web.get_release(pending.oid)
    assert (record.status, record.paused) == ("observing", False)
    assert record.calibrated_policy_revision == entry["revision"]
    assert len(_authorized(pending.oid)) == 1


@pytest.mark.parametrize(
    ("entries", "code"),
    [(0, "RELEASE_POLICY_UNCALIBRATED"), (2, "RELEASE_POLICY_AMBIGUOUS")],
)
def test_d3_without_one_matching_policy_resume_is_422_and_not_attempted(
    app: Any,
    owner: DurableStore,
    web: ReleaseStore,
    reader: ReleaseStore,
    catalog: Catalog,
    entries: int,
    code: str,
) -> None:
    pending = _record_release(owner, web)
    _applied(_post_control(app, pending.oid, "pause", 0))
    catalog.empty()
    for n in range(entries):
        catalog.add(_pending_entry(pending, approval_ref=f"test-only {n}"))
    before = web.get_release(pending.oid)
    audit = len(_audit(reader, pending.oid))

    _refused(_post_control(app, pending.oid, "resume", 1), 422, code)

    assert _state(web.get_release(pending.oid)) == _state(before)
    assert len(_audit(reader, pending.oid)) == audit  # the primitive was not called
    assert _sessions(pending.oid) == []


# ===========================================================================
# D9 / step 1.4: release page and incident page link each other
# ===========================================================================


def test_d9_the_release_and_its_incident_link_each_other(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
    bench: Callable[..., Workbench],
) -> None:
    case, workbench, incident, _ = _created_case(owner, web, observer, catalog, bench)
    link = _link(case.oid)
    incident_id = incident["incident_id"]
    app = _app(workbench, owner, web)

    document = _get_json(app, _detail_path(case.oid))
    page = _get_html(app, _detail_path(case.oid))
    incident_page = _get_html(app, f"/incidents/{incident_id}")

    assert str(incident_id) in _strings(document), document
    assert link["disposition"] in _strings(document), document
    assert f"/incidents/{incident_id}" in page
    assert _detail_path(case.oid) in incident_page
    _assert_label(app, case.oid, "degraded")
