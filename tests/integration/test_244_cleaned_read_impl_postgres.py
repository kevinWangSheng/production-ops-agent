"""#244 PR-3: what the read side shows once the retention cleanup ran.

Cleaned rows are made the only way the product allows: a login in
``opspilot_retention`` running the guarded present -> cleaned update. Covered:
the evidence API and page (investigation evidence and a recovery reading),
the alert section and the alert context, the F6 replay (cleaned versus
absent versus tampered) and the release / comparison replay.
"""

# ruff: noqa: F811 - the M1-02 suite's fixtures are imported by name and
# then named again as test parameters, which is how pytest binds them

from __future__ import annotations

import hashlib
import os
from uuid import UUID

import psycopg
import pytest

from opspilot.alertmanager import alert_context_of_record
from opspilot.observer.loop import ObserverLoop
from opspilot.observer.release_replay import replay_release_history
from opspilot.observer.replay import (
    DATA_CLEANED,
    INTEGRITY_MISMATCH,
    replay_stored_session,
)
from opspilot.persistence.grants import RETENTION_ROLE
from scripts.m0.postgres_lab import DSN
from tests.f13_acceptance_support import seed_upstream
from tests.integration import test_f11_pr4a_comparison_impl_postgres as pr4a
from tests.integration.test_m1_02_observer_postgres import (  # noqa: F401 - fixtures
    _backdate_authorization,
    _incident,
    controller,
    loop,
    observer,
    observer_dsn,
    owner,
    scratch_dsn,
    telemetry,
)
from tests.integration.test_m1_02_replay_postgres import _authorize, _run
from tests.integration.test_m1_04_alert_intake_impl_postgres import (
    _alert,
    _build,
    _post,
    _webhook,
)
from tests.m1_web_support import basic, call
from tests.runtime_roles_support import login

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)

release_harness = pr4a.release_harness


def _clean(dsn: str, statement: str, *args) -> int:
    """The guarded cleaning update, as a retention login."""
    with login(dsn, RETENTION_ROLE) as retention, psycopg.connect(retention) as conn:
        return conn.execute(statement, args).rowcount


# --- evidence


def test_cleaned_evidence_is_200_with_the_cleaned_state_as_json_and_page() -> None:
    app, _, _ = _build()
    incident = seed_upstream(DSN)
    evidence = f"ev-{incident}"
    assert (
        _clean(
            DSN,
            "UPDATE opspilot_evidence SET raw=NULL,payload_state='cleaned' "
            "WHERE evidence_id=%s",
            evidence,
        )
        == 1
    )
    response = call(
        app, "GET", f"/incidents/{incident}/evidence/{evidence}", headers=basic()
    )
    assert response.status == 200, response.text
    body = response.json()
    assert body["evidence_id"] == evidence
    assert body["payload_state"] == "cleaned"
    assert body["raw_unavailable"] == "CLEANED_DATA"
    assert "raw_utf8" not in body and "raw_base64" not in body
    assert body["raw_sha256"] == "0" * 64 and body["view_sha256"] == "1" * 64
    assert body["hashes_verified"] is None
    assert body["view"]["evidence_id"] == evidence
    page = call(
        app,
        "GET",
        f"/incidents/{incident}/evidence/{evidence}",
        headers={**basic(), "accept": "text/html"},
    )
    assert page.status == 200, page.text
    assert "CLEANED_DATA" in page.text and 'id="raw-cleaned"' in page.text


def test_present_evidence_keeps_its_established_key_set() -> None:
    """The two cleaned keys appear only on a cleaned record (F12 G2 pins the
    key set of a present one)."""
    app, _, _ = _build()
    incident = seed_upstream(DSN)
    body = call(
        app, "GET", f"/incidents/{incident}/evidence/ev-{incident}", headers=basic()
    ).json()
    assert "payload_state" not in body and "raw_unavailable" not in body
    assert "raw_utf8" in body


def test_a_missing_raw_without_the_cleaned_state_is_not_shown_as_cleaned() -> None:
    """Review #382: only ``payload_state == "cleaned"`` reads as cleaned; a
    record without raw in any other state is unverifiable, nothing more."""
    from dataclasses import replace

    app, workbench, _ = _build()
    incident = seed_upstream(DSN)
    record = workbench.evidence_for(incident, f"ev-{incident}")
    assert record is not None
    workbench.evidence_for = lambda *_: replace(record, raw=None)
    response = call(
        app, "GET", f"/incidents/{incident}/evidence/ev-{incident}", headers=basic()
    )
    assert response.status == 200, response.text
    body = response.json()
    assert "raw_unavailable" not in body and "payload_state" not in body
    assert "raw_utf8" not in body and body["hashes_verified"] is None


