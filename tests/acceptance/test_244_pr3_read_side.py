"""Issue #244 PR-3 independent read-side acceptance.

The fixtures create the cleaned state through a temporary LOGIN member of
``opspilot_retention``.  They do not use a superuser update to bypass the
0023 guard.  The assertions use the HTTP/read and replay seams named by the
brief; no private implementation call order is inspected.

Assumed public signatures (to align with the brief):

* ``GET /incidents/{incident_id}/evidence/{evidence_id}`` returns the evidence
  projection, including ``payload_state`` and ``raw_unavailable``.
* ``replay_stored_session(store, session_id)`` and
  ``replay_release_history(history)`` return replay DTOs whose ``integrity``
  contains the named reason and whose verdict is ``unknown`` when cleaned
  data prevents verification.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Iterator
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from opspilot import schema
from opspilot.observer.release_replay import replay_release_history
from opspilot.observer.replay import (
    INTEGRITY_MISMATCH,
    replay_history,
    replay_stored_session,
)
from opspilot.persistence import DurableStore
from opspilot.tools.registry import canonical_hash
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
)
from opspilot.web.charts import evidence_chart
from opspilot.web.evidence import StoredEvidence
from scripts.m0.postgres_lab import DSN
from tests.acceptance.test_244_pr1_retention_schema_roles import _seed_observer_graph
from tests.integration.test_f11_pr4a_comparison_impl_postgres import (
    BeforeTelemetry,
    _anomalous,
    _comparisons,
    _history,
)
from tests.integration.test_m1_02_observer_postgres import (
    _backdate_authorization,
    _incident,
)
from tests.integration.test_m1_02_replay_postgres import _authorize, _run
from tests.m1_02_replay_support import healthy_history
from tests.m1_web_support import ORIGIN, UI_PASSWORD, UI_USER, basic, call
from tests.runtime_roles_support import login

pytest_plugins = (
    "tests.integration.test_m1_02_observer_postgres",
    "tests.integration.test_f11_pr2b_impl_postgres",
)

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1",
    reason="explicit PG opt-in required: M1_DURABLE_POSTGRES=1",
)


@pytest.fixture(scope="module", autouse=True)
def migrated_lab() -> None:
    """The release replay fixture uses the opt-in lab DSN directly."""
    schema.migrate(DSN, pg_dump=os.environ.get("OPSPILOT_PG_DUMP", "pg_dump"))


@pytest.fixture(scope="module")
def pr3_dsn() -> Iterator[str]:
    name = f"opspilot_244_pr3_{uuid4().hex[:12]}"
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                sql.Identifier(name)
            )
        )
    dsn = make_conninfo(DSN, dbname=name)
    schema.migrate(dsn, pg_dump=os.environ.get("OPSPILOT_PG_DUMP", "pg_dump"))
    try:
        yield dsn
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(name)
                )
            )


@pytest.fixture
def web_app(pr3_dsn: str):
    store = DurableStore(pr3_dsn)
    store.install()
    events = DurableEventLog(store)
    events.install()
    evidence = DurableEvidenceStore(store)
    evidence.install()
    ledger = DurableWebLedger(store)
    ledger.install()
    workbench = Workbench(
        incidents=DurableIncidentStore(store),
        events=events,
        evidence=evidence,
        ledger=ledger,
        run_versions={"state": "v1"},
        run_seconds=600,
    )
    app = create_app(
        workbench,
        Authenticator(
            AuthConfig(
                ui_users={UI_USER: hash_password(UI_PASSWORD)},
                event_tokens={},
                auth_revision="244-pr3-acceptance",
                allowed_origins=frozenset({ORIGIN}),
            )
        ),
        DurableClock(store),
    )
    try:
        yield app
    finally:
        store.close()


def _clean(dsn: str, table: str, column: str, key: object) -> None:
    """Perform the product-shaped present -> cleaned update as retention."""
    key_column = {
        "opspilot_evidence": "evidence_id",
        "opspilot_alert_deliveries": "delivery_key",
        "opspilot_observation_signal_readings": "sample_id",
        "opspilot_release_query_results": "sample_id",
        "opspilot_release_comparison_queries": "comparison_id",
    }[table]
    with login(dsn, "opspilot_retention") as retention_dsn:
        with psycopg.connect(retention_dsn) as conn:
            conn.execute(
                sql.SQL(
                    "UPDATE {} SET {}=NULL,payload_state='cleaned' WHERE {}=%s"
                ).format(
                    sql.Identifier(table),
                    sql.Identifier(column),
                    sql.Identifier(key_column),
                ),
                (key,),
            )


def _seed_run_evidence(dsn: str) -> tuple[UUID, UUID, str, dict[str, object]]:
    store = DurableStore(dsn)
    incident, run = uuid4(), uuid4()
    store.accept(
        incident,
        run,
        f"pr3-{incident}",
        deadline=datetime.now(timezone.utc) + timedelta(hours=1),
        budget_limit=10,
        versions={"state": "v1"},
    )
    store.close()
    evidence_id = f"e-{uuid4()}"
    raw = b'{"series":[1,2]}'
    view = {
        "tool": "metrics_range_query",
        "status": "ok",
        "adopted": True,
        "window": {
            "start": "2026-10-27T00:00:00+00:00",
            "end": "2026-10-27T00:01:00+00:00",
        },
        "query": {"step_seconds": 30},
        "content": [
            {
                "metric": {"service": "checkout"},
                "values": [[1793059200, "1"], [1793059230, "2"]],
            }
        ],
    }
    view_sha = hashlib.sha256(
        json.dumps(view, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "INSERT INTO opspilot_evidence(evidence_id,run_id,subject_id,status,adopted,raw,raw_sha256,view,view_sha256,projection_revision,observed_at) VALUES(%s,%s,%s,'ok',true,%s,%s,%s::jsonb,%s,'pr3',clock_timestamp())",
            (
                evidence_id,
                str(run),
                str(incident),
                raw,
                hashlib.sha256(raw).hexdigest(),
                json.dumps(view),
                view_sha,
            ),
        )
    return incident, run, evidence_id, view


def _seed_alert(dsn: str) -> tuple[UUID, str]:
    incident, delivery_key = uuid4(), f"pr3-alert-{uuid4()}"
    alert = json.dumps(
        {
            "status": "firing",
            "labels": {"alertname": "CheckoutDown"},
            "annotations": {"summary": "checkout"},
        },
        separators=(",", ":"),
    )
    digest = hashlib.sha256(alert.encode()).hexdigest()
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "INSERT INTO opspilot_incidents(incident_id,intake_key,state,lifecycle,current_run_id,target_id,priority) VALUES(%s,%s,'waiting_human','open',NULL,NULL,2)",
            (incident, f"{delivery_key}:incident"),
        )
        conn.execute(
            "INSERT INTO opspilot_alert_identities(delivery_key,fingerprint,starts_at,starts_at_raw,incident_id,run_id,target_id,namespace,workload,handoff_reason) VALUES(%s,'fp-pr3',clock_timestamp(),%s,%s,NULL,NULL,NULL,NULL,'TARGET_UNRESOLVED')",
            (delivery_key, "2026-10-27T00:00:00Z", incident),
        )
        conn.execute(
            "INSERT INTO opspilot_alert_deliveries(delivery_key,fingerprint,starts_at,starts_at_raw,status,outcome,incident_id,actor,raw_sha256,annotations_sha256,annotation_revision,alert_json,truncated) VALUES(%s,'fp-pr3',clock_timestamp(),%s,'firing','handoff_created',%s,'acceptance',%s,%s,1,%s,false)",
            (delivery_key, "2026-10-27T00:00:00Z", incident, digest, digest, alert),
        )
    return incident, delivery_key


def test_pr3_cleaned_evidence_api_page_report_and_chart_remain_observable(
    pr3_dsn: str, web_app
) -> None:
    """PR-3 AC1: cleaned evidence remains addressable and its view remains usable."""
    incident, _, evidence_id, view = _seed_run_evidence(pr3_dsn)
    _clean(pr3_dsn, "opspilot_evidence", "raw", evidence_id)

    response = call(
        web_app,
        "GET",
        f"/incidents/{incident}/evidence/{evidence_id}",
        headers={**basic(), "accept": "application/json"},
    )
    assert response.status == 200, response.text
    body = response.json()
    assert body["evidence_id"] == evidence_id
    assert body["payload_state"] == "cleaned"
    assert body["raw_unavailable"] == "CLEANED_DATA"
    # The API shows raw bytes as raw_utf8/raw_base64; neither may remain.
    assert "raw_utf8" not in body and "raw_base64" not in body
    assert body["view"] == view
    assert body["view_sha256"]

    page = call(
        web_app, "GET", f"/incidents/{incident}/evidence/{evidence_id}", headers=basic()
    )
    assert page.status == 200, page.text
    assert "cleaned" in page.text.lower()
    assert "checkout" in page.text
    report_page = call(web_app, "GET", f"/incidents/{incident}", headers=basic())
    assert report_page.status == 200, report_page.text

    stored = StoredEvidence(
        evidence_id=evidence_id,
        run_id="run",
        subject_id=str(incident),
        status="ok",
        adopted=True,
        raw=None,
        raw_sha256="a" * 64,
        view=view,
        view_sha256=canonical_hash(view),
        projection_revision="pr3",
        observed_at=datetime.now(timezone.utc),
        payload_state="cleaned",
    )
    chart = evidence_chart(stored, ("claim-1",))
    assert chart is not None and chart.kind == "figure"


def test_pr3_cleaned_alert_page_is_explicitly_unavailable_not_malformed(
    pr3_dsn: str, web_app
) -> None:
    """PR-3 AC2: a cleaned alert is shown as cleaned, never as bad JSON."""
    incident, delivery_key = _seed_alert(pr3_dsn)
    _clean(pr3_dsn, "opspilot_alert_deliveries", "alert_json", delivery_key)
    response = call(web_app, "GET", f"/incidents/{incident}", headers=basic())
    assert response.status == 200, response.text
    lowered = response.text.lower()
    assert "cleaned" in lowered
    assert "malformed" not in lowered and "format" not in lowered


def test_pr3_f6_cleaned_replay_is_unknown_with_a_cleaned_reason(
    scratch_dsn: str, owner, controller, loop
) -> None:
    """PR-3 AC3: cleaned observation data is unknown with its own reason."""
    incident, _, target = _incident(owner)
    session = _authorize(controller, incident, target, sustained=1)
    _backdate_authorization(owner, session)
    first, _ = _run(loop, owner, session, 2)

    _clean(scratch_dsn, "opspilot_observation_signal_readings", "raw", first.sample_id)
    cleaned = replay_stored_session(controller, session)
    assert cleaned.recovery_verdict == "unknown"
    assert "STORED_OBSERVATION_DATA_CLEANED" in cleaned.integrity
    assert "STORED_OBSERVATION_INTEGRITY_MISMATCH" not in cleaned.integrity


def test_pr3_f6_tampered_raw_still_reports_integrity_mismatch(
    owner, controller, loop
) -> None:
    """PR-3 AC3 regression guard: raw/hash tampering remains a mismatch."""
    incident2, _, target2 = _incident(owner)
    session2 = _authorize(controller, incident2, target2, sustained=1)
    _backdate_authorization(owner, session2)
    tampered_first, _ = _run(loop, owner, session2, 2)
    with owner.transaction() as conn:
        signal = conn.execute(
            "SELECT signal_name FROM opspilot_observation_signal_readings WHERE sample_id=%s ORDER BY signal_name LIMIT 1",
            (tampered_first.sample_id,),
        ).fetchone()["signal_name"]
        raw = conn.execute(
            "SELECT raw FROM opspilot_observation_signal_readings WHERE sample_id=%s ORDER BY signal_name LIMIT 1",
            (tampered_first.sample_id,),
        ).fetchone()["raw"]
        conn.execute(
            "UPDATE opspilot_observation_signal_readings SET raw=%s WHERE sample_id=%s AND signal_name=%s",
            (b"tampered", tampered_first.sample_id, signal),
        )
    tampered = replay_stored_session(controller, session2)
    assert tampered.recovery_verdict == "unknown"
    assert INTEGRITY_MISMATCH in tampered.integrity
    assert all("DATA_CLEANED" not in item for item in tampered.integrity)
    with owner.transaction() as conn:
        conn.execute(
            "UPDATE opspilot_observation_signal_readings SET raw=%s WHERE sample_id=%s AND signal_name=%s",
            (bytes(raw), tampered_first.sample_id, signal),
        )


def test_pr3_f6_historical_absent_row_keeps_absent_state(scratch_dsn: str) -> None:
    """PR-3 AC3 regression guard: legacy raw=NULL rows remain absent."""
    graph = _seed_observer_graph(scratch_dsn, raw=None)
    with psycopg.connect(scratch_dsn) as conn:
        row = conn.execute(
            "SELECT payload_state FROM opspilot_observation_signal_readings WHERE sample_id=%s",
            (graph["sample"],),
        ).fetchone()
    assert row is not None and row[0] == "absent"


def test_pr3_f6_absent_replay_keeps_the_legacy_unknown_path() -> None:
    """PR-3 AC3 regression guard: absent is not relabeled as cleaned."""
    history = healthy_history(count=1)
    reading = history["samples"][0]["readings"][0]
    reading.update(raw=None, raw_sha256=None, payload_state="absent")
    replay = replay_history(history)
    assert replay.recovery_verdict == "unknown"
    assert all("DATA_CLEANED" not in item for item in replay.integrity)


def test_pr3_cleaned_release_and_comparison_replay_preserve_query_metadata(
    release_harness,
) -> None:
    """PR-3 AC4: release and before-window comparison replay report cleaned."""
    web, observer, oid, _, _ = release_harness
    context = _anomalous(release_harness)
    source = BeforeTelemetry(context)
    from opspilot.observer.loop import ObserverLoop

    ObserverLoop(store=observer, source=source, batch=1).poll_once()
    (comparison,) = _comparisons(observer, oid)
    release_history_before = observer.release_history(oid)
    query = release_history_before["samples"][0]["queries"][0]
    query_key = query["query_key"]
    comparison_query_key = comparison["queries"][0]["query_key"]

    with login(DSN, "opspilot_retention") as retention_dsn:
        with psycopg.connect(retention_dsn) as conn:
            conn.execute(
                "UPDATE opspilot_release_query_results SET raw=NULL,payload_state='cleaned' WHERE sample_id=%s AND query_key=%s",
                (query["sample_id"], query_key),
            )
            conn.execute(
                "UPDATE opspilot_release_comparison_queries SET raw=NULL,payload_state='cleaned' WHERE comparison_id=%s AND query_key=%s",
                (comparison["comparison_id"], comparison_query_key),
            )

    release_session_id = release_history_before["sessions"][0]["session_id"]
    replay = observer.replay_session(release_session_id)
    assert replay.verdict == "unknown"
    assert "REPLAY_DATA_CLEANED" in replay.integrity
    release_history_after = observer.release_history(oid)
    assert release_history_after["samples"][0]["queries"][0]["query_key"] == query_key

    comparison_replay = replay_release_history(_history(observer, oid))
    assert comparison_replay.verdict == "unknown"
    assert "REPLAY_DATA_CLEANED" in comparison_replay.integrity
    assert (
        _history(observer, oid)["comparisons"][0]["queries"][0]["query_key"]
        == comparison_query_key
    )
