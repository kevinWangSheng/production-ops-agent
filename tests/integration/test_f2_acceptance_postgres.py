"""F2 Durable Incident Runtime: the five ``feature_list.json`` steps on real PostgreSQL.

Fresh-context acceptance tests (AGENTS.md 独立审查 / 验证与汇报), written by an
Agent that implemented none of F2, from:

* ``feature_list.json`` F2 steps 1-5;
* the PR-E pre-review ``~/.agent/handoffs/opspilot-prereview-f2-pre-2026-10-24.md``
  section 6 (deterministic behaviour list) and E-R8 (this file's shape);
* the F2 contract ``docs/tasks/2026-10-17-f2-contract.md`` (U1-U9) and the
  sub-contracts it names (PR-C C1-C3/R-C4, #343 U1-U3, #305 W1-B/W2-A, B1/B2);
* PRODUCT-CONSTRAINTS (read-only product, human control outranks the model).

Every scenario is one ``IncidentScenario(feature_id="F2", acceptance_step="1".."5")``
projected with the public seams ``outcome_from_durable`` (``opspilot.acceptance``)
or ``alert_intake_outcome`` (``opspilot.acceptance_alert``). Stimuli go through
public entries only: ``POST /intake/alertmanager``, ``POST /intake/version-changes``,
``POST /incidents/{id}/control``, ``Workbench.release_anomaly_pass``,
``WorkerLoop.poll_once`` / ``InvestigationRunner.resume`` (the worker's own
composition), the real ``python -m opspilot.worker_main`` child (step 4) and the
incident page. Models are scripted (or the local B1 HTTPS stub for the child
worker); tools are the product fixture profile or the real otel-demo transport
against a local stalling HTTP source. No model HTTP, no kind, no network.

Nothing asserts chain of thought or internal call order. Rows are touched by
hand in exactly two places, both test-clock moves already used by the existing
suites: ``neutralize_claimable`` (other tests' leftovers become overdue before the
child worker starts) and a lease that is waited out, never rewritten.

Test-support modules are imported, never modified; no existing assertion is
changed.

Assumptions where the contract leaves a name open (also marked ``ASSUMPTION``):

* A1 a replayed Alertmanager delivery answers ``outcome="replayed"`` (M1-04 I3).
* A2 an incident-level pause/cancel of a Run whose model reply is still in
  flight leaves the reply as a ``late_result`` step at the older control
  generation; ``outcome_from_durable`` then shows ``late_result_rejected`` and
  ``STALE_CONTROL_GENERATION`` (pre-review section 6 step 5 item 4).
* A3 a parked backlog Run is announced by one ``run_handoff`` event with
  ``reasons == ["BACKLOG_FULL"]``, ``parked is True`` and
  ``execution == "not_started"`` (U-B2, pre-review step 4 item 4).
* A4 #305 (W1-B): the successor Run's ``time_policies[0]`` keeps the J3 keys
  ``window``/``anchor``/``default_query_window`` with ISO-8601 instants; its
  frame end is the creation instant, read here as the Run's ``deadline`` minus
  the workbench ``run_seconds`` (the #305 contract section 3 item 2 reading).
* A5 the per-Run model request cap is ``MAX_MODEL_REQUESTS_PER_RUN`` (100) and
  a Run that spends its whole budget on tool rounds ends ``budget_exhausted``.
* A6 the stalled read in step 3 is chosen by the ``step`` query parameter
  (``step_seconds=45``) the otel-demo transport sends; any other read answers.

#305 (PR #346) is not merged on ``main`` at 2cc2fcd; the one test that depends
on it (``test_step1_successor_run_after_attached_episode_reframes_on_new_episode``)
is expected red there and is not weakened.
"""

# ruff: noqa: F811 - pytest fixtures imported from sibling acceptance modules
from __future__ import annotations

import json
import os
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

import opspilot.tools.fixture as fixture_module
from opspilot.acceptance import (
    IncidentScenario,
    alert_intake_outcome,
    alert_intake_records,
    outcome_from_durable,
)
from opspilot.investigation.limits import MAX_MODEL_REQUESTS_PER_RUN
from opspilot.investigation.progress import sweep_expired
from opspilot.investigation.runner import InvestigationRunner
from opspilot.observation import ObservationStore
from opspilot.observation.releases import ReleaseStore
from opspilot.persistence import DurableStore
from opspilot.tools import TransportResponse
from opspilot.tools.executor import TransportRequest
from opspilot.tools.fixture import (
    FIXTURE_TOOL,
    fixture_executor_factory,
    fixture_face,
    fixture_versions,
)
from opspilot.tools.otel_demo import (
    METRICS_TOOL,
    TARGET_ID,
    OtelDemoConfig,
    otel_demo_executor_factory,
    otel_demo_face,
    otel_demo_versions,
)
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
    token_digest,
)
from opspilot.web.store import MappingTargetRegistry
from opspilot.worker import Worker
from scripts.m0.postgres_lab import DSN
from tests.acceptance.test_343_alert_after_release_incident import (
    CONTROL_COLUMNS,
    RELEASE_PRIORITY,
    identities,
    new_alert,
    release_first,
    scene,
)
from tests.acceptance.test_f2_alert_correlation import (  # noqa: F401 - fixtures
    EVENT_KEYS,
    REQUIRED_EVENT_KEYS,
    T1,
    T2,
    T3,
    alert,
    database,
    events,
    fire,
    harness,
    incident_row,
    make_app,
    resolve,
    run_count,
)
from tests.acceptance.test_f2_alert_correlation import control as correlation_control
from tests.integration.f2_b1_support import (
    Act,
    ModelStub,
    WorkerProcess,
    events_of,
    make_certificate,
    neutralize_claimable,
    new_tag,
    run_rows,
    wait_until,
)
from tests.integration.test_f2_b2_backpressure_postgres import _app as storm_app
from tests.integration.test_f2_b2_backpressure_postgres import _firing as storm_firing
from tests.integration.test_f11_pr3_impl_target_txn_postgres import Env as ReleaseEnv
from tests.integration.test_f11_pr4b_consume_postgres import (  # noqa: F401 - fixtures
    _by_intake,
    _events,
    _incident,
    _link,
    _ready,
    _runs,
    bench,
)
from tests.integration.test_f11_release_observation_postgres import (  # noqa: F401 - fixtures
    Catalog,
    catalog,
    observer,
    observer_dsn,
    owner,
    web,
    web_dsn,
    worker_dsn,
)
from tests.integration.test_m1_04_alert_intake_impl_postgres import (
    REGISTRY as ALERT_REGISTRY,
)
from tests.integration.test_m1_04_alert_intake_impl_postgres import _alert, _webhook
from tests.integration.test_m1_web_worker_postgres import _tool_round, _worker
from tests.m1_investigation_support import (
    ScriptedModel,
    reply,
    report_from_transcript,
    report_json,
    tool_call,
)
from tests.m1_tool_support import WINDOW_START, body
from tests.m1_web_support import (
    EVENT_TOKEN,
    ORIGIN,
    UI_PASSWORD,
    UI_USER,
    basic,
    bearer,
    call,
    post_form,
    post_json,
    same_origin,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1",
    reason="explicit PG opt-in required: M1_DURABLE_POSTGRES=1",
)

FEATURE = "F2"
#: The in-process runner's lease; a "killed" attempt keeps it to expiry.
LEASE_SECONDS = 1
RUN_SECONDS = 600
FOLLOW_UP = "F2 acceptance: re-check checkout with the newest alert episode."


# ===========================================================================
# shared helpers: scenarios and projections
# ===========================================================================


