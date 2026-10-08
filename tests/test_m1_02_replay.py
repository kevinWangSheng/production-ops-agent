"""Offline replay recomputes the recovery verdict from stored samples alone
(M1-02 step 5, #87; F6 step 5).

Input is what ``ObservationStore.session_history`` returns; nothing here
touches a database, telemetry or a model. The replay rebuilds every reading
from its raw bundle, re-judges it against the frozen profile content stored
behind the session's revision, folds the session (streak, sustained window,
budget) from the recomputed outcomes, and compares all of it with what the
rows say. Tampered bundles, tampered verdicts or an unreadable profile are
reported as an integrity mismatch with an ``unknown`` verdict -- the stored
verdict is never repeated.
"""

from __future__ import annotations

import hashlib
import io
import json
from datetime import timedelta
from uuid import uuid4

import pytest

from opspilot.observer.replay import (
    INTEGRITY_MISMATCH,
    SessionReplay,
    main,
    replay,
    replay_history,
    summary,
)
from tests.m1_02_replay_support import (
    NOW,
    PROFILE,
    healthy_history,
    rehash,
    stored_history,
    take,
    with_sample_time,
)


def test_a_healthy_session_replays_consistently_and_confirms_recovery():
    history = healthy_history()
    result = replay_history(history)
    assert isinstance(result, SessionReplay)
    assert result.consistent and result.integrity == ()
    assert result.recovery_verdict == "healthy"
    assert result.recovery_confirmed is True
    assert result.healthy_window_seconds == 600
    assert result.expected_lifecycle == "resolved"
    assert result.recorded_lifecycle == "resolved"
    assert result.replayed_session_state == result.stored_session_state == "completed"
    assert len(result.samples) == len(history["samples"]) == 6
    last = result.samples[-1]
    assert last.decision.replayed[4] == "recovery_confirmed"
    assert (last.replayed_outcome, last.replayed_required_signals_present) == (
        "healthy",
        True,
    )
    # the basis of every reading is exposed: query, window, source, hash,
    # stored vs replayed status/value/count, per-signal verdict
    reading = next(r for r in last.readings if r.signal_name == "errors")
    assert (
        reading.query
        == "e{namespace='ns',service='svc'} / t{namespace='ns',service='svc'}"
    )
    assert reading.stored == reading.replayed == ("ok", 0.0, 5)
    assert reading.raw_verified is True and len(reading.raw_sha256) == 64
    assert reading.verdict == "healthy"
    assert result.external_queries == () and result.model_requests == ()


def test_a_tampered_stored_verdict_is_an_integrity_mismatch_not_repeated():
    """F6 step 5: only the judgment columns are altered, the bundles stay."""
    history = healthy_history()
    for row in history["samples"]:
        row["outcome"] = "degraded"
        row["confirms_health"] = False
        row["health_basis"] = "outcome_not_healthy"
    result = replay_history(history)
    assert not result.consistent
    assert INTEGRITY_MISMATCH in result.integrity
    assert result.recovery_verdict == "unknown"
    assert result.recovery_confirmed is False
    assert result.expected_lifecycle is None
    # what the bundles actually say is still reported, apart from the claim
    assert result.recomputed_verdict == "healthy"
    first = result.samples[0]
    assert (first.stored_outcome, first.replayed_outcome) == ("degraded", "healthy")
    assert "OUTCOME_MISMATCH" in first.integrity


def test_a_tampered_bundle_fails_that_sample_and_the_session():
    history = healthy_history()
    target = history["samples"][2]["readings"][0]
    target["raw"] = bytes(target["raw"]) + b" "
    result = replay_history(history)
    assert not result.consistent
    assert INTEGRITY_MISMATCH in result.integrity
    assert result.recovery_verdict == "unknown"
    sample = result.samples[2]
    assert any(code.startswith("RAW_HASH_MISMATCH:") for code in sample.integrity)
    reading = sample.readings[0]
    assert reading.raw_verified is False and reading.replayed is not None
    assert reading.replayed[0] == "failed"
    assert sample.replayed_outcome == "failed"


def test_a_bundle_rehashed_after_tampering_still_changes_the_verdict():
    """The hash alone is not the authority: a bundle rewritten with a
    matching hash is replayed from its content, and the recomputed verdict
    disagrees with the stored one."""
    history = healthy_history()
    sample = history["samples"][1]
    sample["readings"] = [
        with_sample_time(r, NOW + timedelta(seconds=60 + 200))
        for r in sample["readings"]
    ]
    result = replay_history(history)
    assert not result.consistent
    assert result.samples[1].replayed_outcome == "stale"
    assert result.samples[1].stored_outcome == "healthy"
    assert "OUTCOME_MISMATCH" in result.samples[1].integrity
    assert result.recovery_verdict == "unknown"


