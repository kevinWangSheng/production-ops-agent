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
    PROFILE_SENTINEL,
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
    profile_with,
    rehash,
    stored_history,
    take,
    take_unavailable,
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
    short = profile_with(max_samples=3, sustained_window_seconds=60)
    taken = [
        take(
            kind,
            sequence=i + 1,
            window_end=NOW + timedelta(seconds=60 * i),
            session_id=session_id,
            profile=short,
        )
        for i in range(3)
    ]
    history = stored_history(
        taken,
        profile=short,
        session_id=session_id,
        authorized_at=NOW - timedelta(seconds=600),
        max_samples=3,
        sustained_window_seconds=60,
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
    bare["session"]["healthy_since"] = None  # no profile: no streak
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


# --- independent review of PR #139 (Codex, 4 x P2): tampering that must not pass


@pytest.mark.parametrize(
    "outcome,present",
    [("degraded", True), ("no_data", False)],
)
def test_deleted_readings_cannot_hide_behind_a_non_healthy_verdict(outcome, present):
    """P2-1: reading rows deleted and the verdict rewritten as not healthy
    was replayed as consistent and the rewritten verdict repeated. An
    adopted sample under a readable profile must carry a reading for every
    profile signal; nothing in the sample row says "filed without readings"
    (that only happens without a revision or with an unreadable profile,
    neither of which holds here)."""
    history = healthy_history(count=1)
    sample = history["samples"][0]
    sample["readings"] = []
    sample["outcome"] = outcome
    sample["required_signals_present"] = present
    sample["confirms_health"] = False
    sample["health_basis"] = "outcome_not_healthy"
    history["session"]["healthy_since"] = None
    result = replay_history(history)
    assert not result.consistent
    assert INTEGRITY_MISMATCH in result.integrity
    assert result.recovery_verdict == "unknown"
    assert "NO_READINGS" in result.samples[0].integrity
    # one deleted reading among several is reported by name
    partial = healthy_history(count=1)
    partial["samples"][0]["readings"] = partial["samples"][0]["readings"][1:]
    partial["samples"][0]["outcome"] = "no_data"
    partial["samples"][0]["required_signals_present"] = False
    partial["samples"][0]["confirms_health"] = False
    partial["samples"][0]["health_basis"] = "outcome_not_healthy"
    partial["session"]["healthy_since"] = None
    result = replay_history(partial)
    assert not result.consistent and result.recovery_verdict == "unknown"
    assert "READING_MISSING:rate" in result.samples[0].integrity


def test_a_deleted_sample_or_a_tampered_watermark_is_detected():
    """P2-2: the session row's watermarks (adopted count/sequence/window end,
    healthy_since) are recomputed by the fold and must match."""
    history = healthy_history()
    del history["samples"][2]
    result = replay_history(history)
    assert not result.consistent
    assert "WATERMARK_MISMATCH" in result.integrity
    assert result.recovery_verdict == "unknown"
    for field, value in (
        ("adopted_count", 0),
        ("adopted_sequence", 1),
        ("adopted_window_end", None),
        ("healthy_since", None),
    ):
        tampered = healthy_history()
        tampered["session"][field] = value
        result = replay_history(tampered)
        assert "WATERMARK_MISMATCH" in result.integrity, field
        assert result.recovery_verdict == "unknown", field
    assert replay_history(healthy_history()).consistent


def test_a_tampered_query_source_or_window_is_not_trusted_basis():
    """P2-3: the reading's query, source and window must be the frozen
    profile's and the sample's (and the bundle's), not whatever the row says."""
    for field, value in (
        ("query", "totally_different_query"),
        ("source", "elsewhere"),
        ("window_start", NOW - timedelta(seconds=3600)),
    ):
        history = healthy_history()
        history["samples"][0]["readings"][0][field] = value
        result = replay_history(history)
        assert not result.consistent, field
        assert result.recovery_verdict == "unknown", field
        assert any(
            code.startswith("READING_BASIS_MISMATCH:rate")
            for code in result.samples[0].integrity
        ), (field, result.samples[0].integrity)
    # a reading for a signal the profile does not know is no basis either
    history = healthy_history()
    history["samples"][0]["readings"][0]["signal_name"] = "bogus"
    result = replay_history(history)
    assert not result.consistent and result.recovery_verdict == "unknown"
    assert "UNKNOWN_SIGNAL:bogus" in result.samples[0].integrity


def test_a_malformed_reading_row_is_an_integrity_mismatch_not_a_crash():
    """P2-4: a value the database column accepts but the domain does not
    (``source`` with a space) must come out as unknown + mismatch, with the
    CLI exiting 1 instead of raising."""
    history = healthy_history()
    history["samples"][0]["readings"][0]["source"] = "BAD SOURCE"
    result = replay_history(history)
    assert not result.consistent and result.recovery_verdict == "unknown"
    assert INTEGRITY_MISMATCH in result.integrity
    assert any(
        code.startswith("BASIS_UNPARSABLE") for code in result.samples[0].integrity
    )

    class Store:
        def __init__(self, dsn):
            pass

        def session_history(self, requested):
            return history

        def close(self):
            pass

    out = io.StringIO()
    code = main(
        ["--dsn", "host=x", "--session", str(history["session"]["session_id"])],
        store_factory=Store,
        stdout=out,
    )
    assert code == 1
    assert json.loads(out.getvalue())[0]["recovery_verdict"] == "unknown"
    # a history the fold itself cannot parse still yields a result and 1
    history["samples"][0]["outcome"] = "bogus"

    out = io.StringIO()
    code = main(
        ["--dsn", "host=x", "--session", str(history["session"]["session_id"])],
        store_factory=Store,
        stdout=out,
    )
    assert code == 1
    (document,) = json.loads(out.getvalue())
    assert document["recovery_verdict"] == "unknown" and not document["consistent"]
    assert any(c.startswith("REPLAY_FAILED") for c in document["integrity"])


# --- Codex recheck of PR #139 (4 x P2)


@pytest.mark.parametrize("kind", ["coverage", "freshness"])
def test_coverage_and_freshness_queries_must_be_the_frozen_profiles(kind):
    """Recheck P2-1: the point-count and freshness queries carry the
    minimum-samples and staleness judgement; a bundle whose expression is
    another target's is no basis even when its hash is rewritten."""
    history = healthy_history()
    reading = history["samples"][0]["readings"][0]
    bundle = json.loads(bytes(reading["raw"]))
    bundle[kind]["expr"] = "some_other_target_query"
    history["samples"][0]["readings"][0] = rehash(
        reading, json.dumps(bundle, sort_keys=True, separators=(",", ":")).encode()
    )
    result = replay_history(history)
    assert not result.consistent and result.recovery_verdict == "unknown"
    assert f"READING_BASIS_MISMATCH:rate:bundle_{kind}" in result.samples[0].integrity


def test_the_frozen_deadline_is_recomputed_not_trusted():
    """Recheck P2-2: ``within_deadline`` is recomputed from the session's
    frozen ``deadline_at`` and the sample window end; a stored flag that
    contradicts it is an integrity mismatch and nothing is confirmed."""
    history = healthy_history()
    history["session"]["deadline_at"] = NOW - timedelta(seconds=1)
    result = replay_history(history)
    assert not result.consistent and result.recovery_verdict == "unknown"
    assert "DEADLINE_MISMATCH" in result.integrity
    assert "DEADLINE_MISMATCH" in result.samples[0].integrity
    assert result.recovery_confirmed is False


def test_every_ending_must_be_backed_by_the_replayed_fold():
    """Recheck P2-3: a budget ending needs the replayed adopted count to
    reach the frozen budget; a second ending record is one too many."""
    forged = healthy_history(count=1)
    session = forged["session"]
    session["state"], session["ended_reason"] = "expired", "max_samples_exhausted"
    forged["endings"] = [
        {
            "ending_id": uuid4(),
            "session_id": session["session_id"],
            "incident_id": session["incident_id"],
            "ended_reason": "max_samples_exhausted",
            "transition": "observation_ended_unconfirmed",
            "sample_id": None,
            "lifecycle_before": "observing_recovery",
            "lifecycle_after": "open",
            "recorded_at": NOW + timedelta(seconds=5),
        }
    ]
    forged["incident_lifecycle"] = "open"
    result = replay_history(forged)
    assert not result.consistent and result.recovery_verdict == "unknown"
    assert "ENDING_MISMATCH" in result.integrity

    extra = healthy_history()
    extra["endings"].insert(
        0,
        {
            "ending_id": uuid4(),
            "session_id": extra["session"]["session_id"],
            "incident_id": extra["session"]["incident_id"],
            "ended_reason": "authority_revoked",
            "transition": None,
            "sample_id": None,
            "lifecycle_before": "observing_recovery",
            "lifecycle_after": "observing_recovery",
            "recorded_at": NOW - timedelta(seconds=30),
        },
    )
    result = replay_history(extra)
    assert not result.consistent and result.recovery_verdict == "unknown"
    assert "ENDING_MISMATCH" in result.integrity


def test_a_sample_filed_while_the_profile_was_unavailable_is_a_recorded_reason():
    """Recheck P2-4: the Observer files a sample it could not judge (profile
    row unreadable at the time) with one sentinel reading that records why;
    the replay verifies that record and exempts the sample from the
    coverage check -- a sample whose readings were simply deleted has no
    such record and stays an integrity mismatch."""
    session_id = uuid4()
    first = take("healthy", sequence=1, window_end=NOW, session_id=session_id)
    unavailable = take_unavailable(
        sequence=2, window_end=NOW + timedelta(seconds=60), session_id=session_id
    )
    sample, readings = unavailable
    assert sample.outcome == "failed" and len(readings) == 1
    (sentinel,) = readings
    assert sentinel.signal_name == PROFILE_SENTINEL and sentinel.status == "failed"
    bundle = json.loads(sentinel.raw)
    assert bundle["reading_error"] == "HEALTH_PROFILE_UNAVAILABLE"
    assert bundle["error_type"] == "PersistenceError"
    later = [
        take(
            "healthy",
            sequence=3 + i,
            window_end=NOW + timedelta(seconds=120 + 60 * i),
            session_id=session_id,
        )
        for i in range(6)
    ]
    history = stored_history(
        [first, unavailable, *later],
        session_id=session_id,
        authorized_at=NOW - timedelta(seconds=600),
    )
    result = replay_history(history)
    assert result.consistent, result.integrity
    assert result.samples[1].recompute_skipped == "HEALTH_PROFILE_UNAVAILABLE"
    assert result.samples[1].integrity == ()
    assert result.recovery_verdict == "healthy"
    # without the record, the same empty sample is a damaged basis
    history["samples"][1]["readings"] = []
    result = replay_history(history)
    assert "NO_READINGS" in result.samples[1].integrity
    # a sentinel whose bundle does not verify is no record either
    history = stored_history(
        [first, unavailable, *later],
        session_id=session_id,
        authorized_at=NOW - timedelta(seconds=600),
    )
    row = history["samples"][1]["readings"][0]
    row["raw"] = bytes(row["raw"]) + b" "
    result = replay_history(history)
    assert not result.consistent
    assert "RAW_HASH_MISMATCH:health_profile" in result.samples[1].integrity


# --- final bot round on PR #139 (threads) and issue #141


def test_session_parameters_must_be_the_frozen_profiles():
    """store.py thread: the fold's budget, sustained window, interval and
    deadline come from the session row, which cannot vouch for itself; each
    must equal the frozen HealthProfile's session value."""
    for field, value in (
        ("max_samples", 5),
        ("sustained_window_seconds", 120),
        ("sample_interval_seconds", 30),
        ("deadline_at", NOW + timedelta(hours=5)),
    ):
        history = healthy_history()
        history["session"][field] = value
        result = replay_history(history)
        assert not result.consistent, field
        assert result.recovery_verdict == "unknown", field
        assert f"SESSION_PARAMETER_MISMATCH:{field}" in result.integrity, field
    assert replay_history(healthy_history()).consistent


def test_a_session_without_a_profile_never_yields_a_verdict():
    """replay.py thread, PRODUCT-CONSTRAINTS: no profile -> unknown, even
    when the stored samples say degraded."""
    session_id = uuid4()
    taken = [
        take(
            "degraded",
            sequence=i + 1,
            window_end=NOW + timedelta(seconds=60 * i),
            session_id=session_id,
        )
        for i in range(2)
    ]
    history = stored_history(
        taken,
        profile=None,
        session_id=session_id,
        authorized_at=NOW - timedelta(seconds=600),
    )
    for row in history["samples"]:
        row["health_profile_revision"] = None
    result = replay_history(history)
    assert result.consistent, result.integrity
    assert result.recovery_verdict == result.recomputed_verdict == "unknown"
    assert result.latest_sample_verdict == "degraded"
    assert "NO_HEALTH_PROFILE" in result.reasons


def test_scope_generations_are_recomputed_not_the_stored_flag():
    """store.py thread, C3 section 4: a sample whose recorded control-scope
    generations differ from the authorization's was taken under a moved
    scope whatever ``scope_suspended`` says; the contradiction is reported
    and the sample never counts towards recovery."""
    moved = healthy_history()
    moved["samples"][0]["global_generation"] = 1
    result = replay_history(moved)
    assert not result.consistent and result.recovery_verdict == "unknown"
    assert "SCOPE_MISMATCH" in result.samples[0].integrity
    assert result.samples[0].decision.replayed[0] == "history_only"
    assert result.samples[0].decision.replayed[1] == "suspended"
    flagged = healthy_history()
    flagged["samples"][1]["scope_suspended"] = True
    result = replay_history(flagged)
    assert not result.consistent and "SCOPE_MISMATCH" in result.samples[1].integrity


def test_a_sentinel_sample_keeps_the_outcome_and_window_the_observer_wrote():
    """Issue #141: the sentinel sample's outcome is what that path writes
    (failed, required signals missing) and its reading window is the
    sample's and the bundle's, like any other reading."""
    session_id = uuid4()
    first = take("healthy", sequence=1, window_end=NOW, session_id=session_id)
    unavailable = take_unavailable(
        sequence=2, window_end=NOW + timedelta(seconds=60), session_id=session_id
    )

    def build():
        return stored_history(
            [first, unavailable],
            session_id=session_id,
            authorized_at=NOW - timedelta(seconds=600),
        )

    assert replay_history(build()).consistent
    history = build()
    history["samples"][1]["outcome"] = "degraded"
    result = replay_history(history)
    assert not result.consistent and result.recovery_verdict == "unknown"
    assert "SENTINEL_MISMATCH:outcome" in result.samples[1].integrity
    history = build()
    history["samples"][1]["required_signals_present"] = True
    result = replay_history(history)
    assert "SENTINEL_MISMATCH:required_signals_present" in result.samples[1].integrity
    history = build()
    history["samples"][1]["readings"][0]["window_start"] = NOW - timedelta(hours=2)
    result = replay_history(history)
    assert not result.consistent
    assert "SENTINEL_MISMATCH:window" in result.samples[1].integrity
    history = build()
    row = history["samples"][1]["readings"][0]
    bundle = json.loads(bytes(row["raw"]))
    bundle["window_start"] = (NOW - timedelta(hours=2)).isoformat()
    history["samples"][1]["readings"][0] = rehash(
        row, json.dumps(bundle, sort_keys=True, separators=(",", ":")).encode()
    )
    result = replay_history(history)
    assert "SENTINEL_MISMATCH:bundle_window" in result.samples[1].integrity


# --- issue #142: bundled timestamps and the session deadline bind both ways


def _retime(history, index, **changes):
    reading = history["samples"][0]["readings"][index]
    bundle = json.loads(bytes(reading["raw"]))
    bundle.update(changes)
    history["samples"][0]["readings"][index] = rehash(
        reading, json.dumps(bundle, sort_keys=True, separators=(",", ":")).encode()
    )


def test_evaluated_at_is_bound_to_the_sample_window_end():
    """The instant the queries ran at is the window end; a rewritten
    ``evaluated_at`` with a rehashed bundle is no basis."""
    history = healthy_history()
    window_end = history["samples"][0]["window_end"]
    _retime(history, 0, evaluated_at=(window_end - timedelta(hours=2)).isoformat())
    result = replay_history(history)
    assert not result.consistent and result.recovery_verdict == "unknown"
    assert result.recovery_confirmed is False
    assert (
        "READING_BASIS_MISMATCH:rate:bundle_evaluated_at" in result.samples[0].integrity
    )


def test_a_bundle_without_evaluated_at_next_to_a_window_is_no_basis():
    history = healthy_history()
    reading = history["samples"][0]["readings"][0]
    bundle = json.loads(bytes(reading["raw"]))
    del bundle["evaluated_at"]
    history["samples"][0]["readings"][0] = rehash(
        reading, json.dumps(bundle, sort_keys=True, separators=(",", ":")).encode()
    )
    result = replay_history(history)
    assert not result.consistent and result.recovery_verdict == "unknown"


def test_every_reading_of_a_sample_carries_the_same_sample_time():
    """The replay judged freshness at the first bundle's ``sample_time``;
    a later bundle with another instant (rehashed) must not pass."""
    history = healthy_history()
    readings = history["samples"][0]["readings"]
    assert len(readings) > 1
    last = len(readings) - 1
    other = json.loads(bytes(readings[last]["raw"]))["sample_time"]
    shifted = (
        __import__("datetime").datetime.fromisoformat(other) + timedelta(seconds=5)
    ).isoformat()
    _retime(history, last, sample_time=shifted)
    result = replay_history(history)
    assert not result.consistent and result.recovery_verdict == "unknown"
    assert result.recovery_confirmed is False
    assert any(
        code.startswith("SAMPLE_TIME_MISMATCH") for code in result.samples[0].integrity
    )


@pytest.mark.parametrize("delta", [-2700, -61, 61, 3600])
def test_the_session_deadline_binds_in_both_directions(delta):
    """A 3600 s profile whose deadline is moved to 901 s (or beyond the
    skew tolerance either way) is not the frozen span."""
    history = healthy_history()
    history["session"]["deadline_at"] += timedelta(seconds=delta)
    result = replay_history(history)
    assert not result.consistent and result.recovery_verdict == "unknown"
    assert "SESSION_PARAMETER_MISMATCH:deadline_at" in result.integrity


@pytest.mark.parametrize("delta", [-60, -1, 0, 1, 60])
def test_the_session_deadline_allows_only_clock_skew(delta):
    history = healthy_history()
    history["session"]["deadline_at"] += timedelta(seconds=delta)
    result = replay_history(history)
    assert "SESSION_PARAMETER_MISMATCH:deadline_at" not in result.integrity


def test_deleting_the_window_does_not_escape_the_evaluated_at_binding():
    """PR #150 review P2: a query bundle without ``window_end`` and an
    earlier ``evaluated_at`` (rehashed) must still be refused."""
    history = healthy_history()
    reading = history["samples"][0]["readings"][0]
    bundle = json.loads(bytes(reading["raw"]))
    del bundle["window_end"]
    bundle["evaluated_at"] = (
        history["samples"][0]["window_end"] - timedelta(hours=2)
    ).isoformat()
    history["samples"][0]["readings"][0] = rehash(
        reading, json.dumps(bundle, sort_keys=True, separators=(",", ":")).encode()
    )
    result = replay_history(history)
    assert not result.consistent and result.recovery_verdict == "unknown"
    assert result.recovery_confirmed is False
    assert (
        "READING_BASIS_MISMATCH:rate:bundle_evaluated_at" in result.samples[0].integrity
    )


def test_a_forged_reading_error_marker_does_not_exempt_a_query_bundle():
    """PR #150 bot P1: ``reading_error`` on a bundle that still carries its
    query sections is no construction-failure bundle."""
    history = healthy_history()
    _retime(
        history,
        0,
        reading_error="FORGED",
        evaluated_at=(
            history["samples"][0]["window_end"] - timedelta(hours=2)
        ).isoformat(),
    )
    result = replay_history(history)
    assert not result.consistent and result.recovery_verdict == "unknown"
    assert result.recovery_confirmed is False
