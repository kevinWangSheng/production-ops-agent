"""F11 step 1: "Trigger investigation from an observed rollout and verify
exact before/after revisions and service identities are retained".

Independent acceptance tests for ``docs/tasks/2026-10-24-f11-pr5-contract.md``
section 4, step 1, items 1-6 (U-1 = A: the web pass authorizes as
``system``; D1 routes, D4 page content, D7 pass, D9 incident back link,
D12 access). Harness and conventions: ``tests/acceptance/f11_steps_support.py``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from opspilot.investigation.context import INPUT_VERSION_RELEASE, RELEASE_OBSERVATION
from tests.acceptance import f11_steps_support as s
from tests.f11_pr3_support import ALERT_ACTOR, RELEASE_TOKEN, bearer_for
from tests.m1_web_support import EVENT_TOKEN, basic, bearer, call, same_origin

pytestmark = s.PG

SYSTEM = "system"


@pytest.fixture(scope="module")
def env(tmp_path_factory: pytest.TempPathFactory) -> Any:
    with s.module_env(Path(tmp_path_factory.mktemp("f11-step1"))) as built:
        yield built


def _authorize_rows(env: s.Env, oid: UUID) -> list[dict[str, Any]]:
    return [row for row in env.audit(oid) if row["action"] == "authorize"]


def _assert_observing_once(env: s.Env, oid: UUID) -> None:
    record = env.record(oid)
    assert (record.status, record.paused) == ("observing", False), record
    sessions = env.sessions(oid)
    assert [x["state"] for x in sessions] == ["authorized"], sessions
    rows = _authorize_rows(env, oid)
    assert len(rows) == 1, rows
    assert (rows[0]["outcome"], rows[0]["reason"]) == ("applied", None), rows[0]
    assert rows[0]["principal_kind"] == SYSTEM, rows[0]
    assert rows[0]["actor"] == SYSTEM, rows[0]


# ===========================================================================
# item 1: intake -> pending -> one automatic pass -> observing, once
# ===========================================================================


def test_item1_an_observed_rollout_is_recorded_pending_then_observed_once_as_system(
    env: s.Env,
) -> None:
    case = s.deliver(env)
    assert env.sessions(case.oid) == []
    assert _authorize_rows(env, case.oid) == []

    env.authorize_pass()

    _assert_observing_once(env, case.oid)
    record = env.record(case.oid)
    assert record.calibrated_policy_revision == case.entry["revision"]  # type: ignore[index]
    audit = env.audit(case.oid)
    sessions = env.sessions(case.oid)

    for _ in range(3):
        env.authorize_pass()

    assert env.audit(case.oid) == audit
    assert env.sessions(case.oid) == sessions
    _assert_observing_once(env, case.oid)


# ===========================================================================
# item 2: the same payload again is a replay, not a second observation
# ===========================================================================


def test_item2_redelivering_the_same_payload_replays_without_a_second_session(
    env: s.Env,
) -> None:
    case, _ = s.observing(env)
    app = env.app()

    again = s.intake(app, case.body)
    env.authorize_pass()

    assert again.status == 200, again.text
    document = again.json()
    assert document.get("replayed") is True or "replayed" in s.strings(document)
    assert UUID(document["observation_id"]) == case.oid
    assert (
        env.count(
            "SELECT count(*) AS n FROM opspilot_release_observations "
            "WHERE source=%s AND source_release_id=%s",
            (case.payload["source"], case.payload["source_release_id"]),
        )
        == 1
    )
    _assert_observing_once(env, case.oid)


# ===========================================================================
# item 3: the page keeps every delivered value byte for byte
# ===========================================================================


def _exact_values(payload: dict[str, Any]) -> list[str]:
    values = [payload["source"], payload["source_release_id"]]
    values += [payload["target"][k] for k in sorted(payload["target"])]
    for side in ("before", "after"):
        values += sorted(payload[side]["images"].values())
        values.append(payload[side]["pod_template_digest"])
    return values


@pytest.mark.parametrize("stage", ["pending", "observing"])
def test_item3_html_and_json_pages_show_versions_and_identity_exactly(
    env: s.Env, stage: str
) -> None:
    case = s.deliver(env, calibrate=stage == "observing")
    if stage == "observing":
        env.authorize_pass()
        assert env.record(case.oid).status == "observing"
    app = env.app()

    document = s.get_json(app, s.detail_path(case.oid))
    page = s.get_html(app, s.detail_path(case.oid))

    found = s.strings(document)
    for value in _exact_values(case.payload):
        assert value in found, (value, document)
        assert value in page, value
    assert s.has_instant(document, case.record.released_at)
    assert case.record.released_at == s.instant_of(case.payload["released_at"])
    assert s.DISCLAIMER in page
    # the auxiliary identity (Deployment / ReplicaSet UIDs, hashes) too (D4)
    for value in ("deployment_uid", "before_replicaset_uid", "after_replicaset_uid"):
        assert case.payload["auxiliary"][value] in found
        assert case.payload["auxiliary"][value] in page
    for value in (s.BEFORE_HASH, s.AFTER_HASH):
        assert value in found and value in page


def test_item3_released_at_is_shown_byte_for_byte_as_delivered(env: s.Env) -> None:
    """PRODUCT GAP (contract section 4 step 1 item 3: ``released_at`` 与载荷
    **逐字节相等**). The payload carries ``released_at`` as an explicit-offset
    UTC instant (PR-3 r2 normalizes it to UTC). The page must show those
    bytes whatever the database session time zone is; this module pins a
    non-UTC zone (``f11_steps_support.DB_TIME_ZONE``). Observed on main
    377e091: the page renders the instant in the session zone (e.g.
    ``...-07:00``), the same instant but not the delivered bytes."""
    case = s.deliver(env)
    app = env.app()

    document = s.get_json(app, s.detail_path(case.oid))
    page = s.get_html(app, s.detail_path(case.oid))

    assert case.payload["released_at"] in s.strings(document), document["released_at"]
    assert case.payload["released_at"] in page


# ===========================================================================
# item 4: a regression opens one incident and one Run with the same facts
# ===========================================================================


def _regressed(env: s.Env) -> tuple[s.Delivered, UUID, s.ReleaseWorld, Any]:
    """Healthy samples, then a degraded one taken by the Observer tick; the
    tick also collects the PR-4a comparison; one anomaly pass consumes."""
    regression = s.regress(env)
    env.anomaly_pass()
    return regression.case, regression.session_id, regression.world, regression.pods


def test_item4_a_regression_opens_one_incident_and_run_with_the_same_versions(
    env: s.Env,
) -> None:
    case, _, world, pods = _regressed(env)

    incidents = env.incidents_on(case.target_id)
    assert len(incidents) == 1, incidents
    incident_id = incidents[0]["incident_id"]
    runs = env.runs_of(incident_id)
    assert len(runs) == 1, runs
    (link,) = env.links(case.oid)
    assert link["incident_id"] == incident_id
    assert env.record(case.oid).incident_id == incident_id

    run_input = runs[0]["input"]
    assert run_input["version"] == INPUT_VERSION_RELEASE
    fact = json.dumps(run_input["scope_facts"][RELEASE_OBSERVATION], sort_keys=True)
    payload = case.payload
    for value in (
        str(case.oid),
        payload["source"],
        payload["source_release_id"],
        *[payload["target"][k] for k in sorted(payload["target"])],
        *payload["before"]["images"].values(),
        *payload["after"]["images"].values(),
        payload["before"]["pod_template_digest"],
        payload["after"]["pod_template_digest"],
    ):
        assert value in fact, value
    # new instances (the sampled pods) and old instances (the before slice)
    for pod in pods:
        assert str(pod.uid) in fact, ("new instance", pod)
    for pod in world.comparison.old_pods:
        assert str(pod.uid) in fact, ("old instance", pod)

    # the release page names the link and the incident; the incident page
    # links back to the release (D4, D9)
    app = env.app()
    document = s.get_json(app, s.detail_path(case.oid))
    assert str(incident_id) in s.strings(document)
    assert link["disposition"] in s.strings(document)
    assert str(incident_id) in s.get_html(app, s.detail_path(case.oid))
    incident_page = call(
        app, "GET", f"/incidents/{incident_id}", headers={**basic(), **s.HTML_ACCEPT}
    )
    assert incident_page.status == 200, incident_page.text[:500]
    assert s.detail_path(case.oid) in incident_page.text


def test_item4_more_anomaly_passes_open_no_second_incident_or_run(env: s.Env) -> None:
    case, _, _, _ = _regressed(env)
    (incident,) = env.incidents_on(case.target_id)

    for _ in range(3):
        env.anomaly_pass()

    assert env.incidents_on(case.target_id) == [incident]
    assert len(env.runs_of(incident["incident_id"])) == 1
    assert len(env.links(case.oid)) == 1


# ===========================================================================
# item 5: no automatic authorization; refusal visible; never reopened
# ===========================================================================


def _assert_not_authorized(env: s.Env, case: s.Delivered, code: str | None) -> None:
    record = env.record(case.oid)
    assert record.status == "pending", record
    assert record.calibrated_policy_revision is None
    assert env.sessions(case.oid) == []
    assert not [r for r in _authorize_rows(env, case.oid) if r["outcome"] == "applied"]
    # an Observer tick queries nothing for it
    world = s.ReleaseWorld()
    env.tick(world)
    assert env.samples(case.oid) == 0
    assert world.sample_calls == []
    if code is not None:
        app = env.app()
        assert code in s.strings(s.get_json(app, s.detail_path(case.oid)))
        assert code in s.get_html(app, s.detail_path(case.oid))


def _suspend_global(env: s.Env) -> Callable[[], None]:
    generation = env.owner.suspension_state()["global"]["generation"]
    suspended = env.owner.set_global_suspension(
        True, expected_generation=generation, actor="test:f11-step1"
    )

    def lift() -> None:
        env.owner.set_global_suspension(
            False, expected_generation=suspended, actor="test:f11-step1"
        )

    return lift


def _suspend_target(env: s.Env, case: s.Delivered) -> Callable[[], None]:
    """``case`` is on a fresh target: its suspension generation starts at 0."""
    suspended = env.owner.set_target_suspension(
        case.target_id, True, expected_generation=0, actor="test:f11-step1"
    )

    def lift() -> None:
        env.owner.set_target_suspension(
            case.target_id, False, expected_generation=suspended, actor="test:f11-step1"
        )

    return lift


@pytest.mark.parametrize("scope", ["global", "target"])
def test_item5_a_suspended_scope_refuses_and_lifting_it_does_not_authorize(
    env: s.Env, scope: str
) -> None:
    case = s.deliver(env)
    lift = _suspend_global(env) if scope == "global" else _suspend_target(env, case)
    try:
        env.authorize_pass()
        _assert_not_authorized(env, case, "SCOPE_SUSPENDED")
    finally:
        lift()

    env.authorize_pass()
    env.authorize_pass()

    _assert_not_authorized(env, case, "SCOPE_SUSPENDED")


def test_item5_a_release_without_a_catalog_entry_is_uncalibrated(env: s.Env) -> None:
    case = s.deliver(env, calibrate=False)

    env.authorize_pass()

    _assert_not_authorized(env, case, "RELEASE_POLICY_UNCALIBRATED")
    env.add_policy(s.policy_for(case.record))  # the cause goes away
    env.authorize_pass()
    _assert_not_authorized(env, case, "RELEASE_POLICY_UNCALIBRATED")


def test_item5_a_catalog_entry_that_does_not_match_is_a_mismatch(env: s.Env) -> None:
    case = s.deliver(env, calibrate=False)
    env.add_policy(
        s.policy_for(
            case.record, min_tracking_seconds=case.record.min_tracking_seconds + 60
        )
    )

    env.authorize_pass()

    _assert_not_authorized(env, case, "RELEASE_POLICY_MISMATCH")


def test_item5_an_expired_release_is_not_authorized_and_the_sweep_ends_it_unknown(
    env: s.Env,
) -> None:
    case = s.deliver(env, released_ago=s.DEADLINE - 4.0)
    env.wait_past(case.record.deadline_at)

    env.authorize_pass()

    _assert_not_authorized(env, case, None)
    env.tick(s.ReleaseWorld())  # the Observer tick sweeps expired releases
    record = env.record(case.oid)
    assert (record.status, record.ended_reason) == ("unknown", "deadline_expired")
    assert env.sessions(case.oid) == []
    document = s.get_json(env.app(), s.detail_path(case.oid))
    assert "deadline_expired" in s.strings(document)


def test_item5_a_suspension_refused_release_expires_unknown_after_lifting(
    env: s.Env,
) -> None:
    case = s.deliver(env, released_ago=s.DEADLINE - 8.0)
    lift = _suspend_target(env, case)
    try:
        env.authorize_pass()
    finally:
        lift()
    env.authorize_pass()
    _assert_not_authorized(env, case, "SCOPE_SUSPENDED")

    env.wait_past(case.record.deadline_at)
    env.tick(s.ReleaseWorld())

    record = env.record(case.oid)
    assert (record.status, record.ended_reason) == ("unknown", "deadline_expired")
    assert env.sessions(case.oid) == []


# ===========================================================================
# item 6: access separation (D12, r5)
# ===========================================================================


def test_item6_an_event_token_cannot_read_or_control_release_pages(
    env: s.Env,
) -> None:
    case = s.deliver(env)
    app = env.app()
    for token in (EVENT_TOKEN, RELEASE_TOKEN):
        for path in (s.LIST, s.detail_path(case.oid)):
            response = call(
                app, "GET", path, headers={**bearer(token), **s.JSON_ACCEPT}
            )
            assert response.status == 401, (token, path, response.status)
        response = call(
            app,
            "POST",
            s.control_path(case.oid),
            headers={
                "content-type": "application/x-www-form-urlencoded",
                **bearer(token),
                **same_origin(),
                **s.JSON_ACCEPT,
            },
            body=(
                "action=pause&expected_control_generation=0"
                f"&idempotency_key=f11s-{uuid4().hex}"
            ).encode(),
        )
        assert response.status == 401, (token, response.status)
    record = env.record(case.oid)
    assert (record.paused, record.control_generation) == (False, 0)


def test_item6_a_signed_in_user_or_another_event_actor_cannot_deliver_a_release(
    env: s.Env,
) -> None:
    app = env.app()
    uid = f"deployment/checkout-f11s-{uuid4().hex[:10]}"
    env.registry[uid] = s.registry_entry(cluster_uid=s.cluster_of(uid))
    app = env.app()
    payload = s.release_payload(
        uid, f"f11s-{uuid4().hex[:12]}", env.now() - timedelta(seconds=60)
    )
    body = json.dumps(payload).encode()

    as_user = s.intake(app, body, headers={**basic(), **same_origin()})
    as_alertmanager = s.intake(app, body, headers=bearer_for(EVENT_TOKEN))

    assert as_user.status in (401, 403), as_user.status
    assert as_alertmanager.status == 403, (ALERT_ACTOR, as_alertmanager.status)
    assert (
        env.count(
            "SELECT count(*) AS n FROM opspilot_release_observations "
            "WHERE source_release_id=%s",
            (payload["source_release_id"],),
        )
        == 0
    )
