"""Independent acceptance tests for F11 PR-3, no PostgreSQL.

Covers contract behaviours 2, 3, 5 and 10 and the "refused before storage"
half of 1, 6 and 15 of ``docs/tasks/2026-10-21-f11-pr3-contract.md`` through
the public ``POST /intake/version-changes`` route. Storage is a recorder that
answers like an unavailable database, so a refusal that must happen before
any write is observable as ``calls == []`` and a request that reaches storage
is observable as a 503. The PostgreSQL half lives in
``tests/integration/test_f11_pr3_intake_postgres.py``; harness behaviours
12-14 need kind (see the end of this file).

Ambiguities (reported, tests use the strictest reasonable reading):

* A0  how ``create_app`` receives the ReleaseStore (see ``f11_pr3_support``).
* A1  behaviour 3 says an unknown business field is a 400, behaviour 4 / D1
  say unknown fields go to the audit text only. Reading used: unknown keys
  inside ``target``/``before``/``after``/``auxiliary`` are 400; an unknown
  top-level key is accepted (PG file). The top-level case is its own test.
* A2  the JSON key carrying the error code (``code`` or ``error``).
* A3  resolved by contract r2: resource_uid is required (400 if missing);
  TARGET_AMBIGUOUS means the uid hits entry A while another entry B shares
  (cluster_uid, namespace, workload).
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from tests.f11_pr3_support import (
    ALERT_TOKEN,
    OTHER_TOKEN,
    REGISTRY,
    RELEASE_TOKEN,
    TARGET,
    RecordingReleaseStore,
    assert_route_exists,
    build_app,
    call,
    error_code,
    post_release,
    raw,
    release_payload,
    utc_now,
)
from tests.m1_web_support import basic

# --- 2. authentication and the dedicated actor -----------------------------


def test_missing_credentials_is_401_before_anything_else() -> None:
    store = RecordingReleaseStore()
    app, _ = build_app(store)
    response = post_release(app, token=None)
    assert_route_exists(response)
    assert response.status == 401
    assert error_code(response) == "MISSING_CREDENTIALS"
    assert store.calls == []


def test_unknown_token_is_401() -> None:
    store = RecordingReleaseStore()
    app, _ = build_app(store)
    response = post_release(app, token="not-a-configured-token")
    assert_route_exists(response)
    assert response.status == 401
    assert error_code(response) == "INVALID_CREDENTIALS"
    assert "not-a-configured-token" not in response.text
    assert store.calls == []


def test_ui_basic_credentials_do_not_reach_the_release_route() -> None:
    store = RecordingReleaseStore()
    app, _ = build_app(store)
    response = post_release(app, token=None, headers=basic())
    assert_route_exists(response)
    assert response.status == 401
    assert store.calls == []


@pytest.mark.parametrize(
    "token", [ALERT_TOKEN, OTHER_TOKEN], ids=["alertmanager", "other"]
)
def test_only_the_release_deployer_actor_may_submit(token: str) -> None:
    """U4: another valid event actor is 403 RELEASE_ACTOR_REQUIRED, and the
    refusal happens before parsing, target resolution or storage."""
    store = RecordingReleaseStore()
    app, workbench = build_app(store)
    response = post_release(app, token=token)
    assert_route_exists(response)
    assert response.status == 403
    assert error_code(response) == "RELEASE_ACTOR_REQUIRED"
    assert store.calls == []
    assert workbench.incidents.incidents == {}
    assert workbench.incidents.runs == {}


def test_actor_is_refused_even_with_an_invalid_body() -> None:
    """Authentication/authorization precede parsing: a wrong actor learns
    nothing about payload validity."""
    store = RecordingReleaseStore()
    app, _ = build_app(store)
    response = post_release(app, body=b"not json", token=ALERT_TOKEN)
    assert_route_exists(response)
    assert response.status == 403
    assert error_code(response) == "RELEASE_ACTOR_REQUIRED"
    assert store.calls == []


# --- 3. payload parsing ----------------------------------------------------


def _without(*path: str):
    def mutate(payload: dict) -> None:
        node = payload
        for key in path[:-1]:
            node = node[key]
        del node[path[-1]]

    return mutate


def _set(path: tuple[str, ...], value):
    def mutate(payload: dict) -> None:
        node = payload
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value

    return mutate


def _rename(old: str, new: str):
    def mutate(payload: dict) -> None:
        payload[new] = payload.pop(old)

    return mutate


INVALID_MUTATIONS = {
    "missing-source-release-id": _without("source_release_id"),
    "empty-source-release-id": _set(("source_release_id",), ""),
    "missing-released-at": _without("released_at"),
    "released-at-without-timezone": _set(("released_at",), "2026-10-21T12:00:00"),
    "released-at-not-a-time": _set(("released_at",), "yesterday"),
    # P3-01: no alias; event_time is not released_at.
    "event-time-alias": _rename("released_at", "event_time"),
    "missing-source": _without("source"),
    "format-version-2": _set(("format_version",), 2),
    "missing-format-version": _without("format_version"),
    "missing-target": _without("target"),
    "missing-before": _without("before"),
    "missing-after": _without("after"),
    "image-digest-not-sha256": _set(("after", "images"), {"checkout": "latest"}),
    "image-digest-uppercase": _set(
        ("after", "images"), {"checkout": "sha256:" + "A" * 64}
    ),
    "image-digest-short": _set(("before", "images"), {"checkout": "sha256:abc"}),
    "empty-images": _set(("after", "images"), {}),
    "template-digest-invalid": _set(("after", "pod_template_digest"), "sha256:zz"),
    "generation-negative": _set(("auxiliary", "deployment_generation"), -1),
    # A1: unknown keys inside business objects.
    "unknown-key-in-after": _set(("after", "surprise"), "x"),
    "unknown-key-in-before": _set(("before", "surprise"), "x"),
    "unknown-key-in-auxiliary": _set(("auxiliary", "surprise"), "x"),
    "unknown-key-in-target": _set(("target", "surprise"), "x"),
    "nul-in-source": _set(("source",), "kind\x00harness"),
}


@pytest.mark.parametrize("name", sorted(INVALID_MUTATIONS))
def test_invalid_payload_is_400_and_never_reaches_storage(name: str) -> None:
    payload = release_payload()
    INVALID_MUTATIONS[name](payload)
    store = RecordingReleaseStore()
    app, workbench = build_app(store)
    response = post_release(app, payload)
    assert_route_exists(response)
    assert response.status == 400, response.text
    assert error_code(response) == "INVALID_RELEASE_PAYLOAD"
    assert store.calls == []
    assert workbench.incidents.incidents == {}


@pytest.mark.parametrize(
    "body",
    [b"", b"not json", b"[]", b'"x"', b"null", b"\xff\xfe", b'{"a":'],
    ids=["empty", "text", "array", "string", "null", "not-utf8", "truncated"],
)
def test_non_object_json_is_400(body: bytes) -> None:
    store = RecordingReleaseStore()
    app, _ = build_app(store)
    response = post_release(app, body=body)
    assert_route_exists(response)
    assert response.status == 400
    assert error_code(response) == "INVALID_RELEASE_PAYLOAD"
    assert store.calls == []


def test_source_release_id_is_not_derived_from_the_deployment_generation() -> None:
    """P3-01: a payload with auxiliary.deployment_generation but no
    source_release_id is refused, not given an inferred identity."""
    payload = release_payload()
    del payload["source_release_id"]
    assert payload["auxiliary"]["deployment_generation"] == 12
    store = RecordingReleaseStore()
    app, _ = build_app(store)
    response = post_release(app, payload)
    assert_route_exists(response)
    assert response.status == 400
    assert store.calls == []


def test_released_at_is_not_replaced_by_the_reception_time() -> None:
    payload = release_payload()
    del payload["released_at"]
    store = RecordingReleaseStore()
    app, _ = build_app(store)
    response = post_release(app, payload)
    assert_route_exists(response)
    assert response.status == 400
    assert store.calls == []


# --- body cap (D4) ---------------------------------------------------------


def test_body_above_the_release_cap_is_413_before_storage() -> None:
    """D4: independent cap of 128 KiB; the 1 MiB Alertmanager cap is not
    inherited."""
    store = RecordingReleaseStore()
    app, _ = build_app(store)
    body = raw(release_payload()) + b" " * (200 * 1024)
    response = post_release(app, body=body)
    assert_route_exists(response)
    assert response.status == 413
    assert error_code(response) == "BODY_TOO_LARGE"
    assert store.calls == []


def test_body_between_64_and_128_kib_is_not_size_refused() -> None:
    """The release cap is wider than the generic 64 KiB one. Whatever the
    verdict on this body (it carries padding), it is not a 413."""
    store = RecordingReleaseStore()
    app, _ = build_app(store)
    body = raw(release_payload()) + b" " * (100 * 1024)
    response = post_release(app, body=body)
    assert_route_exists(response)
    assert response.status != 413


# --- 5. target resolution (U6): fail closed, no store call -----------------


def test_unregistered_target_is_422_unresolved() -> None:
    target = {**TARGET, "resource_uid": "deployment/never-registered"}
    store = RecordingReleaseStore()
    app, workbench = build_app(store)
    response = post_release(app, release_payload(target=target))
    assert_route_exists(response)
    assert response.status == 422
    assert error_code(response) == "TARGET_UNRESOLVED"
    assert store.calls == []
    assert workbench.incidents.incidents == {}


@pytest.mark.parametrize(
    "field", ["integration_id", "cluster_uid", "namespace", "workload"]
)
def test_field_contradicting_the_registry_is_422_mismatch(field: str) -> None:
    target = {**TARGET, field: "somewhere-else"}
    store = RecordingReleaseStore()
    app, _ = build_app(store)
    response = post_release(app, release_payload(target=target))
    assert_route_exists(response)
    assert response.status == 422
    assert error_code(response) == "TARGET_MISMATCH"
    assert store.calls == []


def test_payload_cannot_choose_a_target_by_uuid_or_registry_alias() -> None:
    """U6 rejects trusting a payload UUID: a target_id key is an unknown
    business field, and nothing is stored."""
    payload = release_payload()
    payload["target"]["target_id"] = "00000000-0000-0000-0000-000000000001"
    store = RecordingReleaseStore()
    app, _ = build_app(store)
    response = post_release(app, payload)
    assert_route_exists(response)
    assert response.status in {400, 422}
    assert store.calls == []


def test_uid_hit_with_another_entry_on_the_same_workload_is_422_ambiguous() -> None:
    """Contract r2: resource_uid hits entry A while (cluster_uid, namespace,
    workload) also equals a different entry B; fail closed."""
    twin = dict(REGISTRY[TARGET["resource_uid"]])
    registry = {**REGISTRY, "deployment/checkout-twin": twin}
    store = RecordingReleaseStore()
    app, _ = build_app(store, registry=registry)
    response = post_release(app, release_payload())
    assert_route_exists(response)
    assert response.status == 422
    assert error_code(response) == "TARGET_AMBIGUOUS"
    assert store.calls == []


def test_missing_resource_uid_is_400() -> None:
    """Contract r2: resource_uid is required."""
    target = {k: v for k, v in TARGET.items() if k != "resource_uid"}
    store = RecordingReleaseStore()
    app, _ = build_app(store)
    response = post_release(app, release_payload(target=target))
    assert_route_exists(response)
    assert response.status == 400
    assert error_code(response) == "INVALID_RELEASE_PAYLOAD"
    assert store.calls == []


def test_refusal_bodies_do_not_leak_registry_contents() -> None:
    target = {**TARGET, "resource_uid": "deployment/never-registered"}
    store = RecordingReleaseStore()
    app, _ = build_app(store)
    response = post_release(app, release_payload(target=target))
    assert_route_exists(response)
    assert "payment-pr3" not in response.text
    assert RELEASE_TOKEN not in response.text


# --- 6. time ---------------------------------------------------------------


def test_released_at_one_day_ahead_is_never_stored_as_a_valid_release() -> None:
    """Behaviour 6 needs the database clock (PG file). Without a database
    the request must at least not succeed."""
    payload = release_payload(released_at=utc_now() + timedelta(days=1))
    store = RecordingReleaseStore()
    app, _ = build_app(store)
    response = post_release(app, payload)
    assert_route_exists(response)
    assert response.status not in {200, 201}


# --- 10. storage failure ---------------------------------------------------


@pytest.mark.parametrize("code", ["STORAGE_UNAVAILABLE", "RETRY", "TIMEOUT"])
def test_storage_failure_is_503_storage_unavailable(code: str) -> None:
    store = RecordingReleaseStore(code)
    app, workbench = build_app(store)
    response = post_release(app)
    assert_route_exists(response)
    assert response.status == 503, response.text
    assert error_code(response) == "STORAGE_UNAVAILABLE"
    assert store.calls, "a valid, resolved release must reach storage"
    assert workbench.incidents.incidents == {}
    assert workbench.incidents.runs == {}
    # No internal exception text or driver detail is returned.
    assert "Traceback" not in response.text
    assert "psycopg" not in response.text


# --- 15. old intake unchanged ----------------------------------------------


def test_old_event_intake_still_refuses_basic_credentials() -> None:
    store = RecordingReleaseStore()
    app, _ = build_app(store)
    response = call(
        app,
        "POST",
        "/intake/events",
        headers={"content-type": "application/json", **basic()},
        body=json.dumps(
            {
                "source": "alertmanager",
                "external_event_id": "e-1",
                "target_id": "t",
                "question": "q",
            }
        ).encode(),
    )
    assert response.status == 401
    assert store.calls == []


def test_alertmanager_token_cannot_submit_a_release() -> None:
    store = RecordingReleaseStore()
    app, _ = build_app(store)
    forbidden = post_release(app, token=ALERT_TOKEN)
    assert_route_exists(forbidden)
    assert forbidden.status == 403
    assert store.calls == []


# --- 16. passes flips only with PR-5c ------------------------------------


def test_f11_passes_is_flipped_by_pr5c() -> None:
    """PR-3 kept ``passes`` false; PR-5c flips it once all four steps
    passed with evidence (docs/evidence/f11-acceptance-live/)."""
    features = json.loads(
        (Path(__file__).resolve().parents[1] / "feature_list.json").read_text()
    )
    f11 = next(f for f in features if f["id"] == "F11")
    assert f11["passes"] is True


# --- 12-14 need kind -------------------------------------------------------
#
# Behaviour 12 (harness patch records before/after digest, ReplicaSet UID,
# Deployment generation, released_at), 13 (harness sends the webhook over real
# HTTP; response, actor, raw/semantic hash and ledger agree) and 14 (the
# harness's own rollout is labelled as the experiment action; the product path
# wrote no CI, gate, rollout or traffic policy) and 16 (F11 passes stays false
# until the evidence is complete) are asserted against a real cluster by the
# lead. The harness's interface for them is not in the contract, so no fake
# based contract test is written here (ambiguity A4).