def _present_reading(incident: UUID) -> tuple[UUID, str]:
    raw = b'{"status":"success"}'
    with psycopg.connect(DSN) as conn:
        sample, signal = conn.execute(
            "SELECT r.sample_id, r.signal_name FROM opspilot_observation_signal_readings r "
            "JOIN opspilot_observation_samples s USING (sample_id) "
            "JOIN opspilot_observation_sessions o ON o.session_id=s.session_id "
            "WHERE o.incident_id=%s ORDER BY s.sequence, r.signal_name LIMIT 1",
            (incident,),
        ).fetchone()
        conn.execute(
            "INSERT INTO opspilot_observation_signal_readings(sample_id,signal_name,status,"
            "value,sample_count,query,window_start,window_end,source,raw_sha256,raw) "
            "SELECT sample_id,'latency',status,value,sample_count,query,window_start,"
            "window_end,source,%s,%s FROM opspilot_observation_signal_readings "
            "WHERE sample_id=%s AND signal_name=%s",
            (hashlib.sha256(raw).hexdigest(), raw, sample, signal),
        )
    return sample, signal


def test_a_cleaned_recovery_reading_is_200_and_says_cleaned_an_absent_one_absent() -> (
    None
):
    app, _, _ = _build()
    incident = seed_upstream(DSN)
    sample, absent = _present_reading(incident)
    _clean(
        DSN,
        "UPDATE opspilot_observation_signal_readings SET raw=NULL,"
        "payload_state='cleaned' WHERE sample_id=%s AND signal_name='latency'",
        sample,
    )
    cleaned = call(
        app,
        "GET",
        f"/incidents/{incident}/evidence/{sample}:latency",
        headers=basic(),
    )
    assert cleaned.status == 200
    body = cleaned.json()
    assert body["payload_state"] == "cleaned"
    assert body["raw_unavailable"] == "CLEANED_DATA"
    assert body["hashes_verified"] is None and body["raw_sha256"]
    other = call(
        app,
        "GET",
        f"/incidents/{incident}/evidence/{sample}:{absent}",
        headers=basic(),
    ).json()
    assert "raw_unavailable" not in other and "payload_state" not in other
    assert other["hashes_verified"] is None
    # the incident page's recovery section says the data was cleaned
    page = call(app, "GET", f"/incidents/{incident}", headers=basic())
    assert page.status == 200, page.text
    assert 'class="recovery-data-cleaned"' in page.text
    assert "STORED_OBSERVATION_DATA_CLEANED" in page.text


# --- alerts


def test_a_cleaned_alert_shows_as_cleaned_not_as_a_cut_record() -> None:
    app, _, _ = _build()
    alert = _alert(annotations={"summary": "checkout errors"})
    result = _post(app, _webhook(alert)).json()["results"][0]
    incident = result["incident_id"]
    assert (
        _clean(
            DSN,
            "UPDATE opspilot_alert_deliveries SET alert_json=NULL,"
            "payload_state='cleaned' WHERE incident_id=%s",
            incident,
        )
        == 1
    )
    page = call(app, "GET", f"/incidents/{incident}", headers=basic())
    assert page.status == 200, page.text
    assert 'id="alert-cleaned"' in page.text and "CLEANED_DATA" in page.text
    assert 'id="alert-record-text"' not in page.text
    assert "checkout errors" not in page.text
    # the deliveries table keeps its hash and metadata
    assert result["incident_id"] in page.text


def test_the_alert_context_of_a_cleaned_record_is_none_without_raising() -> None:
    assert alert_context_of_record(None) is None


# --- F6 replay


def _clean_session(dsn: str, session: object) -> int:
    return _clean(
        dsn,
        "UPDATE opspilot_observation_signal_readings SET raw=NULL,"
        "payload_state='cleaned' WHERE payload_state='present' AND sample_id IN "
        "(SELECT sample_id FROM opspilot_observation_samples WHERE session_id=%s)",
        session,
    )


def test_a_cleaned_session_replays_as_data_cleaned_not_as_tampering(
    loop, owner, controller, scratch_dsn
) -> None:
    incident, _, target = _incident(owner)
    session = _authorize(controller, incident, target, sustained=1)
    _backdate_authorization(owner, session)
    _run(loop, owner, session, 2)
    assert replay_stored_session(controller, session).consistent
    assert _clean_session(scratch_dsn, session) > 0

    result = replay_stored_session(controller, session)

    assert result.recovery_verdict == "unknown" and not result.recovery_confirmed
    assert DATA_CLEANED in result.integrity
    assert INTEGRITY_MISMATCH not in result.integrity
    assert DATA_CLEANED in result.recovery_reasons
    readings = [r for s in result.samples for r in s.readings]
    assert readings and all(r.raw_verified is None for r in readings)
    # the query metadata of every reading stays
    assert all(r.query and r.raw_sha256 for r in readings)


