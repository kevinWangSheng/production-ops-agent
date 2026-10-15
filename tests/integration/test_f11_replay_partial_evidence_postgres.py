"""Replay partial samples collected through the real non-owner PG path."""

import os

import psycopg
import pytest

from opspilot.observer.release_replay import replay_release_history
from opspilot.observer.release_sampling import take_release_sample
from scripts.m0.postgres_lab import DSN
from tests.integration.test_f11_pr2b_impl_postgres import (
    Telemetry,
    authorized_due,
    shared_claim,
)
from tests.integration.test_f11_pr2b_impl_postgres import (
    release_harness as pr2b_harness,
)

release_harness = pr2b_harness

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)


def cancel_release(web, oid):
    receipt = web.control_release(
        oid,
        action="cancel",
        actor="tester",
        expected_control_generation=web.get_release(oid).control_generation,
        idempotency_key="cancel",
        calibrated_policy_revision=None,
    )
    assert receipt.outcome == "applied"


@pytest.mark.parametrize("allowed", [0, 5, 35])
def test_cancelled_partial_sample_replays(release_harness, allowed):
    web, observer, oid, _, _ = release_harness
    auth = authorized_due(release_harness)
    lease = shared_claim(observer, auth.session_id)

    def cancel(n):
        if n == allowed:
            cancel_release(web, oid)

    if allowed == 0:
        cancel(0)
    source = Telemetry(observer.release_context(lease.session_id), on_call=cancel)
    result = take_release_sample(lease, source, observer)
    assert result.requests_issued == allowed and result.scope_interrupted
    assert not result.receipt.accepted
    assert result.receipt.disposition == "history_only"
    assert result.receipt.reason == "control_generation_stale"
    assert web.get_release(oid).status == "cancelled"
    assert web.get_release(oid).used_sample_count == 0
    history = observer.release_history(oid)
    assert len(history["samples"]) == 1
    assert len(history["samples"][0]["queries"]) == allowed
    replay = replay_release_history(history)
    assert replay.consistent, replay.integrity
    assert replay.used_sample_count == 0
    assert replay.replayed_status == "cancelled"
    assert replay.external_queries == () and replay.model_requests == ()


@pytest.mark.parametrize("allowed", [5, 35])
def test_scope_interrupted_partial_sample_replays(release_harness, allowed):
    web, observer, oid, _, _ = release_harness
    auth = authorized_due(release_harness)
    lease = shared_claim(observer, auth.session_id)
    source = Telemetry(observer.release_context(lease.session_id))
    result = take_release_sample(
        lease, source, observer, scope_current=lambda _: len(source.calls) < allowed
    )
    assert result.requests_issued == allowed and result.scope_interrupted
    assert not result.receipt.accepted
    assert result.receipt.disposition == "history_only"
    assert result.receipt.reason == "readings_inconsistent"
    assert web.get_release(oid).used_sample_count == 0
    history = observer.release_history(oid)
    assert len(history["samples"]) == 1
    assert len(history["samples"][0]["queries"]) == allowed
    replay = replay_release_history(history)
    assert replay.consistent, replay.integrity
    assert replay.used_sample_count == 0 and replay.replayed_status == "observing"


def test_adopted_sample_with_deleted_query_is_rejected(release_harness):
    _, observer, oid, _, _ = release_harness
    auth = authorized_due(release_harness)
    lease = shared_claim(observer, auth.session_id)
    source = Telemetry(observer.release_context(lease.session_id))
    result = take_release_sample(lease, source, observer)
    assert result.receipt.accepted and result.receipt.disposition == "adopted"
    assert replay_release_history(observer.release_history(oid)).consistent
    # Test-only owner corruption; product roles cannot rewrite query evidence.
    with psycopg.connect(DSN) as conn:
        conn.execute("SET LOCAL session_replication_role=replica")
        deleted = conn.execute(
            "DELETE FROM opspilot_release_query_results "
            "WHERE sample_id=%s AND query_key=%s",
            (result.receipt.sample_id, "context:error_ratio:query"),
        )
        assert deleted.rowcount == 1
    replay = replay_release_history(observer.release_history(oid))
    assert not replay.consistent
    assert "SAMPLE_DECISION_MISMATCH" in replay.integrity


@pytest.mark.parametrize("cancel_after", [None, 5])
def test_existing_query_text_tampering_is_rejected(release_harness, cancel_after):
    web, observer, oid, _, _ = release_harness
    auth = authorized_due(release_harness)
    lease = shared_claim(observer, auth.session_id)

    def cancel(n):
        if n == cancel_after:
            cancel_release(web, oid)

    source = Telemetry(observer.release_context(lease.session_id), on_call=cancel)
    result = take_release_sample(lease, source, observer)
    assert result.receipt.accepted == (cancel_after is None)
    assert replay_release_history(observer.release_history(oid)).consistent
    with psycopg.connect(DSN) as conn:
        conn.execute("SET LOCAL session_replication_role=replica")
        changed = conn.execute(
            "UPDATE opspilot_release_query_results SET query='vector(999)' "
            "WHERE sample_id=%s AND query_key=%s",
            (result.receipt.sample_id, "mapping:pod_owner:query"),
        )
        assert changed.rowcount == 1
    replay = replay_release_history(observer.release_history(oid))
    assert not replay.consistent
    assert "QUERY_MISMATCH" in replay.integrity
