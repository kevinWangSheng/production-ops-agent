"""Independent PostgreSQL acceptance tests for F11 PR-5b: the web process's
background pass authorizes each newly recorded release whose calibrated
policy matches exactly, once, as ``system``.

Written from the F11 PR-5 contract (``docs/tasks/2026-10-24-f11-pr5-contract.md``
r1; U-1 = A, D3, D7, categories 6-8, section 4 step 1 items 1, 2 and 5)
without reading the PR-5b implementation.

Entry points are public only: the release intake and the PR-5a pages over
HTTP, ``Workbench.release_authorize_pass``, ``ReleaseStore`` methods, the
Observer's ``poll_once`` against a fake Prometheus and its expiry sweep,
``opspilot.web.__main__._serve`` with uvicorn and the loop replaced. The
product runs under the non-owner ``opspilot_web`` / ``opspilot_observer``
roles; SQL only reads the tables earlier F11 contracts name.

Shared database hygiene (CI runs every module against one lab database,
#331): each test drains the pass before it delivers its own release, so
"one pass" means one pass for that release; every release a test delivers
is cancelled at teardown unless already terminal, and every suspension it
sets is lifted in ``finally``. The drain may record refusals on pending
releases other modules left behind; those are finished tests' rows.

Assumed names, in addition to A1/A2 of
``tests/acceptance/test_f11_pr5b_auto_authorize.py``:

* A3 (D7) the pass's audit row is ``action='authorize'``,
  ``principal_kind='system'``, ``actor='system'``, idempotency key
  ``auto-authorize:<observation_id>``.
* A4 (category 7, D7 "业务拒绝不重试") a business refusal is durable for
  every refusal the contract names, including a release for which the
  catalog has no entry: the refusal code is on the release page and a later
  pass never authorizes the release, even after the cause is gone.
* A5 the PR-5a page names (``/version-changes/{id}``, JSON for any accept
  but ``text/html``) and fixtures, imported from
  ``tests/integration/test_f11_pr5a_release_pages_postgres.py``.
"""

# ruff: noqa: F811 - pytest fixtures imported from the PR-2a / PR-5a modules
from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from opspilot.observation import ObservationStore
from opspilot.observation.releases import ReleaseRecord, ReleaseStore
from opspilot.persistence import DurableStore
from opspilot.web import Workbench
from opspilot.web import __main__ as web_main
from tests.f11_pr3_support import RELEASE_TOKEN, bearer_for, iso
from tests.f11_release_support import digest, policy_content, policy_entry
from tests.integration.test_f11_pr4b_consume_postgres import _registry_entry
from tests.integration.test_f11_pr5a_release_pages_postgres import (  # noqa: F401
    _app,
    _authorized,
    _detail_path,
    _get_html,
    _get_json,
    _poll_finds_nothing_for,
    _post_control,
    _samples,
    _sessions,
    _strings,
    bench,
)
from tests.integration.test_f11_release_observation_postgres import (  # noqa: F401
    SESSION,
    Catalog,
    _db_now,
    _rows,
    _wait_past,
    catalog,
    observer,
    observer_dsn,
    owner,
    reader,
    web,
    web_dsn,
)
from tests.m1_web_support import call
from tests.target_support import IDENTITY, WORKLOAD

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1",
    reason="explicit PG opt-in required",
)

SYSTEM = "system"
TERMINAL = {"healthy", "anomalous", "unknown", "cancelled"}


# ===========================================================================
# fixtures and helpers
# ===========================================================================


@dataclass
class Delivered:
    app: Any
    workbench: Workbench
    oid: UUID
    body: bytes
    record: ReleaseRecord
    #: the catalog entry listed for it (``calibrated`` only)
    entry: dict[str, Any]


