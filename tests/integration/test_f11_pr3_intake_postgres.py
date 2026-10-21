"""Independent acceptance tests for F11 PR-3 on PostgreSQL: release intake.

Written from ``docs/tasks/2026-10-21-f11-pr3-contract.md`` without reading the
implementation. Behaviour is driven through ``POST /intake/releases`` on an
app whose release store is a ``ReleaseStore`` connected as the real web role;
SQL only reads the tables the contract names and builds race fixtures.

Behaviours covered: 1, 2, 3 and 4 (database side), 5 and 6 (nothing stored),
7 (late delivery), 8 (replay), 9 (conflict), 10 (storage failure and retry),
11 (empty policy list) and 15 (legacy intake). Ambiguities A0-A3 are listed in
``tests/test_f11_pr3_acceptance.py``; additional ones are marked ``A5..``.

* A5  HTTP status of a late first delivery: the store's receipt says
  ``created``, so 201 with ``status == "unknown"`` is demanded.
* A6  a rejected delivery (conflict, future time) also leaves an audit row;
  asserted because ``ReleaseStore`` already commits one for business refusals.
* A7  parallel identical deliveries: exactly one 201, the rest 200 replayed.
* A8  ``min_tracking_seconds`` of the registered release is 1500 (lead's
  decision recorded in the PR-3 section of the task record).
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from opspilot.observation.release_policy import load_release_policy_catalog
from opspilot.observation.releases import ReleaseStore
from opspilot.persistence import DurableStore, PoolConfig
from opspilot.persistence.grants import WEB_ROLE
from opspilot.tools.fixture import fixture_versions
from opspilot.web import (
    DurableClock,
    DurableEventLog,
    DurableEvidenceStore,
    DurableIncidentStore,
    DurableWebLedger,
)
from scripts.m0.postgres_lab import DSN
from tests.f11_pr3_support import (
    ALERT_TOKEN,
    MIN_TRACKING_SECONDS,
    PROFILE,
    RELEASE_ACTOR,
    RELEASE_TOKEN,
    TARGET,
    assert_route_exists,
    bearer_for,
    build_app,
    call,
    error_code,
    iso,
    post_release,
    raw,
    release_payload,
    utc_now,
)
from tests.runtime_roles_support import login

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1",
    reason="explicit PG opt-in required",
)

POOL = PoolConfig(min_size=1, max_size=8, timeout=5.0)
OBSERVATIONS = "opspilot_release_observations"
EVENTS = "opspilot_release_events"
# Tables whose growth would mean an incident, Run, session or query was made.
SIDE_EFFECT_TABLES = (
    "opspilot_incidents",
    "opspilot_runs",
    "opspilot_observation_sessions",
    "opspilot_observation_samples",
    "opspilot_release_query_results",
)


# --- fixtures and helpers --------------------------------------------------


@pytest.fixture(scope="module")
def owner() -> Iterator[DurableStore]:
    store = DurableStore(DSN, pool=POOL)
    store.install()
    yield store
    store.close()


@pytest.fixture(scope="module")
def web_dsn() -> Iterator[str]:
    with login(DSN, WEB_ROLE) as dsn:
        yield dsn


@pytest.fixture(scope="module")
def web(web_dsn: str) -> ReleaseStore:
    return ReleaseStore(web_dsn, pool=POOL)


class Env:
    """One app, one registry, one unique target and release source."""

    def __init__(self, owner: DurableStore, web: ReleaseStore, releases: Any = None):
        suffix = uuid4().hex[:10]
        self.uid = f"deployment/checkout-pr3-{suffix}"
        self.source = f"kind-harness-{suffix}"
        self.target = {**TARGET, "resource_uid": self.uid}
        entry = {
            "integration_id": TARGET["integration_id"],
            "cluster_uid": TARGET["cluster_uid"],
            "namespace": TARGET["namespace"],
            "workload": TARGET["workload"],
            "health_profile_id": PROFILE.profile_id,
        }
        self.registry = {
            self.uid: entry,
            # Registered, but its workload is not the one the profile observes.
            f"deployment/payment-pr3-{suffix}": {**entry, "workload": "payment"},
        }
        self.payment_uid = f"deployment/payment-pr3-{suffix}"
        self.owner = owner
        self.web = web
        self.app, self.workbench = build_app(
            web if releases is None else releases,
            registry=self.registry,
            workbench_kwargs={
                "incidents": DurableIncidentStore(owner),
                "events": DurableEventLog(owner),
                "evidence": DurableEvidenceStore(owner),
                "ledger": DurableWebLedger(owner),
                "run_versions": fixture_versions(),
            },
            clock=DurableClock(owner),
        )

    def payload(
        self, release_id: str | None = None, **overrides: Any
    ) -> dict[str, Any]:
        return release_payload(
            source=self.source,
            release_id=release_id or f"rel-{uuid4().hex[:12]}",
            target=self.target,
            **overrides,
        )

    def post(self, payload: dict[str, Any], **kwargs: Any):
        response = post_release(self.app, payload, **kwargs)
        assert_route_exists(response)
        return response


@pytest.fixture
def env(owner: DurableStore, web: ReleaseStore) -> Env:
    return Env(owner, web)


def _rows(statement: str, params: tuple = ()) -> list[dict[str, Any]]:
    with psycopg.connect(DSN, row_factory=dict_row) as conn:
        return conn.execute(statement, params).fetchall()


def _db_now() -> datetime:
    return _rows("SELECT clock_timestamp() AS now")[0]["now"]


def _observations(env: Env, release_id: str) -> list[dict[str, Any]]:
    return _rows(
        f"SELECT * FROM {OBSERVATIONS} WHERE source=%s AND source_release_id=%s",
        (env.source, release_id),
    )


def _events(env: Env, release_id: str) -> list[dict[str, Any]]:
    return _rows(
        f"SELECT * FROM {EVENTS} WHERE source=%s AND source_release_id=%s "
        "ORDER BY event_id",
        (env.source, release_id),
    )


def _counts() -> dict[str, int]:
    return {
        table: _rows(f"SELECT count(*) AS n FROM {table}")[0]["n"]
        for table in SIDE_EFFECT_TABLES
    }


def _sha_matches(stored: str, body: bytes) -> bool:
    hexdigest = hashlib.sha256(body).hexdigest()
    return stored in (hexdigest, f"sha256:{hexdigest}")


def _post_raw(env: Env, payload: dict[str, Any]):
    body = raw(payload)
    return body, env.post(payload, body=body)


def _frozen(row: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "observation_id",
        "source",
        "source_release_id",
        "target_id",
        "target_snapshot",
        "before_revision",
        "after_revision",
        "auxiliary",
        "semantic_sha256",
        "health_profile_revision",
        "released_at",
        "received_at",
        "deadline_at",
        "deadline_seconds",
        "min_tracking_seconds",
        "status",
        "ended_reason",
        "ended_at",
        "control_generation",
        "observation_generation",
    )
    return {key: row[key] for key in keys}


# --- 1. a valid delivery by the dedicated actor ----------------------------


def test_valid_release_is_registered_pending_with_one_audit_row(env: Env) -> None:
    release_id = f"rel-{uuid4().hex[:12]}"
    released_at = utc_now() - timedelta(minutes=5)
    payload = env.payload(release_id, released_at=released_at)
    payload["released_at"] = iso(released_at)
    before = _counts()
    body, response = _post_raw(env, payload)

    assert response.status == 201, response.text
    answer = response.json()
    assert answer["outcome"] == "created"
    assert answer["status"] == "pending"
    observation_id = UUID(answer["observation_id"])

    [row] = _observations(env, release_id)
    assert row["observation_id"] == observation_id
    assert row["status"] == "pending"
    assert row["ended_reason"] is None
    assert row["supersedes_observation_id"] is None
    assert row["released_at"] == datetime.fromisoformat(payload["released_at"])
    assert row["received_at"] >= row["released_at"]
    assert row["deadline_at"] == row["released_at"] + timedelta(
        seconds=PROFILE.session.deadline_seconds
    )
    assert row["min_tracking_seconds"] == MIN_TRACKING_SECONDS  # A8
    assert row["before_revision"] == payload["before"]
    assert row["after_revision"] == payload["after"]
    assert row["auxiliary"]["deployment_generation"] == 12
    assert row["target_snapshot"]["resource_uid"] == env.uid
    assert row["target_snapshot"]["workload"] == TARGET["workload"]
    assert row["incident_id"] is None

    [event] = _events(env, release_id)
    assert event["observation_id"] == observation_id
    assert event["outcome"] == "created"
    assert event["sequence"] == 1
    assert event["actor"] == RELEASE_ACTOR
    assert _sha_matches(event["raw_sha256"], body)
    assert event["semantic_sha256"] == row["semantic_sha256"]
    assert event["truncated"] is False

    # Registration only: no incident, Run, session, sample or query.
    assert _counts() == before


def test_same_release_id_from_another_source_is_another_subject(env: Env) -> None:
    release_id = f"rel-{uuid4().hex[:12]}"
    first = env.post(env.payload(release_id))
    other_source = release_payload(
        source=env.source + "-b", release_id=release_id, target=env.target
    )
    second = post_release(env.app, other_source)
    assert (first.status, second.status) == (201, 201)
    assert first.json()["observation_id"] != second.json()["observation_id"]


def test_equivalent_utc_offset_is_the_same_instant(env: Env) -> None:
    release_id = f"rel-{uuid4().hex[:12]}"
    released_at = (utc_now() - timedelta(minutes=10)).replace(microsecond=0)
    zulu = env.payload(release_id)
    zulu["released_at"] = iso(released_at)
    offset = dict(zulu)
    offset["released_at"] = released_at.astimezone(
        timezone(timedelta(hours=2))
    ).isoformat()
    assert env.post(zulu).status == 201
    replay = env.post(offset)
    assert replay.status == 200, replay.text
    assert replay.json()["outcome"] == "replayed"
    [row] = _observations(env, release_id)
    assert row["released_at"] == released_at


# --- 2. wrong actor leaves nothing behind ----------------------------------


@pytest.mark.parametrize("token", [ALERT_TOKEN], ids=["alertmanager"])
def test_other_actor_creates_no_release_incident_or_audit(env: Env, token: str) -> None:
    release_id = f"rel-{uuid4().hex[:12]}"
    before = _counts()
    response = env.post(env.payload(release_id), token=token)
    assert response.status == 403
    assert error_code(response) == "RELEASE_ACTOR_REQUIRED"
    assert _observations(env, release_id) == []
    assert _events(env, release_id) == []
    assert _counts() == before


def test_unauthenticated_delivery_leaves_nothing_behind(env: Env) -> None:
    release_id = f"rel-{uuid4().hex[:12]}"
    response = env.post(env.payload(release_id), token=None)
    assert response.status == 401
    assert _observations(env, release_id) == []
    assert _events(env, release_id) == []


# --- 3 and 4. invalid payloads and unknown fields --------------------------


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.pop("source_release_id") and None,
        lambda p: p.pop("released_at") and None,
        lambda p: p.update(released_at="2026-10-21T12:00:00"),
        lambda p: p["after"].update(images={"checkout": "sha256:nothex"}),
        lambda p: p["after"].update(surprise=1),
    ],
    ids=["no-id", "no-time", "naive-time", "bad-digest", "unknown-business-field"],
)
def test_invalid_payload_writes_no_subject_and_no_side_effects(
    env: Env, mutate
) -> None:
    release_id = f"rel-{uuid4().hex[:12]}"
    payload = env.payload(release_id)
    mutate(payload)
    before = _counts()
    response = env.post(payload)
    assert response.status == 400
    assert error_code(response) == "INVALID_RELEASE_PAYLOAD"
    assert _observations(env, release_id) == []
    assert _events(env, release_id) == []
    assert _counts() == before


def test_unknown_top_level_field_is_audit_only_and_redacted(env: Env) -> None:
    """A1 reading: an unknown envelope field is accepted, appears (redacted
    and bounded) only in the audit text, and never in the semantic hash,
    target, versions or any query scope."""
    release_id = f"rel-{uuid4().hex[:12]}"
    secret = "Zs3cretValue9876543210abcdef"
    plain = env.payload(release_id)
    noisy = {**plain, "note": f"Authorization: Bearer {secret}", "trace": "t-1"}

    first = env.post(noisy)
    assert first.status == 201, first.text
    [row] = _observations(env, release_id)
    [event] = _events(env, release_id)
    assert "note" in event["audit_text"]
    assert secret not in event["audit_text"]
    stored = json.dumps(
        [
            row["target_snapshot"],
            row["before_revision"],
            row["after_revision"],
            row["auxiliary"],
        ],
        default=str,
    )
    assert "trace" not in stored and "note" not in stored

    # The semantic fingerprint ignores unknown fields: the same release with
    # different or no extra fields is a replay, not a conflict.
    again = env.post({**plain, "note": "something else entirely"})
    assert again.status == 200, again.text
    assert again.json()["outcome"] == "replayed"
    bare = env.post(plain)
    assert bare.status == 200
    events = _events(env, release_id)
    assert {e["semantic_sha256"] for e in events} == {row["semantic_sha256"]}
    assert len({e["raw_sha256"] for e in events}) == 3


def test_audit_text_is_bounded_to_16_kib(env: Env) -> None:
    release_id = f"rel-{uuid4().hex[:12]}"
    payload = {**env.payload(release_id), "padding": "x" * (100 * 1024)}
    response = env.post(payload)
    assert response.status == 201, response.text  # 100 KiB < the 128 KiB cap
    [event] = _events(env, release_id)
    assert len(event["audit_text"].encode()) <= 16 * 1024
    assert event["truncated"] is True


# --- 5. target resolution failures store nothing ---------------------------


def test_unresolved_target_stores_nothing(env: Env) -> None:
    release_id = f"rel-{uuid4().hex[:12]}"
    payload = env.payload(release_id)
    payload["target"]["resource_uid"] = f"deployment/unknown-{uuid4().hex[:8]}"
    before = _counts()
    response = env.post(payload)
    assert (response.status, error_code(response)) == (422, "TARGET_UNRESOLVED")
    assert _observations(env, release_id) == []
    assert _counts() == before


def test_mismatching_target_stores_nothing(env: Env) -> None:
    release_id = f"rel-{uuid4().hex[:12]}"
    payload = env.payload(release_id)
    payload["target"]["namespace"] = "elsewhere"
    before = _counts()
    response = env.post(payload)
    assert (response.status, error_code(response)) == (422, "TARGET_MISMATCH")
    assert _observations(env, release_id) == []
    assert _counts() == before


def test_target_whose_workload_the_profile_does_not_observe_is_422(env: Env) -> None:
    release_id = f"rel-{uuid4().hex[:12]}"
    payload = env.payload(release_id)
    payload["target"] = {
        **env.target,
        "resource_uid": env.payment_uid,
        "workload": "payment",
    }
    response = env.post(payload)
    assert (response.status, error_code(response)) == (
        422,
        "HEALTH_PROFILE_TARGET_MISMATCH",
    )
    assert _observations(env, release_id) == []


# --- 6. a release from the future ------------------------------------------


@pytest.mark.parametrize("ahead", [timedelta(hours=1), timedelta(days=1)])
def test_released_at_after_database_time_is_422(env: Env, ahead: timedelta) -> None:
    release_id = f"rel-{uuid4().hex[:12]}"
    payload = env.payload(release_id)
    payload["released_at"] = iso(_db_now() + ahead)
    response = env.post(payload)
    assert (response.status, error_code(response)) == (422, "RELEASE_TIME_IN_FUTURE")
    assert _observations(env, release_id) == []
    # A6: the business refusal keeps one audit row without a subject.
    [event] = _events(env, release_id)
    assert event["outcome"] == "rejected"
    assert event["reason"] == "RELEASE_TIME_IN_FUTURE"
    assert event["observation_id"] is None
    assert event["actor"] == RELEASE_ACTOR


# --- 7. late delivery -------------------------------------------------------


def test_first_delivery_after_the_deadline_is_recorded_unknown_late(env: Env) -> None:
    release_id = f"rel-{uuid4().hex[:12]}"
    payload = env.payload(release_id)
    payload["released_at"] = iso(
        utc_now() - timedelta(seconds=PROFILE.session.deadline_seconds + 600)
    )
    before = _counts()
    response = env.post(payload)
    assert response.status == 201, response.text  # A5
    answer = response.json()
    assert (answer["outcome"], answer["status"]) == ("created", "unknown")
    [row] = _observations(env, release_id)
    assert row["status"] == "unknown"
    assert row["ended_reason"] == "late_delivery"
    assert row["ended_at"] is not None
    [event] = _events(env, release_id)
    assert event["outcome"] == "created"
    assert event["reason"] == "late_delivery"
    # No session, sample, query, incident or Run: nothing was observed.
    assert _counts() == before
    assert (
        _rows(
            "SELECT count(*) AS n FROM opspilot_observation_sessions "
            "WHERE release_observation_id=%s",
            (row["observation_id"],),
        )[0]["n"]
        == 0
    )

    # Replaying a late release neither reopens it nor resets its times.
    replay = env.post(payload)
    assert replay.status == 200
    assert replay.json()["outcome"] == "replayed"
    assert replay.json()["status"] == "unknown"
    [after] = _observations(env, release_id)
    assert _frozen(after) == _frozen(row)


# --- 8. replay ---------------------------------------------------------------


def test_semantic_replay_changes_nothing_but_the_audit_sequence(env: Env) -> None:
    release_id = f"rel-{uuid4().hex[:12]}"
    payload = env.payload(release_id)
    first = env.post(payload)
    assert first.status == 201
    [frozen] = _observations(env, release_id)

    # Same meaning, different bytes: key order and whitespace.
    reordered = json.dumps(payload, indent=2, sort_keys=False).encode()
    second = env.post(payload, body=reordered)
    assert second.status == 200, second.text
    answer = second.json()
    assert answer["outcome"] == "replayed"
    assert answer["observation_id"] == first.json()["observation_id"]
    assert answer["status"] == "pending"

    [after] = _observations(env, release_id)
    assert _frozen(after) == _frozen(frozen)
    events = _events(env, release_id)
    assert [e["sequence"] for e in events] == [1, 2]
    assert [e["outcome"] for e in events] == ["created", "replayed"]
    assert events[0]["semantic_sha256"] == events[1]["semantic_sha256"]
    assert events[0]["raw_sha256"] != events[1]["raw_sha256"]
    assert _sha_matches(events[1]["raw_sha256"], reordered)


def test_concurrent_identical_deliveries_create_exactly_one_subject(env: Env) -> None:
    """A7."""
    release_id = f"rel-{uuid4().hex[:12]}"
    payload = env.payload(release_id)
    results: list[Any] = []
    barrier = threading.Barrier(6)

    def deliver() -> None:
        barrier.wait()
        results.append(post_release(env.app, payload))

    threads = [threading.Thread(target=deliver) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert len(results) == 6
    statuses = [r.status for r in results]
    # A7: a transient 503 (retryable, contract section 7) is allowed under a
    # true race; a second 201, a 409 or any other status is not.
    assert set(statuses) <= {200, 201, 503}, [(r.status, r.text) for r in results]
    assert statuses.count(201) <= 1
    for r in results:
        if r.status == 503:
            assert error_code(r) == "STORAGE_UNAVAILABLE"
    # Retrying what failed converges: one subject, every call answered.
    for r in [r for r in results if r.status == 503]:
        retry = post_release(env.app, payload)
        assert retry.status in {200, 201}, retry.text
        statuses.append(retry.status)
    assert statuses.count(201) == 1, "exactly one delivery creates the subject"
    assert len(_observations(env, release_id)) == 1
    events = _events(env, release_id)
    sequences = sorted(e["sequence"] for e in events)
    assert sequences == list(range(1, len(events) + 1))
    assert [e["outcome"] for e in events].count("created") == 1
    assert {e["outcome"] for e in events} <= {"created", "replayed"}


# --- 9. conflicts ------------------------------------------------------------


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p, env: p["after"].update(pod_template_digest="sha256:" + "1" * 64),
        lambda p, env: p["before"].update(images={"checkout": "sha256:" + "2" * 64}),
        lambda p, env: p.update(
            released_at=iso(
                datetime.fromisoformat(p["released_at"]) - timedelta(minutes=1)
            )
        ),
        lambda p, env: p["auxiliary"].update(deployment_generation=99),
        lambda p, env: p.update(
            target={
                **env.target,
                "resource_uid": env.payment_uid,
                "workload": "payment",
            }
        ),
    ],
    ids=["after-template", "before-image", "released-at", "auxiliary", "target"],
)
def test_same_key_different_content_is_409_and_keeps_the_original(
    env: Env, mutate
) -> None:
    release_id = f"rel-{uuid4().hex[:12]}"
    payload = env.payload(release_id)
    assert env.post(payload).status == 201
    [original] = _observations(env, release_id)

    changed = json.loads(json.dumps(payload))
    mutate(changed, env)
    response = env.post(changed)
    assert response.status == 409, response.text
    assert error_code(response) == "RELEASE_IDENTITY_CONFLICT"

    [after] = _observations(env, release_id)
    assert _frozen(after) == _frozen(original)
    # A6: the refusal is audited against the existing subject.
    events = _events(env, release_id)
    assert events[-1]["outcome"] == "rejected"
    assert events[-1]["reason"] == "RELEASE_IDENTITY_CONFLICT"
    assert events[-1]["observation_id"] == original["observation_id"]


# --- 10. storage failure and safe retry --------------------------------------


def test_unreachable_database_is_503_and_stores_no_half_subject(
    owner: DurableStore, web: ReleaseStore
) -> None:
    broken = ReleaseStore(
        "host=127.0.0.1 port=1 dbname=none user=none connect_timeout=2",
        pool=PoolConfig(min_size=0, max_size=1, timeout=2.0),
    )
    env = Env(owner, web, releases=broken)
    release_id = f"rel-{uuid4().hex[:12]}"
    response = env.post(env.payload(release_id))
    assert response.status == 503, response.text
    assert error_code(response) == "STORAGE_UNAVAILABLE"
    assert _observations(env, release_id) == []
    assert _events(env, release_id) == []


def test_lock_timeout_is_503_then_retry_is_a_clean_replay(env: Env) -> None:
    """Hold the per-subject audit lock the store takes for sequencing; the
    request must give up with 503 (store lock timeout 4 s), leave no partial
    audit row, and a retry after the lock is gone must succeed as a replay."""
    release_id = f"rel-{uuid4().hex[:12]}"
    payload = env.payload(release_id)
    created = env.post(payload)
    assert created.status == 201
    oid = created.json()["observation_id"]

    holder = psycopg.connect(DSN, autocommit=False)
    outcome: list[Any] = []
    try:
        holder.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"release-audit:{oid}",),
        )
        thread = threading.Thread(
            target=lambda: outcome.append(post_release(env.app, payload))
        )
        started = time.monotonic()
        thread.start()
        thread.join(30)
        blocked_for = time.monotonic() - started
        stuck = thread.is_alive()
    finally:
        holder.rollback()
        holder.close()
    if stuck:
        thread.join(30)
        pytest.fail("delivery blocked on the lock instead of timing out with 503")
    [response] = outcome
    assert response.status == 503, (response.status, response.text)
    assert error_code(response) == "STORAGE_UNAVAILABLE"
    assert blocked_for < 20
    assert [e["sequence"] for e in _events(env, release_id)] == [1]  # nothing partial

    retry = env.post(payload)
    assert retry.status == 200, retry.text
    assert retry.json()["outcome"] == "replayed"
    assert [e["sequence"] for e in _events(env, release_id)] == [1, 2]
    assert len(_observations(env, release_id)) == 1


# --- 11. registration never authorizes; empty policy list is explicit --------


def test_authorizing_a_registered_release_with_no_calibrated_policy_is_refused(
    env: Env, web: ReleaseStore
) -> None:
    if load_release_policy_catalog().policies:
        pytest.skip("the shipped calibration catalog is no longer empty")
    release_id = f"rel-{uuid4().hex[:12]}"
    created = env.post(env.payload(release_id))
    assert created.status == 201
    oid = UUID(created.json()["observation_id"])
    before = _counts()

    receipt = web.authorize_release(
        oid,
        actor="controller:test",
        expected_control_generation=0,
        idempotency_key=f"pr3-{uuid4().hex}",
        calibrated_policy_revision="checkout-release-none@000000000000",
    )
    assert receipt.outcome == "rejected"
    assert receipt.reason == "RELEASE_POLICY_UNCALIBRATED"
    assert receipt.session_id is None
    assert receipt.status == "pending"

    [row] = _observations(env, release_id)
    assert row["status"] == "pending"
    assert row["ended_reason"] is None
    assert _counts() == before  # no session, sample, query, incident or Run


# --- 15. legacy intake is unchanged -----------------------------------------


def test_alertmanager_intake_still_works_and_creates_no_release(env: Env) -> None:
    webhook = {
        "receiver": "opspilot",
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "Unmatched",
                    "service": f"nobody-{uuid4().hex[:6]}",
                },
                "annotations": {},
                "startsAt": "2026-10-10T10:41:54.365Z",
                "endsAt": "0001-01-01T00:00:00Z",
                "fingerprint": uuid4().hex[:16],
            }
        ],
        "version": "4",
    }
    releases_before = _rows(f"SELECT count(*) AS n FROM {OBSERVATIONS}")[0]["n"]
    response = call(
        env.app,
        "POST",
        "/intake/alertmanager",
        headers={"content-type": "application/json", **bearer_for(ALERT_TOKEN)},
        body=json.dumps(webhook).encode(),
    )
    assert response.status == 200, response.text
    assert all(r["outcome"] != "failed" for r in response.json()["results"])
    assert _rows(f"SELECT count(*) AS n FROM {OBSERVATIONS}")[0]["n"] == releases_before


def test_release_token_cannot_use_the_alertmanager_webhook_for_releases(
    env: Env,
) -> None:
    """The release route is the only way in: a release payload sent to the
    Alertmanager route is not a release."""
    release_id = f"rel-{uuid4().hex[:12]}"
    response = call(
        env.app,
        "POST",
        "/intake/alertmanager",
        headers={"content-type": "application/json", **bearer_for(RELEASE_TOKEN)},
        body=raw(env.payload(release_id)),
    )
    assert response.status != 201
    assert _observations(env, release_id) == []


# --- 12-14 need kind: see the end of tests/test_f11_pr3_acceptance.py ---------