@pytest.mark.parametrize(
    "kind,outcome",
    [("degraded", "degraded"), ("no-traffic", "no_data")],
)
def test_unknown_and_degraded_sessions_replay_consistently(kind, outcome):
    session_id = uuid4()
    taken = [
        take(
            kind,
            sequence=i + 1,
            window_end=NOW + timedelta(seconds=60 * i),
            session_id=session_id,
        )
        for i in range(3)
    ]
    history = stored_history(
        taken,
        session_id=session_id,
        authorized_at=NOW - timedelta(seconds=600),
        max_samples=3,
    )
    assert history["session"]["state"] == "expired"
    result = replay_history(history)
    assert result.consistent, result.integrity
    assert result.recovery_confirmed is False
    assert result.latest_sample_verdict == outcome
    assert result.recovery_verdict == (
        "degraded" if outcome == "degraded" else "unknown"
    )
    assert result.healthy_window_seconds == 0
    assert result.expected_lifecycle == "open" and result.recorded_lifecycle == "open"
    assert all(s.replayed_outcome == outcome for s in result.samples)
    assert result.reasons and all(isinstance(r, str) for r in result.reasons)


def test_an_unreadable_or_mismatching_profile_is_reported_not_guessed():
    history = healthy_history()
    profile = history["health_profile"]
    # content no longer hashes to its revision
    profile["content"] = profile["content"].replace('"max":0.01', '"max":0.5')
    result = replay_history(history)
    assert not result.consistent
    assert "HEALTH_PROFILE_UNREADABLE" in result.integrity
    assert result.recovery_verdict == "unknown"
    assert all(s.replayed_outcome is None for s in result.samples)
    # a session without a profile revision never confirms and is not an
    # integrity problem: there is nothing to recompute against
    bare = healthy_history(profile=PROFILE)
    bare["session"]["health_profile_revision"] = None
    bare["health_profile"] = None
    for row in bare["samples"]:
        row["health_profile_revision"] = None
        row["confirms_health"] = False
        row["health_basis"] = "no_health_profile"
        row["transition"] = None
    bare["session"]["state"], bare["session"]["ended_reason"] = "authorized", None
    bare["endings"] = []
    bare["incident_lifecycle"] = "observing_recovery"
    result = replay_history(bare)
    assert result.consistent
    assert result.recovery_verdict == "unknown" and not result.recovery_confirmed
    assert "NO_HEALTH_PROFILE" in result.reasons


def test_a_sample_without_reading_rows_cannot_be_recomputed_and_says_so():
    history = healthy_history()
    history["samples"][0]["readings"] = []
    result = replay_history(history)
    assert not result.consistent
    sample = result.samples[0]
    assert "NO_READINGS" in sample.integrity
    assert sample.replayed_outcome is None


def test_the_seam_takes_frozen_profile_handled_at_and_samples_only():
    history = healthy_history()
    frozen = dict(
        handled_at=history["session"]["authorized_at"],
        samples=history["samples"],
        session=history["session"],
        endings=history["endings"],
    )
    result = replay(
        profile=history["health_profile"]["content"],
        allow_telemetry=False,
        allow_model=False,
        **frozen,
    )
    assert result.consistent and result.recovery_verdict == "healthy"
    assert result.handled_at == history["session"]["authorized_at"]
    assert result.external_queries == () and result.model_requests == ()
    # the profile may also be given as the validated model
    assert replay(profile=PROFILE, **frozen).consistent
    with pytest.raises(ValueError, match="REPLAY_IS_OFFLINE"):
        replay(profile=PROFILE, allow_telemetry=True, **frozen)
    # the frozen content must be the session's revision
    other = history["health_profile"]["content"].replace('"max":0.01', '"max":0.5')
    mismatch = replay(profile=other, **frozen)
    assert "HEALTH_PROFILE_UNREADABLE" in mismatch.integrity


def test_the_summary_is_json_and_the_cli_exit_code_follows_consistency():
    history = healthy_history()
    session_id = history["session"]["session_id"]
    document = summary(replay_history(history))
    text = json.dumps(document)
    assert document["consistent"] is True
    assert document["recovery_verdict"] == "healthy"
    assert document["samples"][0]["readings"][0]["raw_sha256"]
    assert "raw" not in document["samples"][0]["readings"][0]
    assert str(session_id) in text

    class Store:
        def __init__(self, dsn):
            self.dsn = dsn
            self.closed = False

        def session_history(self, requested):
            assert requested == session_id
            return history

        def incident_sessions(self, incident_id):
            return [history["session"]]

        def close(self):
            self.closed = True

    stores: list[Store] = []

    def factory(dsn):
        stores.append(Store(dsn))
        return stores[-1]

    out = io.StringIO()
    code = main(
        ["--dsn", "host=x", "--session", str(session_id)],
        store_factory=factory,
        stdout=out,
    )
    assert code == 0 and stores[0].dsn == "host=x" and stores[0].closed
    assert json.loads(out.getvalue())[0]["consistent"] is True

    out = io.StringIO()
    code = main(
        ["--dsn", "host=x", "--incident", str(history["session"]["incident_id"])],
        store_factory=factory,
        stdout=out,
    )
    assert code == 0 and len(json.loads(out.getvalue())) == 1

    for row in history["samples"]:
        row["outcome"] = "degraded"
    out = io.StringIO()
    code = main(
        ["--dsn", "host=x", "--session", str(session_id)],
        store_factory=factory,
        stdout=out,
    )
    assert code == 1
    assert json.loads(out.getvalue())[0]["recovery_verdict"] == "unknown"
    assert main(["--session", str(session_id)], store_factory=factory, env={}) == 2


def test_rehash_helper_keeps_the_hash_honest():
    history = healthy_history(count=1)
    reading = history["samples"][0]["readings"][0]
    row = rehash(reading, b"{}")
    assert row["raw_sha256"] == hashlib.sha256(b"{}").hexdigest()