@pytest.fixture
def delivered_ids(web: ReleaseStore) -> Iterator[list[UUID]]:
    """Every release a test delivers; cancelled at teardown unless terminal,
    so no release of this module is left observing for later modules."""
    ids: list[UUID] = []
    yield ids
    for oid in ids:
        record = web.get_release(oid)
        if record.status in TERMINAL:
            continue
        web.control_release(
            oid,
            action="cancel",
            actor="operator:pr5b-cleanup",
            expected_control_generation=record.control_generation,
            idempotency_key=f"pr5b-cleanup-{uuid4().hex}",
            calibrated_policy_revision=None,
        )


@pytest.fixture
def deliver(
    bench: Callable[..., Workbench],
    owner: DurableStore,
    web: ReleaseStore,
    delivered_ids: list[UUID],
) -> Callable[..., Delivered]:
    """Deliver one release over the HTTP intake as the release-deployer, on
    a fresh target the workbench's registry lists (201 created, pending)."""

    def make(*, released_ago: float = 300.0) -> Delivered:
        uid = f"deployment/checkout-pr5b-{uuid4().hex[:10]}"
        workbench = bench({uid: _registry_entry()})
        app = _app(workbench, owner, web)
        payload = _payload(uid, f"pr5b-{uuid4().hex[:12]}", released_ago)
        body = json.dumps(payload, sort_keys=True).encode()
        response = _intake(app, body)
        assert response.status == 201, response.text
        oid = UUID(response.json()["observation_id"])
        delivered_ids.append(oid)
        record = web.get_release(oid)
        assert (record.status, record.paused) == ("pending", False), record
        return Delivered(app, workbench, oid, body, record, {})

    return make


@pytest.fixture
def calibrated(
    bench: Callable[..., Workbench],
    deliver: Callable[..., Delivered],
    catalog: Catalog,
) -> Callable[..., Delivered]:
    """Drain the pass, then deliver a release and list its exact policy in
    the catalog: no pass has seen the release yet."""

    def make(**release: Any) -> Delivered:
        _drain(bench({}))
        case = deliver(**release)
        case.entry.update(_entry(case.record))
        catalog.add(case.entry)
        return case

    return make


def _payload(uid: str, release_id: str, released_ago: float) -> dict[str, Any]:
    return {
        "format_version": 1,
        "source": "kind-harness-pr5b",
        "source_release_id": release_id,
        "released_at": iso(_db_now() - timedelta(seconds=released_ago)),
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
            "deployment_generation": 3,
        },
    }


def _intake(app: Any, body: bytes) -> Any:
    return call(
        app,
        "POST",
        "/intake/version-changes",
        headers={"content-type": "application/json", **bearer_for(RELEASE_TOKEN)},
        body=body,
    )


def _entry(record: ReleaseRecord, **overrides: Any) -> dict[str, Any]:
    """The catalog entry that matches ``record`` exactly (D3: its target
    snapshot and HealthProfile revision; category 6: its tracking minimum)."""
    tracking = overrides.pop("min_tracking_seconds", record.min_tracking_seconds)
    return policy_entry(
        policy_content(
            record.target_snapshot,
            min_tracking_seconds=tracking,
            health_profile_revision=record.health_profile_revision,
            **overrides,
        )
    )


def _drain(workbench: Workbench) -> None:
    """Settle releases other tests left eligible, so the next pass is about
    this test's release only (they number far below the limit)."""
    for _ in range(2):
        workbench.release_authorize_pass(limit=1000)


def _audit(reader: ReleaseStore, oid: UUID) -> tuple[dict[str, Any], ...]:
    return reader.release_audit(oid)


def _authorize_audit(reader: ReleaseStore, oid: UUID) -> list[dict[str, Any]]:
    return [row for row in _audit(reader, oid) if row["action"] == "authorize"]


def _comparisons(oid: UUID) -> int:
    return int(
        _rows(
            "SELECT count(*) AS n FROM opspilot_release_comparisons "
            "WHERE observation_id=%s",
            (oid,),
        )[0]["n"]
    )