def scenario(step: str, scenario_id: str, subject: Any, kind: str = "incident"):
    return IncidentScenario(
        scenario_id=f"f2-acc-{scenario_id}",
        feature_id=FEATURE,
        acceptance_step=step,
        kind=kind,
        subject_id=str(subject),
    )


def intake_outcome(source: Any, step: str, scenario_id: str, incident_id: Any):
    """``AlertIntakeOutcome`` of one alert incident, from a DSN or a store."""
    if isinstance(source, str):
        with psycopg.connect(source) as conn:
            conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
            records = alert_intake_records(conn, incident_id)
    else:
        records = alert_intake_records(source, incident_id)
    return alert_intake_outcome(
        scenario(step, scenario_id, incident_id, kind="alert-intake"), records
    )


def durable_outcome(store: DurableStore, events_log: Any, step: str, sid: str, subject):
    snapshot = store.rebuild(subject)
    controls = tuple(
        asdict(row) for row in DurableIncidentStore(store).control_audit(subject)
    )
    handoffs = ()
    if events_log is not None:
        handoffs = tuple(
            dict(event.payload)
            for event in events_log.read_after(subject, 0, limit=1000)
            if event.kind == "run_handoff"
        )
    outcome = outcome_from_durable(
        scenario(step, sid, subject),
        snapshot,
        controls=controls,
        handoff_events=handoffs,
    )
    return outcome, snapshot


def late_rows(snapshot) -> list[dict[str, Any]]:
    return [s for s in snapshot["steps"] if s["status"] == "late_result"]


def adopted_evidence(snapshot) -> list[str]:
    found: list[str] = []
    for step in snapshot["steps"]:
        if step["status"] == "late_result":
            continue
        for item in step["tool_results"] or ():
            evidence_id = item["result"].get("evidence_id")
            if isinstance(evidence_id, str) and evidence_id not in found:
                found.append(evidence_id)
    return found


def assert_read_only(outcome) -> None:
    # PRODUCT-CONSTRAINTS: the product never writes to the investigated system.
    assert set(outcome.permissions) <= {"read_only", "human_control"}
    assert set(outcome.actions) <= {
        "read_only_query",
        "late_result_rejected",
        "worker_resumed",
    }


def db_now(dsn: str = DSN) -> datetime:
    with psycopg.connect(dsn) as conn:
        return conn.execute("SELECT clock_timestamp()").fetchone()[0]