def test_tampering_next_to_cleaned_data_is_still_an_integrity_mismatch(
    loop, owner, controller, scratch_dsn
) -> None:
    incident, _, target = _incident(owner)
    session = _authorize(controller, incident, target, sustained=1)
    _backdate_authorization(owner, session)
    _run(loop, owner, session, 2)
    _clean_session(scratch_dsn, session)
    with psycopg.connect(scratch_dsn) as conn:
        conn.execute(
            "UPDATE opspilot_observation_samples SET outcome='degraded' "
            "WHERE session_id=%s AND sequence=1",
            (session,),
        )
    result = replay_stored_session(controller, session)
    assert result.recovery_verdict == "unknown"
    assert INTEGRITY_MISMATCH in result.integrity
    assert DATA_CLEANED in result.integrity


# --- release replay


def test_cleaned_release_and_comparison_replay_as_data_cleaned_keeping_queries(
    release_harness,
) -> None:
    _, observer, oid, _, _ = release_harness
    context = pr4a._anomalous(release_harness)
    ObserverLoop(
        store=observer, source=pr4a.BeforeTelemetry(context), batch=1
    ).poll_once()
    history = {
        **observer.release_history(oid),
        "comparisons": observer.release_comparisons(oid),
    }
    assert replay_release_history(history).consistent
    queries = sum(len(m["queries"]) for m in history["samples"])
    compared = sum(len(c["queries"]) for c in history["comparisons"])
    assert (
        _clean(
            DSN,
            "UPDATE opspilot_release_query_results SET raw=NULL,payload_state='cleaned' "
            "WHERE sample_id IN (SELECT sample_id FROM opspilot_observation_samples "
            "WHERE subject_id=%s)",
            oid,
        )
        == queries
    )
    assert (
        _clean(
            DSN,
            "UPDATE opspilot_release_comparison_queries SET raw=NULL,"
            "payload_state='cleaned' WHERE comparison_id IN (SELECT comparison_id "
            "FROM opspilot_release_comparisons WHERE observation_id=%s)",
            oid,
        )
        == compared
    )
    history = {
        **observer.release_history(oid),
        "comparisons": observer.release_comparisons(oid),
    }
    # query metadata stays: every row, its key, query text and hash
    assert sum(len(m["queries"]) for m in history["samples"]) == queries
    for row in [q for m in history["samples"] for q in m["queries"]] + [
        q for c in history["comparisons"] for q in c["queries"]
    ]:
        assert row["raw"] is None and row["payload_state"] == "cleaned"
        assert row["query_key"] and row["query"] and row["raw_sha256"]

    replay = replay_release_history(history)
    assert not replay.consistent and replay.verdict == "unknown"
    assert replay.integrity == ("REPLAY_DATA_CLEANED",)

    (session,) = observer.release_history(oid)["sessions"]
    stored = observer.replay_session(session["session_id"])
    assert not stored.consistent and "REPLAY_DATA_CLEANED" in stored.integrity


def test_tampering_next_to_cleaned_release_data_is_still_reported(
    release_harness,
) -> None:
    """Review #382: cleaned queries skip only their own recompute; the
    release, sample and audit checks still run."""
    _, observer, oid, _, _ = release_harness
    pr4a._anomalous(release_harness)
    _clean(
        DSN,
        "UPDATE opspilot_release_query_results SET raw=NULL,payload_state='cleaned' "
        "WHERE sample_id IN (SELECT sample_id FROM opspilot_observation_samples "
        "WHERE subject_id=%s)",
        oid,
    )
    history = observer.release_history(oid)
    assert replay_release_history(history).integrity == ("REPLAY_DATA_CLEANED",)
    for status in ("healthy", "observing"):
        forged = {
            **history,
            "release": history["release"].model_copy(update={"status": status}),
        }
        replay = replay_release_history(forged)
        assert "REPLAY_DATA_CLEANED" in replay.integrity
        assert set(replay.integrity) - {"REPLAY_DATA_CLEANED"}, status
        assert replay.verdict == "unknown"
    forged = {
        **history,
        "samples": [
            {**history["samples"][0], "outcome": "healthy"},
            *history["samples"][1:],
        ],
    }
    replay = replay_release_history(forged)
    assert "SAMPLE_DECISION_MISMATCH" in replay.integrity
    assert "REPLAY_DATA_CLEANED" in replay.integrity


def test_only_the_comparison_cleaned_still_replays_the_samples(
    release_harness,
) -> None:
    _, observer, oid, _, _ = release_harness
    context = pr4a._anomalous(release_harness)
    ObserverLoop(
        store=observer, source=pr4a.BeforeTelemetry(context), batch=1
    ).poll_once()
    _clean(
        DSN,
        "UPDATE opspilot_release_comparison_queries SET raw=NULL,"
        "payload_state='cleaned' WHERE comparison_id IN (SELECT comparison_id "
        "FROM opspilot_release_comparisons WHERE observation_id=%s)",
        oid,
    )
    replay = replay_release_history(
        {
            **observer.release_history(oid),
            "comparisons": observer.release_comparisons(oid),
        }
    )
    assert replay.integrity == ("REPLAY_DATA_CLEANED",)
    assert replay.sample_evaluations
