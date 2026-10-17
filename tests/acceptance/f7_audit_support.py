"""Chain builder for the F7 step 5 audit acceptance test (PostgreSQL).

Builds one incident from input to postmortem review through the product's
public stores and routes, on a scratch database (``f7_pg_support``):

1. UI intake (``POST /intake/ui``)                  -> input, Run created
2. a Run on the product runner, scripted model and recorded backend bytes
   (fixture transport, R4)                           -> query, evidence, report
3. ``POST /incidents/{id}/control`` ``correct``     -> human correction
4. ``POST /incidents/{id}/control`` ``register_remediation`` (a second
   workbench on the same store, with the shipped checkout HealthProfile and
   a test identity registry)                         -> observation session
5. one Observer sample through ``take_sample`` over a stub Prometheus
   source answering empty vectors, then the Observer's deadline sweep
   (deadline pulled forward in SQL, as the F6 driver nudges due times)
                                                     -> sample, ending
6. ``KnowledgeStore.record_draft`` + ``approve``    -> postmortem version,
   review, knowledge entry

and a second, shorter incident with only steps 1-2. PostgreSQL's
``clock_timestamp()`` brackets the whole build, so a test can tell PG write
time from the recorded backend's (September 2026) source timestamps.
Nothing here reads ``opspilot.acceptance_audit``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from io import BytesIO
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest

from opspilot.knowledge import ConclusionDraft, ProposalDraft, Watermark
from opspilot.observation import ObservationStore
from opspilot.observer.prometheus import PrometheusReadOnlySource
from opspilot.observer.sampler import take_sample
from opspilot.persistence import DurableStore
from opspilot.tools.otel_demo import TARGET_ID, otel_demo_face, otel_demo_versions
from opspilot.web import (
    Authenticator,
    DurableClock,
    DurableEventLog,
    DurableEvidenceStore,
    DurableIncidentStore,
    DurableWebLedger,
    Workbench,
    create_app,
)
from opspilot.web.store import MappingTargetRegistry
from tests.acceptance.f7_hostile_support import jaeger_payload, prometheus_payload
from tests.acceptance.f7_pg_support import (
    POOL,
    REVIEWER,
    UI,
    WORKER,
    ScriptedProvider,
    _auth,
    assistant,
    build_harness,
    final_report,
    fn,
)
from tests.m1_tool_support import FakeClock
from tests.m1_web_support import HEALTH_PROFILE, UI_USER, bearer, post_form, post_json
from tests.target_support import ANY_TARGETS
from tests.test_m1_otel_demo_contract import GOOD_EXPR, WINDOW_END, FakeOpener

QUESTION = "Why is checkout erroring? f7-audit-question-free-text"
#: Free text the operator types into the correction; must never reach detail.
CORRECTION_TEXT = (
    "The payment span is a red herring, look at the cart cache instead "
    "f7-audit-correction-free-text password=f7-synthetic-auditpw-QZXWKRMVTQ"
)
SYNTHETIC_SECRET = "f7-synthetic-auditpw-QZXWKRMVTQ"
REPORT_SUMMARY = "Checkout traces show an erroring payment span."
KNOWLEDGE_TEXT = "f7-audit-knowledge-free-text check the cart cache first"


@dataclass(frozen=True)
class Chain:
    incident: UUID
    first_run: UUID
    started_at: datetime
    finished_at: datetime
    executed_reads: int
    source_times: tuple[datetime, ...]
    session_id: UUID
    postmortem_id: UUID


@dataclass(frozen=True)
class ShortChain:
    incident: UUID
    run: UUID


def _empty_vector_opener():
    class _Opener:
        requests: list[Any] = []

        def open(self, request, timeout=None):
            assert request.get_method() == "GET", request.full_url
            self.requests.append(request.full_url)
            response = BytesIO(
                b'{"status":"success","data":{"resultType":"vector","result":[]}}'
            )
            response.status = 200
            return response

    return _Opener()


def _pg_now(dsn: str) -> datetime:
    with psycopg.connect(dsn) as conn:
        return conn.execute("SELECT clock_timestamp()").fetchone()[0]


def _script() -> list[Any]:
    return [
        assistant(
            tool_calls=[
                fn("read-traces", "traces_search", {"service": "checkout"}),
                fn("read-metrics", "metrics_range_query", {"expr": GOOD_EXPR}),
            ]
        ),
        final_report(TARGET_ID),
    ]


def _opener() -> FakeOpener:
    return FakeOpener(
        by_query={GOOD_EXPR: prometheus_payload()},
        routes={"/api/traces": jaeger_payload()},
    )


def _control(app, incident: UUID, generation: int, **fields: str):
    response = post_form(
        app,
        f"/incidents/{incident}/control",
        {
            "expected_generation": str(generation),
            "idempotency_key": f"f7-audit-{uuid4().hex}",
            **fields,
        },
        headers=UI,
    )
    assert response.status == 200, response.text
    return response.json()


def build_short_chain(dsn: str) -> ShortChain:
    """Input and a completed Run only: no correction, recovery or postmortem."""
    patch = pytest.MonkeyPatch()
    opener = _opener()
    harness = build_harness(dsn, patch, opener)
    try:
        incident, run = harness.submit(
            "Short chain: is checkout slow?", target_id=TARGET_ID
        )
        harness.run(incident, ScriptedProvider(_script()))
        return ShortChain(incident, run)
    finally:
        for closer in harness.closers:
            closer.close()
        patch.undo()


@dataclass(frozen=True)
class AlertIncident:
    incident: UUID
    starts_at: datetime
    received_after: datetime


#: Registry key the alert below matches (labels), as in the M1-04 tests.
_ALERT_TARGET = "f7-audit-alert-target"
_ALERT_REGISTRY = {
    _ALERT_TARGET: {
        "integration_id": "f7-audit",
        "cluster_uid": "opspilot-f7",
        "namespace": "otel-demo",
        "workload": "checkout",
        "health_profile_id": "otel-demo-checkout",
        "match": {
            "version": 1,
            "labels": {"namespace": "otel-demo", "service": "checkout"},
        },
    },
}


def build_alert_incident(dsn: str) -> AlertIncident:
    """One Alertmanager delivery (``POST /intake/alertmanager``) whose
    ``startsAt`` -- a source-supplied time -- lies an hour before the PG
    write; no Run is executed."""
    store = DurableStore(dsn, pool=POOL)
    store.install()
    try:
        clock = DurableClock(store)
        workbench = Workbench(
            incidents=DurableIncidentStore(store),
            events=DurableEventLog(store),
            evidence=DurableEvidenceStore(store),
            ledger=DurableWebLedger(store),
            run_versions=otel_demo_versions(),
            run_seconds=600,
            tool_face=otel_demo_face(clock),
            targets=MappingTargetRegistry(_ALERT_REGISTRY),
        )
        app = create_app(workbench, Authenticator(_auth()), clock)
        before = _pg_now(dsn)
        starts_at = before - timedelta(hours=1)
        payload = {
            "receiver": "opspilot",
            "status": "firing",
            "alerts": [
                {
                    "status": "firing",
                    "labels": {
                        "alertname": "CheckoutPlaceOrderErrorRatioHigh",
                        "namespace": "otel-demo",
                        "service": "checkout",
                        "severity": "critical",
                    },
                    "annotations": {
                        "summary": "checkout errors f7-audit-alert-free-text"
                    },
                    "startsAt": starts_at.astimezone(timezone.utc).isoformat(),
                    "endsAt": "0001-01-01T00:00:00Z",
                    "fingerprint": uuid4().hex[:16],
                    "generatorURL": "http://prometheus:9090/graph?g0.expr=up",
                }
            ],
            "groupLabels": {},
            "commonLabels": {},
            "commonAnnotations": {},
            "externalURL": "http://alertmanager:9093",
            "version": "4",
            "groupKey": "{}:{}",
            "truncatedAlerts": 0,
        }
        response = post_json(app, "/intake/alertmanager", payload, headers=bearer())
        assert response.status == 200, response.text
        [result] = response.json()["results"]
        assert result.get("incident_id"), result
        return AlertIncident(UUID(result["incident_id"]), starts_at, before)
    finally:
        store.close()


def build_chain(dsn: str) -> Chain:
    patch = pytest.MonkeyPatch()
    opener = _opener()
    harness = build_harness(dsn, patch, opener)
    observer = ObservationStore(dsn, pool=POOL)
    observer.install()
    started = _pg_now(dsn)
    try:
        # 1-2: input, Run, queries, evidence, report.
        incident, first_run = harness.submit(QUESTION, target_id=TARGET_ID)
        harness.run(incident, ScriptedProvider(_script()))
        executed = len(opener.requests)
        assert executed >= 1, "the Run executed no read"
        with psycopg.connect(dsn) as conn:
            source_times = tuple(
                row[0]
                for row in conn.execute(
                    "SELECT observed_at FROM opspilot_evidence WHERE run_id=%s "
                    "AND observed_at IS NOT NULL",
                    (str(first_run),),
                ).fetchall()
            )

        # 3: human correction of the published conclusion.
        generation = harness.store.control_state(incident)["incident_generation"]
        corrected = _control(
            harness.app, incident, generation, action="correct", text=CORRECTION_TEXT
        )

        # 4: register the remediation (second workbench: profile + identity).
        registrar = Workbench(
            incidents=DurableIncidentStore(harness.store),
            events=harness.events,
            evidence=harness.evidence,
            ledger=DurableWebLedger(harness.store),
            run_versions=harness.profile.versions(),
            run_seconds=600,
            tool_face=otel_demo_face(FakeClock(start=WINDOW_END)),
            health_profile=HEALTH_PROFILE,
            targets=ANY_TARGETS,
        )
        registrar_app = create_app(
            registrar, Authenticator(_auth()), DurableClock(harness.store)
        )
        _control(
            registrar_app,
            incident,
            corrected["generation"],
            action="register_remediation",
            revision="checkout:f7-audit",
        )
        sessions = observer.incident_sessions(incident)
        assert len(sessions) == 1, sessions
        session_id = sessions[0]["session_id"]

        # 5: one Observer sample, then the Observer's deadline sweep.
        with psycopg.connect(dsn) as conn:
            conn.execute(
                "UPDATE opspilot_observation_sessions SET "
                "active_sample_due_at=clock_timestamp() WHERE session_id=%s",
                (session_id,),
            )
        leases = [
            lease
            for lease in observer.claim_due_samples(uuid4(), limit=20)
            if getattr(lease, "incident_id", None) == incident
        ]
        assert len(leases) == 1, leases
        source = PrometheusReadOnlySource(
            "http://f7-audit.invalid", opener=_empty_vector_opener()
        )
        take_sample(leases[0], HEALTH_PROFILE, source, observer)
        with psycopg.connect(dsn) as conn:
            conn.execute(
                "UPDATE opspilot_observation_sessions SET "
                "deadline_at=clock_timestamp() WHERE session_id=%s "
                "AND state='authorized'",
                (session_id,),
            )
        observer.sweep_expired_sessions(limit=20)
        with psycopg.connect(dsn) as conn:
            ending = conn.execute(
                "SELECT ending_id FROM opspilot_observation_endings "
                "WHERE session_id=%s",
                (session_id,),
            ).fetchone()
            assert ending is not None, "the session did not end"
            current = conn.execute(
                "SELECT i.control_generation, i.current_run_id, "
                "i.observation_generation, "
                "(SELECT count(*) FROM opspilot_runs r "
                " WHERE r.incident_id=i.incident_id), "
                "(SELECT COALESCE(max(n.sequence), 0) FROM opspilot_inputs n "
                " WHERE n.incident_id=i.incident_id) "
                "FROM opspilot_incidents i WHERE i.incident_id=%s",
                (incident,),
            ).fetchone()

        # 6: postmortem draft and human review.
        watermark = Watermark(
            incident_control_generation=current[0],
            observation_generation=current[2],
            observation_session_id=session_id,
            observation_ending_id=ending[0],
            run_count=current[3],
            last_run_id=current[1],
            input_watermark=current[4],
            evidence_snapshot_sha256=harness.knowledge.evidence_snapshot_sha256(
                incident
            ),
        )
        draft = harness.knowledge.record_draft(
            incident,
            expected_generation=0,
            idempotency_key=f"f7-audit-{uuid4().hex}",
            actor=WORKER,
            content={"impact": "checkout degraded f7-audit-postmortem-free-text"},
            watermark=watermark,
            conclusions=[
                ConclusionDraft(
                    key="timeline",
                    section="timeline",
                    body="alert received; Run started; f7-audit-conclusion-free-text",
                    author="code",
                    certainty="deterministic",
                    citations_valid=True,
                )
            ],
            proposals=[
                ProposalDraft(
                    key="kb-1",
                    name="checkout cart cache",
                    tags=["checkout"],
                    content={"checks": [KNOWLEDGE_TEXT], "evidence_refs": ["ev-1"]},
                )
            ],
        )
        harness.knowledge.approve(
            draft.object_id,
            draft.version,
            expected_generation=draft.generation,
            idempotency_key=f"f7-audit-{uuid4().hex}",
            actor=REVIEWER,
        )
        finished = _pg_now(dsn)
        return Chain(
            incident=incident,
            first_run=first_run,
            started_at=started,
            finished_at=finished,
            executed_reads=executed,
            source_times=source_times,
            session_id=session_id,
            postmortem_id=draft.object_id,
        )
    finally:
        observer.close()
        for closer in harness.closers:
            closer.close()
        patch.undo()


def free_text_markers() -> tuple[str, ...]:
    return (
        "f7-audit-question-free-text",
        "f7-audit-correction-free-text",
        "f7-audit-postmortem-free-text",
        "f7-audit-conclusion-free-text",
        "f7-audit-knowledge-free-text",
        "red herring",
        "f7-audit-alert-free-text",
        SYNTHETIC_SECRET,
        "f7-synthetic-",
        REPORT_SUMMARY,
    )


def detail_text(detail: Any) -> str:
    return json.dumps(detail, default=str, ensure_ascii=False, sort_keys=True)


__all__ = [
    "CORRECTION_TEXT",
    "AlertIncident",
    "Chain",
    "ShortChain",
    "UI_USER",
    "build_alert_incident",
    "build_chain",
    "build_short_chain",
    "detail_text",
    "free_text_markers",
]