def _assert_observing_once(reader: ReleaseStore, web: ReleaseStore, oid: UUID) -> None:
    """Step 1.1: observing, exactly one authorized session, one applied
    authorize audit by ``system`` under the D7 key."""
    record = web.get_release(oid)
    assert (record.status, record.paused) == ("observing", False), record
    assert record.observation_generation == 1, record
    assert record.calibrated_policy_revision is not None
    sessions = _sessions(oid)
    assert len(sessions) == 1, sessions
    assert sessions[0]["state"] == "authorized", sessions
    rows = _authorize_audit(reader, oid)
    assert len(rows) == 1, rows
    row = rows[0]
    assert (row["outcome"], row["reason"]) == ("applied", None), row
    assert (row["principal_kind"], row["actor"]) == (SYSTEM, SYSTEM), row
    assert row["idempotency_key"] == f"auto-authorize:{oid}", row
    assert row["session_id"] == sessions[0]["session_id"], row


def _assert_not_authorized(
    case: Delivered,
    web: ReleaseStore,
    reader: ReleaseStore,
    observer: ObservationStore,
    code: str | None,
) -> None:
    """Step 1.5: still pending, no session, no Prometheus query for it, and
    (``code``) the refusal reason on the release page, HTML and JSON."""
    record = web.get_release(case.oid)
    assert record.status == "pending", record
    assert record.observation_generation == 0, record
    assert record.calibrated_policy_revision is None, record
    assert _sessions(case.oid) == []
    assert not [
        r for r in _authorize_audit(reader, case.oid) if r["outcome"] == "applied"
    ]
    _poll_finds_nothing_for(observer, case.oid)
    assert _samples(case.oid) == 0
    assert _comparisons(case.oid) == 0
    if code is not None:
        rows = _authorize_audit(reader, case.oid)
        assert [(r["outcome"], r["reason"]) for r in rows] == [("rejected", code)], rows
        assert code in _strings(_get_json(case.app, _detail_path(case.oid)))
        assert code in _get_html(case.app, _detail_path(case.oid))


def _no_change_after_more_passes(
    case: Delivered, reader: ReleaseStore, rounds: int = 2
) -> None:
    audit = _audit(reader, case.oid)
    sessions = _sessions(case.oid)
    for _ in range(rounds):
        case.workbench.release_authorize_pass()
    assert _audit(reader, case.oid) == audit
    assert _sessions(case.oid) == sessions


# ===========================================================================
# step 1.1 / 1.2: one pass authorizes once, as system
# ===========================================================================


def test_step1_1_one_pass_authorizes_a_new_matching_release_as_system(
    calibrated: Callable[..., Delivered], web: ReleaseStore, reader: ReleaseStore
) -> None:
    case = calibrated()

    case.workbench.release_authorize_pass()

    _assert_observing_once(reader, web, case.oid)
    record = web.get_release(case.oid)
    assert record.deadline_at == case.record.deadline_at
    assert record.released_at == case.record.released_at
    # the frozen policy is the one catalog entry for this release (D3)
    assert record.calibrated_policy_revision == case.entry["revision"]


def test_step1_1_a_second_pass_adds_no_audit_and_no_session(
    calibrated: Callable[..., Delivered],
    web: ReleaseStore,
    reader: ReleaseStore,
    catalog: Catalog,
    observer: ObservationStore,
) -> None:
    case = calibrated()
    case.workbench.release_authorize_pass()
    _assert_observing_once(reader, web, case.oid)

    _no_change_after_more_passes(case, reader, rounds=3)

    _assert_observing_once(reader, web, case.oid)


