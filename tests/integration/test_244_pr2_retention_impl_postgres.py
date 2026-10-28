"""#244 PR-2: the retention cleanup CLI (contract r3 of the task record).

Every run connects as a throwaway login in ``opspilot_retention``; the
upstream records are made by the product (F13 harness: intake, follow-up,
cancel, close, postmortem generation and review) and the close time is
backdated by the owner, the only seeding shortcut. Covered: dry-run lists
the five payload kinds without writing, ``--apply`` cleans them keeping
hashes and views, each retention condition blocks with its code, a merge
group blocks as a whole, a reopen between scan and lock is caught by the
recheck, one failing row does not roll back the other incidents, and the
process entry's exit codes.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from uuid import UUID, uuid4

import psycopg
import pytest

from opspilot.persistence.grants import RETENTION_ROLE
from opspilot.retention import RetentionReport, run_retention
from tests.acceptance.test_f13_postmortem import dsn, h  # noqa: F401 - fixtures
from tests.f13_acceptance_support import CountingModel, output
from tests.runtime_roles_support import login

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)

TABLES = (
    "opspilot_evidence",
    "opspilot_observation_signal_readings",
    "opspilot_alert_deliveries",
)


@pytest.fixture(scope="module")
def retention(dsn):  # noqa: F811
    with login(dsn, RETENTION_ROLE) as value:
        yield value


def _closed(h, incident: UUID, *, days_ago: int = 31) -> None:  # noqa: F811
    response = h.control(incident, "close")
    assert response.status == 200, response.text
    with psycopg.connect(h.dsn) as conn:
        conn.execute(
            "UPDATE opspilot_incidents SET closed_at=clock_timestamp() - make_interval(days => %s) "
            "WHERE incident_id=%s",
            (days_ago, incident),
        )


def _payloads(h, incident: UUID) -> dict[str, str]:  # noqa: F811
    """A present Observer reading and an alert delivery next to the seeded
    evidence; returns the object id of each, keyed by table."""
    raw = b'{"status":"success"}'
    with psycopg.connect(h.dsn) as conn:
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
        delivery = conn.execute(
            "INSERT INTO opspilot_alert_deliveries(delivery_key,fingerprint,starts_at,"
            "starts_at_raw,status,outcome,incident_id,actor,raw_sha256,annotations_sha256,"
            "annotation_revision,alert_json,truncated) VALUES(%s,'fp',now(),'t','firing',"
            "'created',%s,'am',%s,%s,1,'{}',false) RETURNING delivery_id",
            (f"k-{uuid4()}", incident, "a" * 64, "b" * 64),
        ).fetchone()[0]
    return {
        "opspilot_evidence": f"ev-{incident}",
        "opspilot_observation_signal_readings": f"{sample}:latency",
        "opspilot_alert_deliveries": str(delivery),
    }


def _states(h, incident: UUID) -> dict[str, list[str]]:  # noqa: F811
    with psycopg.connect(h.dsn) as conn:
        return {
            "opspilot_evidence": [
                r[0]
                for r in conn.execute(
                    "SELECT e.payload_state FROM opspilot_evidence e "
                    "JOIN opspilot_runs r ON r.run_id::text=e.run_id WHERE r.incident_id=%s",
                    (incident,),
                )
            ],
            "opspilot_observation_signal_readings": sorted(
                r[0]
                for r in conn.execute(
                    "SELECT g.payload_state FROM opspilot_observation_signal_readings g "
                    "JOIN opspilot_observation_samples s USING (sample_id) "
                    "JOIN opspilot_observation_sessions o ON o.session_id=s.session_id "
                    "WHERE o.incident_id=%s",
                    (incident,),
                )
            ),
            "opspilot_alert_deliveries": [
                r[0]
                for r in conn.execute(
                    "SELECT payload_state FROM opspilot_alert_deliveries WHERE incident_id=%s",
                    (incident,),
                )
            ],
        }


def _mine(report: RetentionReport, incident: UUID) -> list[dict]:
    return [line for line in report.lines if line.get("incident_id") == str(incident)]


def _blocked(report: RetentionReport, incident: UUID) -> list[str] | None:
    for line in _mine(report, incident):
        if line["kind"] == "blocked":
            return line["blocked_by"]
    return None


# --- configuration


def test_fewer_than_thirty_days_is_refused(retention) -> None:
    for days in (29, 0, -1):
        with pytest.raises(ValueError, match="RETENTION_DAYS_INVALID"):
            run_retention(retention, days=days)


def test_the_process_entry_exit_codes(retention) -> None:
    def run(*args, env=None):
        return subprocess.run(
            [sys.executable, "-m", "opspilot.retention", *args],
            capture_output=True,
            text=True,
            env={**os.environ, **(env or {})},
            timeout=120,
        )

    missing = {k: v for k, v in os.environ.items() if k != "OPSPILOT_RETENTION_DSN"}
    result = subprocess.run(
        [sys.executable, "-m", "opspilot.retention"],
        capture_output=True,
        text=True,
        env=missing,
        timeout=120,
    )
    assert result.returncode == 2
    for days in ("29", "0", "x"):
        bad = run("--days", days, env={"OPSPILOT_RETENTION_DSN": retention})
        assert bad.returncode == 2 and "RETENTION_DAYS_INVALID" in bad.stderr
    ok = run(env={"OPSPILOT_RETENTION_DSN": retention})
    assert ok.returncode == 0, ok.stderr
    lines = [json.loads(line) for line in ok.stdout.splitlines()]
    assert lines[-1]["kind"] == "summary" and lines[-1]["apply"] is False
    assert lines[-1]["days"] == 30


# --- dry-run and apply


def test_dry_run_lists_candidates_and_writes_nothing_then_apply_cleans(
    h,
    retention,  # noqa: F811
) -> None:
    incident, _, _, _ = h.seed()
    objects = _payloads(h, incident)
    _closed(h, incident)
    before = _states(h, incident)

    report = run_retention(retention)
    candidates = {
        (line["table"], line["object_id"]): line
        for line in _mine(report, incident)
        if line["kind"] == "candidate"
    }
    assert {(t, o) for t, o in objects.items()} <= set(candidates)
    assert all(line["raw_sha256"] for line in candidates.values())
    assert _states(h, incident) == before
    assert report.lines[-1]["kind"] == "summary" and report.failed == 0

    with psycopg.connect(h.dsn) as conn:
        hashes = conn.execute(
            "SELECT raw_sha256, view_sha256 FROM opspilot_evidence WHERE evidence_id=%s",
            (objects["opspilot_evidence"],),
        ).fetchone()
    applied = run_retention(retention, apply=True)
    cleaned = {
        (line["table"], line["object_id"])
        for line in _mine(applied, incident)
        if line["kind"] == "cleaned"
    }
    assert cleaned == set(candidates)
    after = _states(h, incident)
    assert after["opspilot_evidence"] == ["cleaned"]
    assert after["opspilot_alert_deliveries"] == ["cleaned"]
    # the readings stored without raw stay absent, never cleaned
    assert after["opspilot_observation_signal_readings"].count("cleaned") == 1
    assert "present" not in after["opspilot_observation_signal_readings"]
    with psycopg.connect(h.dsn) as conn:
        assert (
            conn.execute(
                "SELECT raw_sha256, view_sha256 FROM opspilot_evidence WHERE evidence_id=%s",
                (objects["opspilot_evidence"],),
            ).fetchone()
            == hashes
        )
    summary = applied.lines[-1]
    assert summary["apply"] is True and summary["cleaned"] >= len(cleaned)
    assert not _mine(run_retention(retention), incident)


# --- what keeps an incident's payloads


def test_each_retention_condition_blocks_with_its_code(h, retention) -> None:  # noqa: F811
    open_one, _, _, _ = h.seed()
    _payloads(h, open_one)

    recent, _, _, _ = h.seed()
    _closed(h, recent, days_ago=3)

    legacy, _, _, _ = h.seed()
    _closed(h, legacy)
    with psycopg.connect(h.dsn) as conn:
        conn.execute(
            "UPDATE opspilot_incidents SET closed_at=NULL WHERE incident_id=%s",
            (legacy,),
        )

    running, _, _, _ = h.seed()
    _closed(h, running)
    observing, _, session, _ = h.seed()
    _closed(h, observing)
    pending, _, _, _ = h.seed()
    _closed(h, pending)
    with psycopg.connect(h.dsn) as conn:
        conn.execute(
            "INSERT INTO opspilot_runs(run_id,incident_id,state,control_generation,"
            "budget_limit,deadline,versions) VALUES(%s,%s,'queued',0,1,"
            "clock_timestamp()+interval '1 hour','{}')",
            (uuid4(), running),
        )
        conn.execute(
            "UPDATE opspilot_observation_sessions SET state='authorized',ended_reason=NULL "
            "WHERE session_id=%s",
            (session,),
        )
        conn.execute(
            "INSERT INTO opspilot_postmortem_generation_jobs(incident_id,"
            "pending_regeneration_version) VALUES(%s,1)",
            (pending,),
        )

    # close first: a close marks an open draft stale (M1-03 D18)
    reviewing, ev, _, _ = h.seed()
    _closed(h, reviewing)
    h.generate(reviewing, CountingModel(output(ev)))

    known, ev, _, _ = h.seed()
    h.generate(known, CountingModel(output(ev)))
    assert h.review(known).status == 200
    _closed(h, known)

    report = run_retention(retention, apply=True)
    expected = {
        open_one: "NOT_CLOSED",
        recent: "WITHIN_RETENTION",
        legacy: "CLOSED_AT_MISSING",
        running: "ACTIVE_RUN",
        observing: "ACTIVE_OBSERVATION",
        pending: "POSTMORTEM_GENERATION_PENDING",
        reviewing: "DRAFT_UNDER_REVIEW",
        known: "ACTIVE_KNOWLEDGE",
    }
    for incident, code in expected.items():
        blocked = _blocked(report, incident)
        assert blocked is not None and code in blocked, (code, blocked)
        assert not [
            line for line in _mine(report, incident) if line["kind"] == "cleaned"
        ], code
        assert "cleaned" not in sum(_states(h, incident).values(), []), code


def test_a_merge_group_is_kept_while_any_member_is(h, retention) -> None:  # noqa: F811
    keeper, _, _, _ = h.seed()
    merged, _, _, _ = h.seed()
    _closed(h, keeper)
    _closed(h, merged, days_ago=3)
    with psycopg.connect(h.dsn) as conn:
        conn.execute(
            "UPDATE opspilot_incidents SET merged_into_incident_id=%s WHERE incident_id=%s",
            (keeper, merged),
        )
    report = run_retention(retention, apply=True)
    assert "WITHIN_RETENTION" in _blocked(report, merged)
    assert _blocked(report, keeper) == ["MERGE_GROUP_BLOCKED"]
    assert "cleaned" not in sum(_states(h, keeper).values(), [])
    # once the member is old enough the group goes together
    with psycopg.connect(h.dsn) as conn:
        conn.execute(
            "UPDATE opspilot_incidents SET closed_at=closed_at - interval '30 days' "
            "WHERE incident_id=%s",
            (merged,),
        )
    report = run_retention(retention, apply=True)
    for incident in (keeper, merged):
        assert _states(h, incident)["opspilot_evidence"] == ["cleaned"]


# --- races and failures


def test_a_reopen_between_scan_and_lock_is_caught_by_the_recheck(
    h,
    retention,  # noqa: F811
) -> None:
    incident, _, _, _ = h.seed()
    _closed(h, incident)

    def reopen() -> None:
        assert h.control(incident, "reopen").status == 200

    report = run_retention(retention, apply=True, after_scan=reopen)
    assert _blocked(report, incident) == ["CHANGED_AFTER_SCAN"]
    assert not [line for line in _mine(report, incident) if line["kind"] == "cleaned"]
    assert _states(h, incident)["opspilot_evidence"] == ["present"]


def test_one_failing_row_is_recorded_and_other_incidents_still_commit(
    h,
    retention,  # noqa: F811
) -> None:
    broken, _, _, _ = h.seed()
    fine, _, _, _ = h.seed()
    _closed(h, broken)
    _closed(h, fine)
    with psycopg.connect(h.dsn, autocommit=True) as conn:
        conn.execute(
            "CREATE FUNCTION impl_244_boom() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN IF NEW.evidence_id = 'ev-" + str(broken) + "' THEN "
            "RAISE EXCEPTION 'injected'; END IF; RETURN NEW; END $$"
        )
        conn.execute(
            "CREATE TRIGGER impl_244_boom BEFORE UPDATE ON opspilot_evidence "
            "FOR EACH ROW EXECUTE FUNCTION impl_244_boom()"
        )
    try:
        report = run_retention(retention, apply=True)
        process = subprocess.run(
            [sys.executable, "-m", "opspilot.retention", "--apply"],
            capture_output=True,
            text=True,
            env={**os.environ, "OPSPILOT_RETENTION_DSN": retention},
            timeout=120,
        )
    finally:
        with psycopg.connect(h.dsn, autocommit=True) as conn:
            conn.execute("DROP TRIGGER impl_244_boom ON opspilot_evidence")
            conn.execute("DROP FUNCTION impl_244_boom()")
    failed = [line for line in _mine(report, broken) if line["kind"] == "failed"]
    assert [line["object_id"] for line in failed] == [f"ev-{broken}"]
    assert failed[0]["error"]
    assert report.failed >= 1 and report.lines[-1]["failed"] == report.failed
    assert _states(h, fine)["opspilot_evidence"] == ["cleaned"]
    assert _states(h, broken)["opspilot_evidence"] == ["present"]
    assert process.returncode == 1