def zulu(moment: datetime) -> str:
    return (
        moment.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def parse(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


# ===========================================================================
# the lab workbench (fixture tool profile, Alertmanager intake) + its worker
# ===========================================================================


def lab(**workbench_extra: Any) -> SimpleNamespace:
    """Workbench + app as the product composes them, on the lab database."""
    store = DurableStore(DSN)
    store.install()
    log = DurableEventLog(store)
    log.install()
    evidence = DurableEvidenceStore(store)
    evidence.install()
    ledger = DurableWebLedger(store)
    ledger.install()
    workbench = Workbench(
        incidents=DurableIncidentStore(store),
        events=log,
        evidence=evidence,
        ledger=ledger,
        run_versions=fixture_versions(),
        run_seconds=RUN_SECONDS,
        tool_face=fixture_face(),
        targets=MappingTargetRegistry(ALERT_REGISTRY),
        **workbench_extra,
    )
    config = AuthConfig(
        ui_users={UI_USER: hash_password(UI_PASSWORD)},
        event_tokens={token_digest(EVENT_TOKEN): "alertmanager"},
        auth_revision="f2-acceptance-pg",
        allowed_origins=frozenset({ORIGIN}),
    )
    app = create_app(workbench, Authenticator(config), DurableClock(store))
    return SimpleNamespace(
        app=app, workbench=workbench, store=store, events=log, evidence=evidence
    )


def fire_alert(env, fingerprint: str, starts_at: str, **labels: str) -> dict:
    alert_body = _alert(fingerprint, startsAt=starts_at)
    if labels:
        alert_body["labels"] = {**alert_body["labels"], **labels}
    response = post_json(
        env.app, "/intake/alertmanager", _webhook(alert_body), headers=bearer()
    )
    assert response.status == 200, response.text
    (item,) = response.json()["results"]
    return item


def act(env, subject, action: str, text: str | None = None):
    """``POST /incidents/{id}/control`` at the incident's current generation."""
    generation = env.store.rebuild(subject)["control_generation"]
    fields = {
        "action": action,
        "expected_generation": str(generation),
        "idempotency_key": f"{action}-{uuid4()}",
    }
    if text is not None:
        fields["text"] = text
    return post_form(
        env.app,
        f"/incidents/{subject}/control",
        fields,
        headers={**basic(), **same_origin(), "accept": "application/json"},
    )


def poll(env, subject, replies):
    loop, model = _worker(env.store, env.events, env.evidence, replies, subject=subject)
    return [(i, o) for i, o in loop.poll_once()], model


def alert_events(store: DurableStore, subject) -> list[dict[str, Any]]:
    return [
        row
        for row in store.read_inputs(UUID(str(subject)))
        if row["kind"] == "event" and row["actor"] == "alertmanager"
    ]


def bad_report(_call):
    return reply(content="not a report")


def runner(env, replies, *, parallelism: int = 2, factory=None, versions=None):
    clock = DurableClock(env.store)
    return InvestigationRunner(
        store=env.store,
        worker=Worker.create(env.store, dict(versions or fixture_versions())),
        model=ScriptedModel(replies),
        executor_factory=factory
        or fixture_executor_factory(env.store, evidence=env.evidence, clock=clock),
        clock=clock,
        lease_seconds=LEASE_SECONDS,
        events=env.events,
        evidence=env.evidence,
        tool_parallelism=parallelism,
    )


def alert_incident(env, tag: str) -> tuple[UUID, UUID]:
    starts = zulu(db_now() - timedelta(minutes=5))
    item = fire_alert(env, f"f2acc{tag}{uuid4().hex[:8]}", starts)
    assert item["outcome"] == "created" and item["run_id"], item
    return UUID(item["incident_id"]), UUID(item["run_id"])


# ===========================================================================
# STEP 1: duplicate alerts and deployment events; correlation policy joins
#         related signals and keeps distinct concurrent incidents apart
# ===========================================================================


def test_step1_duplicate_alert_delivery_is_replayed_into_the_same_incident(
    harness, database, alert
):
    app = harness()
    first = fire(app, alert, T1)
    again = fire(app, alert, T1)

    assert first["outcome"] == "created" and first["run_id"]
    assert again["outcome"] == "replayed"  # ASSUMPTION A1
    assert again["incident_id"] == first["incident_id"]
    outcome = intake_outcome(database, "1", "duplicate-replayed", first["incident_id"])
    assert outcome.run_ids == (first["run_id"],)
    assert [d.outcome for d in outcome.deliveries] == ["created", "replayed"]
    assert events(database, first["incident_id"]) == []
    assert outcome.related_incident_id is None
    assert len(identities_on(database, alert["fingerprint"])) == 1


def identities_on(dsn: str, fingerprint: str) -> list[dict[str, Any]]:
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        return conn.execute(
            "SELECT incident_id FROM opspilot_alert_identities WHERE fingerprint=%s",
            (fingerprint,),
        ).fetchall()


def test_step1_refired_episode_joins_the_open_incident_without_a_new_run(
    harness, database, alert
):
    app = harness()
    first = fire(app, alert, T1)
    before = incident_row(database, first["incident_id"])

    refired = fire(app, alert, T2)
    repeat = fire(app, alert, T2)
    resolved = resolve(app, alert, T2)

    for item in (refired, repeat):
        assert item["outcome"] == "attached", item
        assert item["incident_id"] == first["incident_id"]
        assert item["run_id"] is None
    assert resolved["outcome"] == "resolved_attached"
    assert resolved["incident_id"] == first["incident_id"]
    # no new Run, no state/control change, no new identity row (C1-A, U1-A)
    assert run_count(database, first["incident_id"]) == 1
    assert incident_row(database, first["incident_id"]) == before
    assert len(identities_on(database, alert["fingerprint"])) == 1
    (event,) = events(database, first["incident_id"])  # exactly one (C2-A)
    assert event["kind"] == "event" and event["actor"] == "alertmanager"
    assert set(event["content"]) <= EVENT_KEYS, event["content"]
    assert REQUIRED_EVENT_KEYS <= set(event["content"])
    assert event["content"]["fingerprint"] == alert["fingerprint"]
    assert event["content"]["starts_at"] == T2
    outcome = intake_outcome(database, "1", "refire-attached", first["incident_id"])
    assert [d.outcome for d in outcome.deliveries] == [
        "created",
        "attached",
        "attached",
        "resolved_attached",
    ]
    assert outcome.run_ids == (first["run_id"],)
    assert outcome.lifecycle == "open"


def test_step1_after_close_the_same_fingerprint_opens_a_related_incident(
    harness, database, alert
):
    app = harness()
    a = fire(app, alert, T1)
    correlation_control(app, a["incident_id"], "close")
    b = fire(app, alert, T2)

    assert b["outcome"] == "created" and b["incident_id"] != a["incident_id"]
    assert b["run_id"] and b["run_id"] != a["run_id"]
    closed = intake_outcome(database, "1", "closed-old", a["incident_id"])
    new = intake_outcome(database, "1", "closed-related", b["incident_id"])
    assert new.related_incident_id == a["incident_id"]  # C3
    assert closed.lifecycle == "closed"
    assert [d.outcome for d in closed.deliveries] == ["created"]  # not reopened
    assert new.run_ids == (b["run_id"],)


def test_step1_distinct_concurrent_alerts_on_one_target_stay_distinct_incidents(
    harness, database, alert
):
    app = harness()
    x = {**alert, "fingerprint": uuid4().hex[:16]}
    y = {**alert, "fingerprint": uuid4().hex[:16]}
    with ThreadPoolExecutor(max_workers=2) as pool:
        items = list(pool.map(lambda a: fire(app, a, T1), (x, y)))

    assert [i["outcome"] for i in items] == ["created", "created"], items
    assert items[0]["incident_id"] != items[1]["incident_id"]
    for item in items:
        outcome = intake_outcome(
            database, "1", "concurrent-distinct", item["incident_id"]
        )
        assert outcome.run_ids == (item["run_id"],)
        assert outcome.related_incident_id is None
        assert events(database, item["incident_id"]) == []


def test_step1_concurrent_first_deliveries_of_one_alert_share_one_incident(
    harness, database, alert
):
    app = harness()
    with ThreadPoolExecutor(max_workers=2) as pool:
        items = list(pool.map(lambda _: fire(app, alert, T1), range(2)))

    assert {i["incident_id"] for i in items} == {items[0]["incident_id"]}
    assert sorted(i["outcome"] for i in items) == ["created", "replayed"]
    assert run_count(database, items[0]["incident_id"]) == 1


def test_step1_release_anomaly_attaches_to_the_open_alert_incident_on_its_target(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
    bench,
):
    """U2-A / F11 U8: alert first, then the deployment event joins it."""
    case = _ready(owner, web, observer, catalog, bench)
    current = scene(owner, bench, case)
    first = fire(current.app, new_alert("warning"), T1)
    assert first["outcome"] == "created", first
    existing = UUID(first["incident_id"])
    before = _incident(existing)
    runs_before = _runs(existing)

    current.workbench.release_anomaly_pass()
    current.workbench.release_anomaly_pass()  # a repeated pass adds nothing

    link = _link(case.oid)
    assert link["disposition"] == "attached" and link["incident_id"] == existing
    assert link["run_id"] is None
    assert _by_intake(case.intake_key) == []  # no second investigation
    assert _runs(existing) == runs_before
    after = _incident(existing)
    for column in (
        "state",
        "lifecycle",
        "mode",
        "control_generation",
        "current_run_id",
    ):
        assert after[column] == before[column], column
    release_events = [
        e for e in _events(existing) if e["actor"] == "release_observation"
    ]
    assert len(release_events) == 1
    assert link["input_id"] == release_events[0]["input_id"]
    outcome = intake_outcome(owner, "1", "release-joins-alert", existing)
    assert outcome.run_ids == (first["run_id"],)
    assert [d.outcome for d in outcome.deliveries] == ["created"]


def test_step1_alert_after_a_release_incident_joins_it_and_claims_it(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
    bench,
):
    """#343 U3-A reverse order: release first, alert X joins, Y stays apart."""
    current, release_incident, release_run = release_first(
        owner, web, observer, catalog, bench
    )
    before = _incident(release_incident)
    x, y, z = new_alert("critical"), new_alert("warning"), new_alert("warning")

    joined = fire(current.app, x, T1)

    assert joined["outcome"] == "attached", joined
    assert joined["incident_id"] == str(release_incident)
    assert joined["run_id"] is None
    assert [r["run_id"] for r in _runs(release_incident)] == [release_run]
    after = _incident(release_incident)
    for column in CONTROL_COLUMNS:
        assert after[column] == before[column], column
    assert after["priority"] == min(RELEASE_PRIORITY, 0)  # critical escalates
    assert identities(x["fingerprint"]) == []  # X opened no incident of its own
    (event,) = [e for e in _events(release_incident) if e["actor"] == "alertmanager"]
    assert event["content"]["fingerprint"] == x["fingerprint"]

    # Another fingerprint is not X: the release incident is already claimed.
    other = fire(current.app, y, T2)
    assert other["outcome"] == "created" and other["incident_id"] != str(
        release_incident
    )
    # X resolved still holds the claim; a third fingerprint opens its own.
    assert resolve_on(current.app, x, T1)["outcome"] == "resolved_attached"
    third = fire(current.app, z, T3)
    assert third["outcome"] == "created"
    assert third["incident_id"] not in (str(release_incident), other["incident_id"])
    # one Run in total for the fault X belongs to
    assert len(_runs(release_incident)) == 1
    for item in (other, third):
        outcome = intake_outcome(owner, "1", "release-then-alert", item["incident_id"])
        assert outcome.run_ids == (item["run_id"],)


def resolve_on(app, alert_body, starts_at):
    from tests.acceptance.test_343_alert_after_release_incident import resolve as r343

    return r343(app, alert_body, starts_at)


def test_step1_duplicate_deployment_event_is_replayed_and_a_changed_one_refused(
    owner: DurableStore, web: ReleaseStore
):
    env = ReleaseEnv(owner, web)
    payload = env.payload()

    created = env.post(payload)
    replay = env.post(payload)
    changed = env.payload(env.payment_uid)
    changed["source_release_id"] = payload["source_release_id"]
    conflict = env.post(changed)

    assert created.status == 201, created.text
    assert replay.status == 200 and replay.json()["outcome"] == "replayed"
    assert conflict.status == 409, conflict.text
    assert conflict.json()["code"] == "RELEASE_IDENTITY_CONFLICT"
    with psycopg.connect(DSN) as conn:
        (count,) = conn.execute(
            "SELECT count(*) FROM opspilot_release_observations WHERE source=%s",
            (env.source,),
        ).fetchone()
    assert count == 1


def test_step1_follow_up_after_an_attachment_to_a_concluded_incident_sees_the_event():
    env = lab()
    fingerprint = uuid4().hex[:16]
    t0 = db_now()
    first = fire_alert(env, fingerprint, zulu(t0 - timedelta(minutes=10)))
    subject, first_run = UUID(first["incident_id"]), UUID(first["run_id"])
    published, _ = poll(env, subject, [_tool_round(), report_from_transcript])
    assert [o.status for _, o in published] == ["published"]

    joined = fire_alert(env, fingerprint, zulu(t0 - timedelta(minutes=2)))
    assert joined["outcome"] == "attached" and joined["run_id"] is None
    (event,) = alert_events(env.store, subject)

    response = act(env, subject, "follow_up", FOLLOW_UP)
    assert response.status == 200, response.text
    queued = env.store.rebuild(subject)
    successor = queued["run"]["run_id"]
    assert successor != first_run and queued["run"]["state"] == "queued"

    second, model = poll(env, subject, [_tool_round(), report_from_transcript])

    assert [o.status for _, o in second] == ["published"], second
    assert model.calls, "the successor Run made no model request"
    seen = json.dumps([m for c in model.calls[:1] for m in c.messages])
    assert event["content"]["text"] in seen  # the attached event reached the model
    assert FOLLOW_UP in seen
    outcome, snapshot = durable_outcome(
        env.store, env.events, "1", "attach-concluded-follow-up", subject
    )
    assert snapshot["run"]["run_id"] == successor
    assert outcome.final_state == "completed" and outcome.report_available
    assert outcome.decision == "report_available"
    intake = intake_outcome(env.store, "1", "attach-concluded-follow-up", subject)
    assert intake.run_ids == (str(first_run), str(successor))
    assert_read_only(outcome)


def test_step1_follow_up_after_an_attachment_to_a_handed_off_run_requeues_it():
    """C4-A assertion 2 / #304: the forced-final REPORT_INVALID handoff."""
    env = lab()
    fingerprint = uuid4().hex[:16]
    t0 = db_now()
    first = fire_alert(env, fingerprint, zulu(t0 - timedelta(minutes=10)))
    subject, run_id = UUID(first["incident_id"]), UUID(first["run_id"])
    handed, _ = poll(env, subject, [_tool_round(), bad_report, bad_report])
    ((_, handoff),) = handed
    assert handoff.status == "handed_off" and handoff.reason == "REPORT_INVALID", (
        handoff
    )
    joined = fire_alert(env, fingerprint, zulu(t0 - timedelta(minutes=2)))
    assert joined["outcome"] == "attached"
    (event,) = alert_events(env.store, subject)
    input_before = env.store.rebuild(subject)["run"]["input"]

    response = act(env, subject, "follow_up", FOLLOW_UP)
    assert response.status == 200, response.text
    requeued = env.store.rebuild(subject)
    assert requeued["run"]["run_id"] == run_id  # same Run (W2-A)
    assert requeued["run"]["state"] == "queued"
    assert requeued["run"]["input"] == input_before  # window unchanged

    second, model = poll(env, subject, [report_from_transcript])

    ((_, resumed),) = second
    assert resumed.status == "published", resumed  # not blocked(INCOMPATIBLE_STATE)
    assert resumed.reason != "INCOMPATIBLE_STATE"
    assert len(model.calls) >= 1
    assert event["content"]["text"] in json.dumps(model.calls[0].messages)
    outcome, snapshot = durable_outcome(
        env.store, env.events, "1", "attach-handoff-follow-up", subject
    )
    assert snapshot["run"]["run_id"] == run_id
    assert outcome.final_state == "completed" and outcome.report_available
    assert "worker_resumed" in outcome.actions


def test_step1_successor_run_after_attached_episode_reframes_on_new_episode():
    """#305 W1-B (optional, not an F2 gate; needs PR #346)."""
    env = lab()
    fingerprint = uuid4().hex[:16]
    t0 = db_now()
    first_starts = zulu(t0 - timedelta(minutes=30))
    first = fire_alert(env, fingerprint, first_starts)
    subject, first_run = UUID(first["incident_id"]), UUID(first["run_id"])
    original = env.store.rebuild(subject)["run"]["input"]
    time.sleep(1.1)  # the attached delivery lands after the first frame's end
    attached_starts = zulu(db_now() - timedelta(minutes=2))
    assert fire_alert(env, fingerprint, attached_starts)["outcome"] == "attached"

    assert act(env, subject, "cancel").status == 200
    assert act(env, subject, "new_run").status == 200

    rebuilt = env.store.rebuild(subject)
    successor = rebuilt["run"]
    assert successor["run_id"] != first_run
    policy = successor["input"]["evidence_context"]["time_policies"][0]  # A4
    created_at = successor["deadline"] - timedelta(seconds=RUN_SECONDS)
    start, end = parse(policy["window"]["start"]), parse(policy["window"]["end"])
    assert parse(policy["anchor"]) == parse(attached_starts), policy
    assert abs((end - created_at).total_seconds()) <= 1.0, (end, created_at)
    assert end - start == timedelta(hours=24)
    assert parse(policy["default_query_window"]["start"]) == max(
        parse(attached_starts) - timedelta(hours=1), start
    )
    assert parse(policy["default_query_window"]["end"]) == end
    # the first Run kept its own frame (in-flight inputs never change)
    first_policy = original["evidence_context"]["time_policies"][0]
    assert parse(first_policy["anchor"]) == parse(first_starts)


# ===========================================================================
# STEP 2: kill the investigation worker; the same incident resumes with
#         retained evidence and bounded repeated work
# ===========================================================================


class Crash(RuntimeError):
    """What a killed process leaves: the attempt stops, its lease stays."""


def _batch(*steps: int):
    return reply(
        tool_calls=[
            tool_call(
                f"call-{n}",
                name=FIXTURE_TOOL,
                arguments=f'{{"expr":"rate(http_errors[5m])","step_seconds":{s}}}',
            )
            for n, s in enumerate(steps)
        ],
        finish="tool_calls",
    )


def _resume_after_lease(env, subject, replies, epoch_before):
    """The next worker: refused while the dead lease is live, then claims."""
    early = runner(env, [report_from_transcript]).resume(subject)
    assert early.status == "control_denied" and early.reason == "LEASE_ACTIVE", early
    assert early.epoch is None
    lapsed = time.monotonic()
    while env.store.rebuild(subject)["run"]["lease_until"] > db_now():
        assert time.monotonic() - lapsed < LEASE_SECONDS + 3
        time.sleep(0.1)
    worker = runner(env, replies)
    outcome = worker.resume(subject)
    assert outcome.epoch == epoch_before + 1  # one poll after expiry, new epoch
    return outcome, worker.model


def test_step2_kill_while_the_model_request_is_in_flight_then_resume():
    env = lab()
    subject, run_id = alert_incident(env, "bp1")
    first = runner(env, [_batch(30, 60), Crash("killed in flight")])
    with pytest.raises(Crash):
        first.resume(subject)
    killed = env.store.rebuild(subject)
    evidence_before = adopted_evidence(killed)
    assert len(evidence_before) == 2
    used = env.store.run_usage(run_id)["model_requests_used"]
    assert used == 2  # the killed request keeps its reservation
    tools_before = env.store.run_usage(run_id)["tool_operations_used"]

    resumed, model = _resume_after_lease(
        env, subject, [report_from_transcript], killed["run"]["epoch"]
    )

    assert resumed.status == "published", resumed
    assert len(model.calls) == 1  # the lost request is sent again once
    usage = env.store.run_usage(run_id)
    assert usage["model_requests_used"] == used + 1
    assert usage["tool_operations_used"] == tools_before  # no re-query
    outcome, snapshot = durable_outcome(
        env.store, env.events, "2", "kill-model", subject
    )
    assert snapshot["run"]["run_id"] == run_id  # same incident, same Run
    assert outcome.final_state == "completed" and outcome.report_available
    assert set(evidence_before) <= set(adopted_evidence(snapshot))
    assert set(outcome.evidence_ids) & set(evidence_before)  # cited by the report
    assert "worker_resumed" in outcome.actions
    assert_read_only(outcome)


def test_step2_kill_after_the_reply_commits_before_its_tool_runs_then_resume():
    env = lab()
    subject, run_id = alert_incident(env, "bp2")
    clock = DurableClock(env.store)
    inner = fixture_executor_factory(env.store, evidence=env.evidence, clock=clock)
    dispatched: list[str] = []
    state = {"die": True}

    def factory(lease, run_input):
        executor = inner(lease, run_input)

        class Dying:
            scope = executor.scope
            tool_seconds_used = 0.0

            def execute(self, request):
                if state["die"]:
                    state["die"] = False
                    raise Crash("killed before the tool was dispatched")
                dispatched.append(request.step_id)
                return executor.execute(request)

        return Dying()

    with pytest.raises(Crash):
        runner(env, [_batch(30)], parallelism=1, factory=factory).resume(subject)
    killed = env.store.rebuild(subject)
    assert len(killed["pending_tools"]) == 1 and adopted_evidence(killed) == []
    used = env.store.run_usage(run_id)["model_requests_used"]

    early = runner(env, [], factory=factory).resume(subject)
    assert early.reason == "LEASE_ACTIVE"
    time.sleep(LEASE_SECONDS + 0.5)
    resumed = runner(env, [report_from_transcript], factory=factory).resume(subject)

    assert resumed.status == "published", resumed
    assert resumed.epoch == killed["run"]["epoch"] + 1
    assert resumed.replayed_tools == 1 and len(dispatched) == 1  # once, not twice
    assert env.store.run_usage(run_id)["model_requests_used"] == used + 1
    outcome, snapshot = durable_outcome(
        env.store, env.events, "2", "kill-plan", subject
    )
    assert snapshot["pending_tools"] == []
    assert outcome.final_state == "completed" and "worker_resumed" in outcome.actions
    assert outcome.evidence_ids


class _StuckFirst:
    """Fixture transport: the first ``step_seconds=30`` read hangs until released."""

    lock = threading.Lock()
    requests: list[TransportRequest] = []
    release = threading.Event()
    blocked = False

    def fetch(self, request: TransportRequest) -> TransportResponse:
        cls = type(self)
        with cls.lock:
            cls.requests.append(request)
            block = request.params.get("step_seconds") == 30 and not cls.blocked
            cls.blocked = cls.blocked or block
        if block:
            cls.release.wait(timeout=30)
        return TransportResponse(
            body=body([{"metric": "checkout", "value": 3}]), data_as_of=WINDOW_START
        )

    @classmethod
    def count(cls, step: int) -> int:
        with cls.lock:
            return sum(1 for r in cls.requests if r.params.get("step_seconds") == step)


def test_step2_kill_while_a_tool_is_in_flight_keeps_the_settled_sibling(monkeypatch):
    _StuckFirst.requests, _StuckFirst.blocked = [], False
    _StuckFirst.release = threading.Event()
    monkeypatch.setattr(fixture_module, "_CannedTransport", _StuckFirst)
    env = lab()
    subject, run_id = alert_incident(env, "bp3")
    box: dict[str, Any] = {}

    def attempt():
        try:
            box["outcome"] = runner(env, [_batch(30, 60)]).resume(subject)
        except BaseException as exc:  # noqa: BLE001 - observed after release
            box["error"] = exc

    abandoned = threading.Thread(target=attempt, daemon=True)
    abandoned.start()
    try:
        sibling = wait_until(
            lambda: adopted_evidence(env.store.rebuild(subject)), timeout=10
        )
        assert len(sibling) == 1, "the settled sibling was not committed"
        assert abandoned.is_alive() and _StuckFirst.count(30) == 1
        epoch = env.store.rebuild(subject)["run"]["epoch"]

        resumed, model = _resume_after_lease(
            env, subject, [report_from_transcript], epoch
        )

        assert resumed.status == "published", resumed
        assert _StuckFirst.count(60) == 1  # the settled sibling is not re-sent
        assert _StuckFirst.count(30) == 2  # only the in-flight read, once more
        assert len(model.calls) == 1
        usage = env.store.run_usage(run_id)
        assert usage["tool_operations_used"] == 3  # the stuck one stays counted
        outcome, snapshot = durable_outcome(
            env.store, env.events, "2", "kill-tool", subject
        )
        assert sibling[0] in adopted_evidence(snapshot)
        assert sibling[0] in outcome.evidence_ids
        assert (
            outcome.final_state == "completed" and "worker_resumed" in outcome.actions
        )
    finally:
        _StuckFirst.release.set()
        abandoned.join(timeout=15)
    after = env.store.rebuild(subject)
    assert after["run"]["state"] == "completed"
    # the dead attempt changed nothing
    assert after["conclusion"] == snapshot["conclusion"]
    assert late_rows(after)  # its write landed as history only


# ===========================================================================
# STEP 3: a stalled query: bounded timeout, cleanup, partial evidence kept,
#         explicit status
# ===========================================================================

STALL_MARK = b"step=45"  # ASSUMPTION A6
SOURCE_PREFIX = "f2acc-source"


class StallSource:
    """127.0.0.1 HTTP source: a request whose line carries ``STALL_MARK``
    gets headers and then one body byte every 0.2 s, forever; any other
    request gets ``body`` at once. Records requests and client closes."""

    def __init__(self, response: bytes, on_stall=None):
        self.response = response
        self.on_stall = on_stall
        self.requests: list[bytes] = []
        self.closed: list[threading.Event] = []
        self.stop = threading.Event()
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(16)
        self.listener.settimeout(0.1)
        self.url = f"http://127.0.0.1:{self.listener.getsockname()[1]}"
        threading.Thread(
            target=self._accept, name=f"{SOURCE_PREFIX}-accept", daemon=True
        ).start()

    def _accept(self):
        while not self.stop.is_set():
            try:
                conn, _ = self.listener.accept()
            except (TimeoutError, OSError):
                continue
            closed = threading.Event()
            self.closed.append(closed)
            threading.Thread(
                target=self._serve,
                args=(conn, closed),
                name=f"{SOURCE_PREFIX}-conn",
                daemon=True,
            ).start()

    def _serve(self, conn, closed):
        try:
            conn.settimeout(5)
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                head += chunk
            self.requests.append(head.split(b"\r\n", 1)[0])
            if STALL_MARK not in head.split(b"\r\n", 1)[0]:
                conn.sendall(self._head(len(self.response)) + self.response)
                return
            if self.on_stall is not None:
                self.on_stall()
            conn.sendall(self._head(10_000_000))
            conn.settimeout(0.2)
            while not self.stop.is_set():
                try:
                    if conn.recv(1) == b"":
                        break
                except TimeoutError:
                    pass
                conn.sendall(b"{")
        except OSError:
            pass
        finally:
            closed.set()
            conn.close()

    @staticmethod
    def _head(length: int) -> bytes:
        return (
            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            f"Content-Length: {length}\r\nConnection: close\r\n\r\n"
        ).encode()

    def stalled(self) -> int:
        return sum(1 for line in self.requests if STALL_MARK in line)

    def close(self):
        self.stop.set()
        self.listener.close()


def _prometheus(moment: datetime) -> bytes:
    return json.dumps(
        {
            "status": "success",
            "data": {
                "resultType": "matrix",
                "result": [
                    {
                        "metric": {"service_name": "checkout"},
                        "values": [[moment.timestamp(), "3"]],
                    }
                ],
            },
        }
    ).encode()


def _metrics_call(n: int, step: int):
    return tool_call(
        f"call-{n}",
        name=METRICS_TOOL,
        arguments=json.dumps(
            {
                "expr": "sum by(service_name) (increase(traces_span_metrics_calls_total[5m]))",
                "step_seconds": step,
            }
        ),
    )


def _report_citing_an_answered_read(model_call):
    for message in model_call.messages:
        if message.get("role") != "tool":
            continue
        view = json.loads(message["content"])
        if view.get("status") in ("ok", "no_data") and view.get("evidence_id"):
            return reply(
                content=report_json(
                    evidence_id=view["evidence_id"], target_ref=TARGET_ID
                )
            )
    return reply(content="no answered read to cite")


def otel_incident(run_seconds: float) -> SimpleNamespace:
    """An otel-demo Run accepted on the lab DB, with its workbench page."""
    store = DurableStore(DSN)
    store.install()
    log = DurableEventLog(store)
    log.install()
    evidence = DurableEvidenceStore(store)
    evidence.install()
    clock = DurableClock(store)
    incident, run = uuid4(), uuid4()
    deadline = db_now() + timedelta(seconds=run_seconds)
    run_input = otel_demo_face(clock).input_for(
        run_id=str(run),
        question="Why is checkout slow?",
        target_id=TARGET_ID,
        deadline=deadline,
        model_requests=5,
    )
    store.accept(
        incident,
        run,
        f"f2-acc-stall-{incident}",
        deadline=deadline,
        budget_limit=5,
        versions=otel_demo_versions(),
        input=run_input.as_json(),
    )
    ledger = DurableWebLedger(store)
    ledger.install()
    workbench = Workbench(
        incidents=DurableIncidentStore(store),
        events=log,
        evidence=evidence,
        ledger=ledger,
        run_versions=otel_demo_versions(),
        run_seconds=RUN_SECONDS,
        tool_face=otel_demo_face(clock),
    )
    config = AuthConfig(
        ui_users={UI_USER: hash_password(UI_PASSWORD)},
        event_tokens={token_digest(EVENT_TOKEN): "alertmanager"},
        auth_revision="f2-acceptance-pg",
        allowed_origins=frozenset({ORIGIN}),
    )
    app = create_app(workbench, Authenticator(config), clock)
    return SimpleNamespace(
        store=store,
        events=log,
        evidence=evidence,
        clock=clock,
        app=app,
        incident=incident,
        run=run,
    )


def otel_runner(env, source: StallSource, replies, *, lease_seconds: int):
    config = OtelDemoConfig(prometheus_url=source.url, jaeger_url=source.url)
    return InvestigationRunner(
        store=env.store,
        worker=Worker.create(env.store, otel_demo_versions()),
        model=ScriptedModel(replies),
        executor_factory=otel_demo_executor_factory(
            env.store, evidence=env.evidence, clock=env.clock, config=config
        ),
        clock=env.clock,
        lease_seconds=lease_seconds,
        events=env.events,
        evidence=env.evidence,
        tool_parallelism=2,
    )


def _live_threads() -> set[threading.Thread]:
    return {
        t
        for t in threading.enumerate()
        if t.is_alive() and not t.name.startswith(SOURCE_PREFIX)
    }


def _tool_results(snapshot) -> list[dict[str, Any]]:
    return [
        item["result"]
        for step in snapshot["steps"]
        if step["status"] != "late_result"
        for item in step["tool_results"] or ()
    ]


def test_step3_stalled_query_times_out_keeps_the_sibling_and_the_run_completes():
    source = StallSource(_prometheus(db_now() - timedelta(seconds=120)))
    env = otel_incident(run_seconds=300)
    worker = otel_runner(
        env,
        source,
        [
            reply(
                tool_calls=[_metrics_call(0, 30), _metrics_call(1, 45)],
                finish="tool_calls",
            ),
            _report_citing_an_answered_read,
        ],
        lease_seconds=60,
    )
    env.store.rebuild(env.incident)  # the pool and its threads exist already
    baseline = _live_threads()
    try:
        started = time.monotonic()
        outcome = worker.resume(env.incident)
        elapsed = time.monotonic() - started

        assert outcome.status == "published", outcome
        # registered 30 s bound (well inside the 300 s Run) + 1 s slack
        assert elapsed <= 30.0 + 1.0 + 5.0, elapsed
        snapshot = env.store.rebuild(env.incident)
        results = _tool_results(snapshot)
        timed_out = [r for r in results if r.get("status") == "timeout"]
        answered = [r for r in results if r.get("status") in ("ok", "no_data")]
        assert len(timed_out) == 1 and len(answered) == 1, results
        assert timed_out[0].get("reason") == "TOOL_TIMEOUT"
        assert timed_out[0].get("source_contact") == "possible"
        assert not timed_out[0].get("adopted")  # stalled bytes are no evidence
        assert source.stalled() == 1  # the gateway did not retry on its own
        assert all(c.wait(2.0) for c in source.closed)  # connection closed
        stray = wait_until(lambda: not (_live_threads() - baseline), timeout=5)
        assert stray, sorted(t.name for t in _live_threads() - baseline)

        projected, _ = durable_outcome(
            env.store, env.events, "3", "query-stall", env.incident
        )
        assert projected.final_state == "completed"
        assert answered[0]["evidence_id"] in projected.evidence_ids  # partial kept
        assert projected.report_available
        assert_read_only(projected)
        page = call(env.app, "GET", f"/incidents/{env.incident}", headers=basic())
        assert page.status == 200
        assert "source may have received the request" in page.text
    finally:
        source.close()


def test_step3_a_stall_past_the_run_deadline_ends_in_an_explicit_handoff():
    source = StallSource(_prometheus(db_now() - timedelta(seconds=120)))
    env = otel_incident(run_seconds=4)
    worker = otel_runner(
        env,
        source,
        [
            reply(tool_calls=[_metrics_call(0, 45)], finish="tool_calls"),
            _report_citing_an_answered_read,
        ],
        lease_seconds=30,
    )
    try:
        started = time.monotonic()
        outcome = worker.resume(env.incident)
        elapsed = time.monotonic() - started

        assert elapsed <= 4.0 + 1.0 + 3.0, elapsed  # clipped to the Run's deadline
        assert outcome.status != "published", outcome
        # The worker's next poll sweeps the overdue Run (ADR-0005 section 2).
        swept = sweep_expired(env.store, env.events, incident_id=env.incident)
        settled = env.store.rebuild(env.incident)["run"]["state"]
        assert swept or settled == "waiting_human", (swept, settled, outcome)
        projected, snapshot = durable_outcome(
            env.store, env.events, "3", "query-stall-deadline", env.incident
        )
        assert snapshot["run"]["state"] == "waiting_human"  # not silently hanging
        assert (
            projected.decision == "handoff" and projected.human_interaction == "handoff"
        )
        assert "DEADLINE_EXCEEDED" in projected.handoff_reasons, projected
        assert projected.report_available is False
        assert source.stalled() == 1
    finally:
        source.close()


# ===========================================================================
# STEP 4: an alert storm: priority, concurrency limit, backpressure and
#         per-incident budgets bound the work
# ===========================================================================


@pytest.fixture(scope="module")
def stub_certificate(tmp_path_factory):
    return make_certificate(tmp_path_factory.mktemp("f2-acc-tls"))


@pytest.fixture
def model_stub(stub_certificate):
    cert, key = stub_certificate
    stub = ModelStub(cert=cert, key=key)
    yield stub
    stub.close()


@pytest.fixture
def storm_conn():
    with psycopg.connect(DSN) as conn:
        neutralize_claimable(conn)
        yield conn


@pytest.fixture
def child_workers(model_stub, worker_dsn, tmp_path):
    started: list[WorkerProcess] = []

    def start(**env):
        poll_seconds = float(env.pop("poll_seconds", 0.2))
        proc = WorkerProcess(
            model_stub,
            worker_dsn,
            tmp_path / f"worker-{len(started)}.log",
            poll_seconds=poll_seconds,
            **env,
        )
        started.append(proc)
        return proc

    yield start
    for proc in started:
        proc.stop()


def _current_states(conn, incidents) -> dict[UUID, str]:
    return {i: row["state"] for i, row in run_rows(conn, incidents).items()}


def _priorities(conn, incidents) -> dict[UUID, int]:
    rows = conn.execute(
        "SELECT incident_id,priority FROM opspilot_incidents WHERE incident_id=ANY(%s)",
        (list(incidents),),
    ).fetchall()
    conn.commit()
    return dict(rows)


def test_step4_alert_storm_is_kept_whole_and_bounded_by_priority_capacity_and_backlog(
    web_dsn, storm_conn, model_stub, child_workers
):
    app = storm_app(web_dsn, 1800)
    severities = ["critical"] * 10 + ["warning"] * 20 + [None] * 20
    incidents: list[UUID] = []
    for severity in severities:
        tag = new_tag()
        model_stub.plan(tag, Act("reject", hold=True))  # each claimed Run holds a slot
        incidents.append(storm_firing(app, tag, severity)[0])
    assert len(set(incidents)) == 50
    (runs,) = storm_conn.execute(
        "SELECT count(*) FROM opspilot_runs WHERE incident_id=ANY(%s)", (incidents,)
    ).fetchone()
    storm_conn.commit()
    assert runs == 50  # every alert durably accepted: one incident, one Run each

    peak = {"running": 0}
    stop = threading.Event()

    def sample():
        with psycopg.connect(DSN) as conn:
            while not stop.is_set():
                states = _current_states(conn, incidents)
                running = sum(1 for s in states.values() if s == "running")
                peak["running"] = max(peak["running"], running)
                stop.wait(0.03)

    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    worker = child_workers(
        OPSPILOT_WORKER_CAPACITY="2", OPSPILOT_BACKLOG_MAX="8", poll_seconds=0.2
    )
    try:

        def settled():
            states = _current_states(storm_conn, incidents)
            counts = {s: list(states.values()).count(s) for s in set(states.values())}
            return (
                states
                if counts.get("running") == 2
                and counts.get("running", 0) + counts.get("queued", 0) <= 2 + 8
                and counts.get("waiting_human", 0) >= 40
                else None
            )

        states = wait_until(settled, timeout=60)
        assert states, worker.log()
        time.sleep(1.0)  # a few more passes: nothing else may change the shape
        states = _current_states(storm_conn, incidents)
        priorities = _priorities(storm_conn, incidents)
        running = [i for i, s in states.items() if s == "running"]
        queued = [i for i, s in states.items() if s == "queued"]
        parked = [i for i, s in states.items() if s == "waiting_human"]

        assert len(running) == 2 and peak["running"] <= 2  # capacity
        assert len(queued) <= 8  # backlog bound
        assert len(running) + len(queued) + len(parked) == 50  # nothing lost
        assert {priorities[i] for i in running} == {0}  # critical first
        assert max(priorities[i] for i in running + queued) <= min(
            priorities[i] for i in parked
        )
        handoffs = events_of(storm_conn, parked, "run_handoff")
        assert sorted(str(s) for s, _, _ in handoffs) == sorted(str(p) for p in parked)
        for _, _, payload in handoffs:  # ASSUMPTION A3
            assert payload["reasons"] == ["BACKLOG_FULL"]
            assert payload["parked"] is True
            assert payload["execution"] == "not_started"

        store = DurableStore(DSN)
        log = DurableEventLog(store)
        try:
            outcome, _ = durable_outcome(store, log, "4", "storm-parked", parked[-1])
        finally:
            store.close()
        assert outcome.final_state == "failed" and outcome.decision == "handoff"
        assert outcome.handoff_reasons == ("BACKLOG_FULL",)
        assert outcome.human_interaction == "handoff"
        assert outcome.report_available is False and outcome.evidence_ids == ()

        # A human follow_up re-queues a parked Run; it is never parked again.
        revived = parked[0]
        generation = storm_conn.execute(
            "SELECT control_generation FROM opspilot_incidents WHERE incident_id=%s",
            (revived,),
        ).fetchone()[0]
        storm_conn.commit()
        response = post_form(
            app,
            f"/incidents/{revived}/control",
            {
                "action": "follow_up",
                "expected_generation": str(generation),
                "idempotency_key": uuid4().hex,
                "text": "Please look at this one now.",
            },
            headers={**basic(), **same_origin(), "accept": "application/json"},
        )
        assert response.status == 200, response.text
        time.sleep(1.5)  # several worker passes
        assert _current_states(storm_conn, [revived])[revived] in {"queued", "running"}
        assert len(events_of(storm_conn, [revived], "run_handoff")) == 1
    finally:
        model_stub.release_all()
        worker.stop()
        stop.set()
        sampler.join(5)


def test_step4_a_higher_severity_attachment_only_raises_the_priority(
    harness, database, alert
):
    app = harness()
    labels = alert["labels"]
    first = fire(app, alert, T1, labels={**labels, "severity": "warning"})
    subject = UUID(first["incident_id"])

    def priority():
        with psycopg.connect(database) as conn:
            return conn.execute(
                "SELECT priority FROM opspilot_incidents WHERE incident_id=%s",
                (subject,),
            ).fetchone()[0]

    assert priority() == 1
    up = fire(app, alert, T2, labels={**labels, "severity": "critical"})
    assert up["outcome"] == "attached" and priority() == 0
    down = fire(app, alert, T3, labels={**labels, "severity": "info"})
    assert down["outcome"] == "attached" and priority() == 0  # never lowered
    assert run_count(database, subject) == 1


@pytest.mark.parametrize(
    "env",
    [
        {"OPSPILOT_BACKLOG_MAX": "0"},
        {"OPSPILOT_BACKLOG_MAX": "many"},
        {"OPSPILOT_WORKER_CAPACITY": "3", "OPSPILOT_POOL_MAX_SIZE": "4"},
    ],
)
def test_step4_an_unbounded_or_unsafe_configuration_refuses_to_start(
    storm_conn, model_stub, child_workers, env
):
    worker = child_workers(**env)
    code = worker.wait_exit(20)
    assert code is not None and code != 0, worker.log()
    assert model_stub.arrived() == []


def test_step4_one_incident_never_runs_two_attempts_at_once():
    env = lab()
    subject, run_id = alert_incident(env, "single")
    entered, release = threading.Event(), threading.Event()

    def held(model_call):
        entered.set()
        release.wait(timeout=20)
        return report_from_transcript(model_call)

    box: dict[str, Any] = {}
    first = threading.Thread(
        target=lambda: box.setdefault(
            "outcome", runner(env, [_batch(30), held]).resume(subject)
        ),
        daemon=True,
    )
    first.start()
    try:
        assert entered.wait(15), "the first attempt never reached its model call"
        second = runner(env, [report_from_transcript])
        refused = second.resume(subject)
        assert refused.status == "control_denied" and refused.reason == "LEASE_ACTIVE"
        assert second.model.calls == []
    finally:
        release.set()
        first.join(timeout=20)
    assert box["outcome"].status == "published", box
    with psycopg.connect(DSN) as conn:
        (runs,) = conn.execute(
            "SELECT count(*) FROM opspilot_runs WHERE incident_id=%s", (subject,)
        ).fetchone()
    assert runs == 1
    outcome, snapshot = durable_outcome(env.store, env.events, "4", "one-run", subject)
    assert snapshot["run"]["run_id"] == run_id and snapshot["run"]["epoch"] == 1
    assert (
        outcome.final_state == "completed" and "worker_resumed" not in outcome.actions
    )


def test_step4_the_per_run_model_budget_bounds_the_work():
    assert MAX_MODEL_REQUESTS_PER_RUN == 100  # ASSUMPTION A5
    default = lab()
    subject, run_id = alert_incident(default, "budget-default")
    with psycopg.connect(DSN) as conn:
        (limit,) = conn.execute(
            "SELECT budget_limit FROM opspilot_runs WHERE run_id=%s", (run_id,)
        ).fetchone()
    assert limit == MAX_MODEL_REQUESTS_PER_RUN
    assert default.store.rebuild(subject)["run"]["input"]["model_requests"] == 100

    env = lab(budget_limit=3)
    # A tool-hungry model: the last request is the forced final round.
    subject, run_id = alert_incident(env, "budget-small")
    results, model = poll(env, subject, [_batch(15 + 15 * n) for n in range(10)])

    assert len(model.calls) == 3  # never more than the budget
    assert env.store.run_usage(run_id)["model_requests_used"] == 3
    ((_, attempt),) = results
    assert attempt.status == "handed_off", attempt
    hungry, _ = durable_outcome(env.store, env.events, "4", "budget-final", subject)
    assert hungry.decision == "handoff" and hungry.report_available is False
    assert hungry.handoff_reasons, hungry

    # A worker killed on its last request: the restart has no budget left.
    subject, run_id = alert_incident(env, "budget-restart")
    with pytest.raises(Crash):
        runner(env, [_batch(30), _batch(60), Crash("killed")]).resume(subject)
    time.sleep(LEASE_SECONDS + 0.5)
    restarted = runner(env, [report_from_transcript])
    settled = restarted.resume(subject)

    assert restarted.model.calls == []  # nothing beyond the budget is sent
    assert settled.status == "handed_off", settled
    assert env.store.run_usage(run_id)["model_requests_used"] == 3
    outcome, _ = durable_outcome(env.store, env.events, "4", "budget", subject)
    assert outcome.final_state == "budget_exhausted", outcome
    assert "BUDGET_EXHAUSTED" in outcome.handoff_reasons
    assert outcome.report_available is False


# ===========================================================================
# STEP 5: cancel or pause while work is in flight; a late completion cannot
#         overwrite the newer human decision
# ===========================================================================


def _decide_while_in_flight(env, subject, action):
    """A model reply that arrives after ``action`` was taken by a human."""

    def build(model_call):
        response = act(env, subject, action)
        assert response.status == 200, response.text
        return report_from_transcript(model_call)

    return build


def test_step5_pause_while_the_model_reply_is_in_flight_then_resume():
    env = lab()
    subject, run_id = alert_incident(env, "pause")
    first, _ = poll(
        env, subject, [_tool_round(), _decide_while_in_flight(env, subject, "pause")]
    )
    assert [o.status for _, o in first] != ["published"], first

    outcome, snapshot = durable_outcome(
        env.store, env.events, "5", "pause-model", subject
    )
    assert snapshot["state"] == "paused" and snapshot["run"]["state"] == "paused"
    assert snapshot["control_generation"] == 1  # the human decision stands
    assert snapshot["conclusion"] is None  # the late reply published nothing
    late = late_rows(snapshot)  # ASSUMPTION A2
    assert late and all(row["control_generation"] == 0 for row in late)
    assert outcome.final_state == "paused"
    assert outcome.decision == "human_control" and outcome.human_interaction == "pause"
    assert "late_result_rejected" in outcome.actions
    assert "STALE_CONTROL_GENERATION" in outcome.handoff_reasons
    assert outcome.report_available is False
    assert outcome.evidence_ids  # evidence committed before the pause stays
    assert_read_only(outcome)

    # Paused: no worker may run it.
    idle, model = poll(env, subject, [report_from_transcript])
    assert idle == [] and model.calls == []

    assert act(env, subject, "resume").status == 200
    assert env.store.rebuild(subject)["run"]["state"] == "queued"
    resumed, _ = poll(env, subject, [report_from_transcript])
    assert [o.status for _, o in resumed] == ["published"], resumed
    final, snapshot = durable_outcome(
        env.store, env.events, "5", "pause-resume", subject
    )
    assert snapshot["run"]["run_id"] == run_id
    assert final.final_state == "completed" and final.report_available


def test_step5_cancel_while_the_model_reply_is_in_flight_is_final():
    env = lab()
    subject, run_id = alert_incident(env, "cancel")
    poll(env, subject, [_tool_round(), _decide_while_in_flight(env, subject, "cancel")])

    outcome, snapshot = durable_outcome(
        env.store, env.events, "5", "cancel-model", subject
    )
    assert snapshot["state"] == "cancelled" and snapshot["run"]["state"] == "cancelled"
    assert snapshot["conclusion"] is None
    assert late_rows(snapshot)
    assert outcome.final_state == "cancelled"
    assert outcome.decision == "human_control" and outcome.human_interaction == "cancel"
    assert "late_result_rejected" in outcome.actions
    assert "STALE_CONTROL_GENERATION" in outcome.handoff_reasons
    assert outcome.report_available is False
    assert_read_only(outcome)
    with psycopg.connect(DSN) as conn:
        (runs,) = conn.execute(
            "SELECT count(*) FROM opspilot_runs WHERE incident_id=%s", (subject,)
        ).fetchone()
    assert runs == 1  # the late completion opened no new Run
    idle, model = poll(env, subject, [report_from_transcript])
    assert idle == [] and model.calls == []
    assert runner(env, [report_from_transcript]).resume(subject).status == (
        "already_completed"
    )


def test_step5_pause_while_a_query_is_stalled_lets_the_late_result_be_history_only():
    """Also F2 step 3 pre-review item 7: a pause during a stall wins."""
    env = otel_incident(run_seconds=5)

    def pause():
        env.store.control(env.incident, 0, "pause", "operator:f2-acceptance")

    source = StallSource(_prometheus(db_now() - timedelta(seconds=120)), on_stall=pause)
    worker = otel_runner(
        env,
        source,
        [
            reply(tool_calls=[_metrics_call(0, 45)], finish="tool_calls"),
            _report_citing_an_answered_read,
        ],
        lease_seconds=30,
    )
    try:
        started = time.monotonic()
        result = worker.resume(env.incident)
        assert time.monotonic() - started <= 5.0 + 1.0 + 3.0
        assert result.status != "published", result
        outcome, snapshot = durable_outcome(
            env.store, env.events, "5", "pause-stalled-tool", env.incident
        )
        assert snapshot["state"] == "paused" and snapshot["run"]["state"] == "paused"
        assert snapshot["control_generation"] == 1
        assert snapshot["conclusion"] is None
        assert adopted_evidence(snapshot) == []  # no new evidence
        history = [
            json.dumps(row.get("response"), default=str) for row in late_rows(snapshot)
        ] + [json.dumps(r, default=str) for r in _tool_results(snapshot)]
        assert any('"denied"' in text or '"timeout"' in text for text in history), (
            history
        )
        assert outcome.final_state == "paused" and outcome.human_interaction == "pause"
        assert outcome.evidence_ids == () and outcome.report_available is False
        assert source.stalled() == 1
    finally:
        source.close()