def test_step1_2_redelivering_the_same_payload_does_not_authorize_twice(
    calibrated: Callable[..., Delivered],
    web: ReleaseStore,
    reader: ReleaseStore,
    catalog: Catalog,
) -> None:
    case = calibrated()
    case.workbench.release_authorize_pass()
    _assert_observing_once(reader, web, case.oid)

    again = _intake(case.app, case.body)
    case.workbench.release_authorize_pass()

    assert again.status == 200, again.text
    assert again.json().get("replayed") is True or "replayed" in again.text
    assert UUID(again.json()["observation_id"]) == case.oid
    assert (
        _rows(
            "SELECT count(*) AS n FROM opspilot_release_observations "
            "WHERE source=%s AND source_release_id=%s",
            (case.record.source, case.record.source_release_id),
        )[0]["n"]
        == 1
    )
    _assert_observing_once(reader, web, case.oid)


def test_step1_2_a_redelivery_before_the_first_pass_still_authorizes_once(
    calibrated: Callable[..., Delivered],
    web: ReleaseStore,
    reader: ReleaseStore,
    catalog: Catalog,
) -> None:
    case = calibrated()
    assert _intake(case.app, case.body).status == 200

    case.workbench.release_authorize_pass()
    case.workbench.release_authorize_pass()

    _assert_observing_once(reader, web, case.oid)


def test_d7_two_concurrent_passes_converge_on_one_authorization(
    calibrated: Callable[..., Delivered],
    bench: Callable[..., Workbench],
    web: ReleaseStore,
    reader: ReleaseStore,
    catalog: Catalog,
) -> None:
    """Category 7: two web processes running the pass converge (same key)."""
    case = calibrated()
    other = bench({case.record.target_snapshot["resource_uid"]: _registry_entry()})
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def run(workbench: Workbench) -> None:
        try:
            barrier.wait(5)
            workbench.release_authorize_pass()
        except BaseException as exc:  # reported below
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(w,)) for w in (case.workbench, other)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)

    assert errors == []
    _assert_observing_once(reader, web, case.oid)


# ===========================================================================
# step 1.5: no automatic authorization; the refusal is durable and visible
# ===========================================================================


def test_step1_5_global_suspension_refuses_and_lifting_it_does_not_authorize(
    calibrated: Callable[..., Delivered],
    owner: DurableStore,
    web: ReleaseStore,
    reader: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
) -> None:
    case = calibrated()
    generation = owner.suspension_state()["global"]["generation"]
    suspended = owner.set_global_suspension(
        True, expected_generation=generation, actor="test:pr5b"
    )
    try:
        case.workbench.release_authorize_pass()

        _assert_not_authorized(case, web, reader, observer, "SCOPE_SUSPENDED")
    finally:
        lifted = owner.set_global_suspension(
            False, expected_generation=suspended, actor="test:pr5b"
        )
    assert lifted > suspended

    _no_change_after_more_passes(case, reader)

    _assert_not_authorized(case, web, reader, observer, "SCOPE_SUSPENDED")


def test_step1_5_target_suspension_refuses_and_lifting_it_does_not_authorize(
    calibrated: Callable[..., Delivered],
    owner: DurableStore,
    web: ReleaseStore,
    reader: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
) -> None:
    case = calibrated()
    suspended = owner.set_target_suspension(
        case.record.target_id, True, expected_generation=0, actor="test:pr5b"
    )
    try:
        case.workbench.release_authorize_pass()

        _assert_not_authorized(case, web, reader, observer, "SCOPE_SUSPENDED")
    finally:
        owner.set_target_suspension(
            case.record.target_id,
            False,
            expected_generation=suspended,
            actor="test:pr5b",
        )

    _no_change_after_more_passes(case, reader)

    _assert_not_authorized(case, web, reader, observer, "SCOPE_SUSPENDED")


def test_step1_5_without_a_catalog_entry_it_is_uncalibrated_and_never_retried(
    calibrated: Callable[..., Delivered],
    web: ReleaseStore,
    reader: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
) -> None:
    case = calibrated()
    catalog.empty()  # the catalog no longer lists this release (A4)

    case.workbench.release_authorize_pass()

    _assert_not_authorized(case, web, reader, observer, "RELEASE_POLICY_UNCALIBRATED")
    # the cause goes away: the refusal stands, nothing is retried
    catalog.add(_entry(case.record))
    _no_change_after_more_passes(case, reader)
    _assert_not_authorized(case, web, reader, observer, "RELEASE_POLICY_UNCALIBRATED")


def test_step1_5_a_policy_for_another_target_is_uncalibrated(
    calibrated: Callable[..., Delivered],
    web: ReleaseStore,
    reader: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
) -> None:
    case = calibrated()
    catalog.empty()
    target = dict(case.record.target_snapshot)
    target["resource_uid"] = target["resource_uid"] + "-other"
    catalog.add(
        policy_entry(
            policy_content(
                target,
                min_tracking_seconds=case.record.min_tracking_seconds,
                health_profile_revision=case.record.health_profile_revision,
            )
        )
    )

    case.workbench.release_authorize_pass()

    _assert_not_authorized(case, web, reader, observer, "RELEASE_POLICY_UNCALIBRATED")


def test_step1_5_a_policy_that_does_not_match_is_a_mismatch_never_retried(
    calibrated: Callable[..., Delivered],
    web: ReleaseStore,
    reader: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
) -> None:
    """Same target and HealthProfile revision, so the one candidate the
    pass resolves (D3); another tracking minimum, so the primitive refuses
    it as ``RELEASE_POLICY_MISMATCH``."""
    case = calibrated()
    catalog.empty()
    catalog.add(
        _entry(case.record, min_tracking_seconds=case.record.min_tracking_seconds + 60)
    )

    case.workbench.release_authorize_pass()

    _assert_not_authorized(case, web, reader, observer, "RELEASE_POLICY_MISMATCH")
    catalog.empty()
    catalog.add(_entry(case.record))
    _no_change_after_more_passes(case, reader)
    _assert_not_authorized(case, web, reader, observer, "RELEASE_POLICY_MISMATCH")


def test_step1_5_an_expired_release_is_not_authorized_and_the_sweep_ends_it_unknown(
    calibrated: Callable[..., Delivered],
    web: ReleaseStore,
    reader: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
) -> None:
    case = calibrated(released_ago=SESSION.deadline_seconds - 4.0)
    _wait_past(case.record.deadline_at)

    case.workbench.release_authorize_pass()

    # Category 6 selects only releases before their deadline; if the pass
    # does try one, the primitive refuses it for its deadline.
    _assert_not_authorized(case, web, reader, observer, None)
    for row in _authorize_audit(reader, case.oid):
        assert (row["outcome"], row["reason"]) == (
            "rejected",
            "RELEASE_DEADLINE_EXPIRED",
        ), row
    observer.sweep_expired_releases(limit=200)
    _assert_unknown_expired(case, web)


def test_step1_5_a_refused_release_expires_unknown_after_suspension_is_lifted(
    calibrated: Callable[..., Delivered],
    owner: DurableStore,
    web: ReleaseStore,
    reader: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
) -> None:
    """U-1 note: a never-authorized release refused while suspended is not
    opened when the suspension is lifted; the sweep ends it unknown."""
    case = calibrated(released_ago=SESSION.deadline_seconds - 10.0)
    suspended = owner.set_target_suspension(
        case.record.target_id, True, expected_generation=0, actor="test:pr5b"
    )
    try:
        case.workbench.release_authorize_pass()
    finally:
        owner.set_target_suspension(
            case.record.target_id,
            False,
            expected_generation=suspended,
            actor="test:pr5b",
        )
    _assert_not_authorized(case, web, reader, observer, "SCOPE_SUSPENDED")
    case.workbench.release_authorize_pass()
    _assert_not_authorized(case, web, reader, observer, "SCOPE_SUSPENDED")

    _wait_past(case.record.deadline_at)
    case.workbench.release_authorize_pass()
    observer.sweep_expired_releases(limit=200)

    _assert_unknown_expired(case, web)
    assert _sessions(case.oid) == []


def _assert_unknown_expired(case: Delivered, web: ReleaseStore) -> None:
    record = web.get_release(case.oid)
    assert (record.status, record.ended_reason) == ("unknown", "deadline_expired")
    assert _sessions(case.oid) == []
    document = _get_json(case.app, _detail_path(case.oid))
    assert "unknown" in _strings(document)
    assert "deadline_expired" in _strings(document)


def test_category6_a_release_paused_before_the_pass_is_not_authorized(
    calibrated: Callable[..., Delivered],
    web: ReleaseStore,
    reader: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
) -> None:
    case = calibrated()
    response = _post_control(case.app, case.oid, "pause", 0)
    assert response.status == 200, response.text

    case.workbench.release_authorize_pass()
    case.workbench.release_authorize_pass()

    record = web.get_release(case.oid)
    assert (record.status, record.paused) == ("pending", True), record
    assert _sessions(case.oid) == []
    assert not [
        r for r in _authorize_audit(reader, case.oid) if r["outcome"] == "applied"
    ]
    _poll_finds_nothing_for(observer, case.oid)


# ===========================================================================
# D7 / U-1: interval 0 runs no pass; a positive interval wires a working one
# ===========================================================================


class _Served:
    """Stand-ins for uvicorn and the loop inside ``_serve``."""

    def __init__(self, after_start: Callable[[Workbench], None] | None) -> None:
        self.loops: list[tuple[Any, float]] = []
        self.ready = threading.Event()
        self.after_start = after_start
        self.served = 0

    def loop(self, workbench: Any, interval: float, stop: threading.Event) -> None:
        self.loops.append((workbench, interval))
        self.ready.set()

    def run(self, app: Any, **_: Any) -> None:
        self.served += 1
        if self.after_start is not None:
            assert self.ready.wait(10), "the background thread never started"
            self.after_start(self.loops[0][0])


def _serve(
    monkeypatch: pytest.MonkeyPatch, web_dsn: str, interval: str, served: _Served
) -> None:
    import uvicorn

    monkeypatch.setenv("OPSPILOT_WEB_DSN", web_dsn)
    monkeypatch.setenv("OPSPILOT_SELF_HEAL_INTERVAL_SECONDS", interval)
    monkeypatch.setenv("OPSPILOT_BIND", "127.0.0.1:0")
    for name in (
        "OPSPILOT_TARGET_IDENTITIES",
        "OPSPILOT_HEALTH_PROFILE",
        "OPSPILOT_TOOL_PROFILE",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(web_main, "self_heal_loop", served.loop)
    monkeypatch.setattr(uvicorn, "run", served.run)
    assert web_main._serve() == 0
    assert served.served == 1


def test_d7_interval_zero_starts_no_pass_and_the_release_stays_pending(
    monkeypatch: pytest.MonkeyPatch,
    calibrated: Callable[..., Delivered],
    web_dsn: str,
    web: ReleaseStore,
    reader: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
) -> None:
    case = calibrated()
    served = _Served(after_start=None)

    _serve(monkeypatch, web_dsn, "0", served)

    assert served.loops == []
    _assert_not_authorized(case, web, reader, observer, None)
    assert _authorize_audit(reader, case.oid) == []


def test_d7_a_positive_interval_hands_the_loop_a_workbench_whose_pass_authorizes(
    monkeypatch: pytest.MonkeyPatch,
    calibrated: Callable[..., Delivered],
    web_dsn: str,
    web: ReleaseStore,
    reader: ReleaseStore,
    catalog: Catalog,
) -> None:
    """Positive control for the interval-0 test: the workbench ``_serve``
    builds reaches the release store and authorizes on its own."""
    case = calibrated()
    served = _Served(after_start=lambda workbench: workbench.release_authorize_pass())

    _serve(monkeypatch, web_dsn, "30", served)

    assert [interval for _, interval in served.loops] == [30.0]
    _assert_observing_once(reader, web, case.oid)
