"""Independent acceptance tests for F2 PR-B1 on PostgreSQL (capacity + priority).

Written from the contract only (``docs/tasks/2026-10-21-f2-b1-contract.md``
section 3 public interface, section 5 B1 items 1-8, decision table all A),
without the implementation. The worker is the product entry point
``opspilot.worker_main.main`` in a child process under a throwaway
``opspilot_worker`` login role, configured by environment only; intake goes
through the real web routes under a throwaway ``opspilot_web`` login role
(so the new column grants of U-B1-1 are exercised). The model is a local
HTTPS stub (``tests.integration.f2_b1_support``). Observed: Run rows,
``run_claimed`` / ``run_handoff`` events and their ``recorded_at``, the
stub's concurrent requests, process start/exit.

Not covered here: item 4's "a later, higher-severity alert attached to the
open incident upgrades it, a lower one does not downgrade" -- that path is
PR-C's (not merged).
"""

from __future__ import annotations

import os
import time
from uuid import UUID, uuid4

import psycopg
import pytest

from opspilot.investigation.progress import sweep_expired
from opspilot.persistence import DurableStore, PersistenceError
from opspilot.persistence.grants import WEB_ROLE, WORKER_ROLE
from opspilot.tools.fixture import fixture_face, fixture_versions
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
from scripts.m0.postgres_lab import DSN
from tests.integration.f2_b1_support import (
    Act,
    ModelStub,
    Sampler,
    WorkerProcess,
    events_of,
    make_certificate,
    neutralize_claimable,
    new_tag,
    run_rows,
    running_live,
    wait_until,
)
from tests.integration.test_m1_04_alert_intake_impl_postgres import (
    REGISTRY,
    _alert,
    _webhook,
)
from tests.m1_web_support import (
    EVENT_TOKEN,
    ORIGIN,
    UI_PASSWORD,
    UI_USER,
    basic,
    bearer,
    post_form,
    post_json,
    same_origin,
)
from tests.runtime_roles_support import login

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)

START = 45.0  # child import + first poll, bounded
SETTLE = 30.0


# -- fixtures ---------------------------------------------------------------


@pytest.fixture(scope="module")
def web_dsn():
    with login(DSN, WEB_ROLE) as dsn:
        yield dsn


@pytest.fixture(scope="module")
def worker_dsn():
    with login(DSN, WORKER_ROLE) as dsn:
        yield dsn


@pytest.fixture(scope="module")
def certificate(tmp_path_factory):
    return make_certificate(tmp_path_factory.mktemp("f2b1-tls"))


@pytest.fixture
def stub(certificate):
    cert, key = certificate
    model = ModelStub(cert=cert, key=key)
    yield model
    model.close()


@pytest.fixture
def owner():
    with psycopg.connect(DSN) as conn:
        neutralize_claimable(conn)
        yield conn


@pytest.fixture
def workers(stub, worker_dsn, tmp_path):
    started: list[WorkerProcess] = []

    def start(**env):
        poll = float(env.pop("poll_seconds", 0.2))
        proc = WorkerProcess(
            stub,
            worker_dsn,
            tmp_path / f"worker-{len(started)}.log",
            poll_seconds=poll,
            **env,
        )
        started.append(proc)
        return proc

    yield start
    for proc in started:
        proc.stop()


def _app(dsn: str, run_seconds: int = 600):
    store = DurableStore(dsn)
    store.install()
    workbench = Workbench(
        incidents=DurableIncidentStore(store),
        events=DurableEventLog(store),
        evidence=DurableEvidenceStore(store),
        ledger=DurableWebLedger(store),
        run_versions=fixture_versions(),
        run_seconds=run_seconds,
        tool_face=fixture_face(),
        targets=MappingTargetRegistry(REGISTRY),
    )
    config = AuthConfig(
        ui_users={UI_USER: hash_password(UI_PASSWORD)},
        event_tokens={token_digest(EVENT_TOKEN): "alertmanager"},
        auth_revision="auth-rev-f2b1",
        allowed_origins=frozenset({ORIGIN}),
    )
    return create_app(workbench, Authenticator(config), DurableClock(store))


def _ui(app, tag: str) -> tuple[UUID, UUID]:
    response = post_form(
        app,
        "/intake/ui",
        {
            "target_id": "checkout-prod",
            "question": f"Why is checkout erroring? {tag}",
            "idempotency_key": f"f2b1-{uuid4()}",
        },
        headers={**basic(), **same_origin()},
    )
    assert response.status == 201, response.text
    body = response.json()
    return UUID(body["incident_id"]), UUID(body["run_id"])


def _firing(app, tag: str, severity: str | None) -> tuple[UUID, UUID]:
    labels = {"alertname": tag, "namespace": "otel-demo", "service": "checkout"}
    if severity is not None:
        labels["severity"] = severity
    response = post_json(
        app, "/intake/alertmanager", _webhook(_alert(labels=labels)), headers=bearer()
    )
    assert response.status == 200, response.text
    [result] = response.json()["results"]
    assert result["outcome"] == "created" and result["run_id"], result
    return UUID(result["incident_id"]), UUID(result["run_id"])


def _control(app, incident: UUID, action: str, generation: int, **extra) -> int:
    response = post_form(
        app,
        f"/incidents/{incident}/control",
        {
            "action": action,
            "expected_generation": str(generation),
            "idempotency_key": uuid4().hex,
            **extra,
        },
        headers={**basic(), **same_origin(), "accept": "application/json"},
    )
    assert response.status == 200, (action, response.text)
    return int(response.json()["generation"])


def _priority(conn, incident: UUID) -> int:
    value = conn.execute(
        "SELECT priority FROM opspilot_incidents WHERE incident_id=%s", (incident,)
    ).fetchone()[0]
    conn.commit()
    return value


def _first_claims(conn, incidents) -> list[UUID]:
    order: list[UUID] = []
    for subject, _, _ in events_of(conn, incidents, "run_claimed"):
        if subject not in order:
            order.append(subject)
    return order


def _claims(conn, incidents) -> dict[UUID, int]:
    counts = {i: 0 for i in incidents}
    for subject, _, _ in events_of(conn, incidents, "run_claimed"):
        counts[subject] += 1
    return counts


def _all_parked(conn, incidents) -> bool:
    rows = run_rows(conn, incidents)
    return len(rows) == len(list(incidents)) and all(
        r["state"] == "waiting_human" for r in rows.values()
    )


# -- schema and intake (contract §3, U-B1-1, U-B1-2, R-B1-1) ----------------


def test_priority_column_is_smallint_not_null_default_2_checked(web_dsn, owner):
    row = owner.execute(
        "SELECT data_type, is_nullable, column_default FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name='opspilot_incidents' "
        "AND column_name='priority'"
    ).fetchone()
    owner.commit()
    assert row is not None, "opspilot_incidents.priority is missing"
    assert row[0] == "smallint" and row[1] == "NO"
    assert row[2] is not None and row[2].split("::")[0].strip("'() ") == "2"
    incident, _ = _ui(_app(web_dsn), new_tag())
    for bad in (3, -1):
        with pytest.raises(psycopg.errors.CheckViolation):
            owner.execute(
                "UPDATE opspilot_incidents SET priority=%s WHERE incident_id=%s",
                (bad, incident),
            )
        owner.rollback()


@pytest.mark.parametrize(
    ("severity", "expected"),
    [("critical", 0), ("warning", 1), ("info", 2), ("page", 2), (None, 2)],
)
def test_alert_severity_sets_the_incident_priority(web_dsn, owner, severity, expected):
    incident, _ = _firing(_app(web_dsn), new_tag(), severity)
    assert _priority(owner, incident) == expected


def test_a_web_intake_incident_is_other(web_dsn, owner):
    """B1 item 4: the generic web entry is "other" (2)."""
    incident, _ = _ui(_app(web_dsn), new_tag())
    assert _priority(owner, incident) == 2


def test_accept_takes_a_priority_and_refuses_anything_outside_0_1_2(owner):
    store = DurableStore(DSN)
    store.install()
    deadline = owner.execute(
        "SELECT clock_timestamp() + interval '600 seconds'"
    ).fetchone()[0]
    owner.commit()

    def accept(*, inside: bool, **extra) -> tuple[UUID, str]:
        incident, run, key = uuid4(), uuid4(), f"f2b1-accept-{uuid4()}"
        kwargs = dict(
            deadline=deadline,
            budget_limit=4,
            versions=dict(fixture_versions()),
            input=None,
            target_id=None,
            **extra,
        )
        try:
            if inside:
                with store.transaction() as conn:
                    store._accept_in(conn, incident, run, key, **kwargs)
            else:
                store.accept(incident, run, key, **kwargs)
        finally:
            accept.last_key = key  # type: ignore[attr-defined]
        return incident, key

    for inside in (False, True):
        incident, _ = accept(inside=inside)
        assert _priority(owner, incident) == 2, inside
        for value in (0, 1, 2):
            incident, _ = accept(inside=inside, priority=value)
            assert _priority(owner, incident) == value, (inside, value)
        for bad in (3, -1, "1"):
            with pytest.raises(PersistenceError) as refused:
                accept(inside=inside, priority=bad)
            assert str(refused.value) == "INVALID_INPUT", (bad, inside)
            stored = owner.execute(
                "SELECT count(*) FROM opspilot_incidents WHERE intake_key=%s",
                (accept.last_key,),  # type: ignore[attr-defined]
            ).fetchone()[0]
            owner.commit()
            assert stored == 0, (bad, inside)


# -- item 1: capacity --------------------------------------------------------


def test_capacity_two_never_runs_more_than_two_and_refills_a_freed_slot(
    web_dsn, owner, stub, workers
):
    app = _app(web_dsn)
    tags = [new_tag() for _ in range(6)]
    incidents = []
    for tag in tags:
        stub.plan(tag, Act("reject", hold=True))
        incidents.append(_ui(app, tag)[0])
    sampler = Sampler(DSN, incidents)
    try:
        worker = workers(OPSPILOT_WORKER_CAPACITY="2", poll_seconds=0.5)
        assert wait_until(lambda: len(stub.arrived()) >= 2, timeout=START), worker.log()
        # Several poll periods with both slots busy: still exactly two.
        time.sleep(1.5)
        assert len(stub.arrived()) == 2 and len(stub.inflight_tags()) == 2
        assert running_live(owner, incidents) == 2
        rows = run_rows(owner, incidents)
        assert sorted(r["state"] for r in rows.values()).count("queued") == 4
        running = [r for r in rows.values() if r["state"] == "running"]
        assert all(r["lease_live"] and r["owner"] is not None for r in running)
        # Free one slot: another incident is claimed within one poll period.
        first = stub.arrived()[0]
        initial = {incidents[tags.index(t)] for t in stub.arrived()[:2]}
        stub.release(first)
        assert wait_until(lambda: len(stub.arrived()) >= 3, timeout=SETTLE)
        freed = incidents[tags.index(first)]
        handed = [t for s, t, _ in events_of(owner, [freed], "run_handoff")]
        assert handed, "released Run was not handed off"
        later = [
            t
            for s, t, _ in events_of(owner, incidents, "run_claimed")
            if s not in initial
        ]
        assert later, "no claim after the slot was freed"
        gap = (later[0] - handed[0]).total_seconds()
        assert 0 <= gap <= 0.5 + 1.5, gap
        assert len(stub.inflight_tags()) == 2
        for tag in tags:
            stub.release(tag)
        assert wait_until(lambda: _all_parked(owner, incidents), timeout=SETTLE)
    finally:
        sampler.close()
    assert sampler.samples > 10
    assert sampler.peak == 2, sampler.peak
    assert stub.peak == 2, stub.peak
    assert all(n == 1 for n in _claims(owner, incidents).values())
    for _, _, payload in events_of(owner, incidents, "run_handoff"):
        assert payload["reasons"] == ["MODEL_REJECTED"]
    assert worker.stop() == 0


def test_capacity_one_runs_one_attempt_at_a_time(web_dsn, owner, stub, workers):
    app = _app(web_dsn)
    tags = [new_tag() for _ in range(3)]
    incidents = []
    for tag in tags:
        stub.plan(tag, Act("reject", hold=True))
        incidents.append(_ui(app, tag)[0])
    sampler = Sampler(DSN, incidents)
    try:
        worker = workers(OPSPILOT_WORKER_CAPACITY="1")
        for n in range(1, 4):
            assert wait_until(lambda: len(stub.arrived()) >= n, timeout=START), (
                worker.log()
            )
            time.sleep(0.6)  # three poll periods: nothing else starts
            assert len(stub.arrived()) == n and len(stub.inflight_tags()) == 1
            stub.release(stub.arrived()[-1])
        assert wait_until(lambda: _all_parked(owner, incidents), timeout=SETTLE)
    finally:
        sampler.close()
    assert sampler.peak == 1 and stub.peak == 1
    # Same order as before B1 for equal priority: deadline order.
    assert _first_claims(owner, incidents) == incidents
    assert worker.stop() == 0


# -- item 2: priority, then deadline ----------------------------------------


def test_claims_follow_priority_then_deadline(web_dsn, owner, stub, workers):
    short, long = _app(web_dsn, 600), _app(web_dsn, 900)
    plan = [
        (short, "none"),
        (long, "critical"),
        (short, "warning"),
        (short, "critical"),
        (short, "ui"),
        (long, "warning"),
        (short, None),
    ]
    expected_priority = {"critical": 0, "warning": 1, "none": 2, None: 2, "ui": 2}
    incidents, priorities = [], {}
    for app, severity in plan:
        tag = new_tag()
        stub.plan(tag, Act("reject"))
        if severity == "ui":
            incident = _ui(app, tag)[0]
        else:
            incident = _firing(app, tag, None if severity == "none" else severity)[0]
        incidents.append(incident)
        priorities[incident] = expected_priority[severity]
    rows = run_rows(owner, incidents)
    expected = sorted(
        incidents, key=lambda i: (priorities[i], rows[i]["deadline"], str(i))
    )
    worker = workers(OPSPILOT_WORKER_CAPACITY="1")
    assert wait_until(lambda: _all_parked(owner, incidents), timeout=START + SETTLE), (
        worker.log()
    )
    order = _first_claims(owner, incidents)
    assert order == expected
    # The claim instants themselves are non-decreasing in (priority, deadline).
    claimed = events_of(owner, incidents, "run_claimed")
    keys = [(priorities[s], rows[s]["deadline"]) for s, _, _ in claimed]
    assert keys == sorted(keys)
    assert worker.stop() == 0


# -- item 3: a late critical takes the next free slot ------------------------


def test_a_critical_arriving_while_slots_are_full_is_claimed_next(
    web_dsn, owner, stub, workers
):
    app = _app(web_dsn)
    warn = [new_tag() for _ in range(3)]
    incidents = []
    for tag in warn:
        stub.plan(tag, Act("reject", hold=True))
        incidents.append(_firing(app, tag, "warning")[0])
    worker = workers(OPSPILOT_WORKER_CAPACITY="2")
    assert wait_until(lambda: len(stub.arrived()) >= 2, timeout=START), worker.log()
    assert set(stub.arrived()) == set(warn[:2])
    critical_tag = new_tag()
    stub.plan(critical_tag, Act("reject", hold=True))
    critical = _firing(app, critical_tag, "critical")[0]
    incidents.append(critical)
    time.sleep(1.0)  # five poll periods with both slots busy
    assert _claims(owner, [critical, incidents[2]]) == {critical: 0, incidents[2]: 0}
    assert run_rows(owner, [critical])[critical]["state"] == "queued"
    stub.release(warn[0])
    assert wait_until(lambda: len(stub.arrived()) >= 3, timeout=SETTLE)
    assert stub.arrived()[2] == critical_tag
    for tag in [*warn, critical_tag]:
        stub.release(tag)
    assert wait_until(lambda: _all_parked(owner, incidents), timeout=SETTLE)
    order = _first_claims(owner, incidents)
    assert order.index(critical) < order.index(incidents[2])
    assert worker.stop() == 0


# -- item 4: a renewed Run keeps the incident's priority ---------------------


def test_a_follow_up_renewed_run_inherits_the_incident_priority(
    web_dsn, owner, stub, workers
):
    short, normal = _app(web_dsn, 3), _app(web_dsn, 600)
    critical_tag, warning_tag = new_tag(), new_tag()
    stub.plan(critical_tag, Act("reject"))
    stub.plan(warning_tag, Act("reject"))
    critical, old_run = _firing(short, critical_tag, "critical")

    def overdue() -> bool:
        value = owner.execute(
            "SELECT deadline <= clock_timestamp() FROM opspilot_runs WHERE run_id=%s",
            (old_run,),
        ).fetchone()[0]
        owner.commit()
        return bool(value)

    assert wait_until(overdue, timeout=15)
    store = DurableStore(DSN)
    store.install()
    sweep_expired(store, DurableEventLog(store), incident_id=critical)
    assert run_rows(owner, [critical])[critical]["state"] == "waiting_human"
    warning = _firing(normal, warning_tag, "warning")[0]
    _control(normal, critical, "follow_up", 0, text="Check the error ratio again.")
    renewed = run_rows(owner, [critical])[critical]
    assert renewed["run_id"] != old_run and renewed["state"] == "queued"
    assert _priority(owner, critical) == 0 and _priority(owner, warning) == 1
    # The renewed Run's deadline is later than the warning's, so only the
    # inherited priority puts it first.
    assert renewed["deadline"] > run_rows(owner, [warning])[warning]["deadline"]
    worker = workers(OPSPILOT_WORKER_CAPACITY="1")
    assert wait_until(
        lambda: _all_parked(owner, [critical, warning]), timeout=START + SETTLE
    ), worker.log()
    claims = events_of(owner, [critical, warning], "run_claimed")
    assert [s for s, _, _ in claims][:2] == [critical, warning]
    assert claims[0][2]["run_id"] == str(renewed["run_id"])
    assert worker.stop() == 0


# -- item 5: one Run's model timeout does not abort another ------------------


def test_a_model_timeout_in_one_slot_does_not_abort_the_other_slots_request(
    web_dsn, owner, stub, workers
):
    hang_tag, ok_tag = new_tag(), new_tag()
    stub.plan(hang_tag, Act("hang"))
    stub.plan(ok_tag, Act("tool", hold=True), Act("report"))
    hung, _ = _ui(_app(web_dsn, 15), hang_tag)
    fine, _ = _ui(_app(web_dsn, 600), ok_tag)
    worker = workers(OPSPILOT_WORKER_CAPACITY="2")
    assert wait_until(
        lambda: {hang_tag, ok_tag} <= set(stub.arrived()), timeout=START
    ), worker.log()
    # The hung request ends when its own client gives up at the Run's wall.
    assert wait_until(lambda: hang_tag in stub.eof_at, timeout=SETTLE), worker.log()
    assert stub.first_arrival(ok_tag) < stub.eof_at[hang_tag]
    assert ok_tag in stub.inflight_tags(), "the other request was cut off"
    assert wait_until(
        lambda: run_rows(owner, [hung])[hung]["state"] == "waiting_human",
        timeout=SETTLE,
    )
    stub.release(ok_tag)
    assert wait_until(
        lambda: run_rows(owner, [fine])[fine]["state"] == "completed", timeout=SETTLE
    ), worker.log()
    assert stub.count(ok_tag) == 2
    assert [w for w in stub.writes if w[0] == ok_tag] == [
        (ok_tag, 0, True),
        (ok_tag, 1, True),
    ]
    assert len(events_of(owner, [fine], "run_completed")) == 1
    assert events_of(owner, [fine], "run_handoff") == []
    assert worker.stop() == 0


# -- item 6: a crashed slot -------------------------------------------------


def test_a_crashed_attempt_frees_its_slot_without_stopping_the_other_or_the_worker(
    web_dsn, owner, stub, workers
):
    app = _app(web_dsn)
    crash_tag, busy_tag, next_tag = new_tag(), new_tag(), new_tag()
    stub.plan(crash_tag, Act("crash", hold=True), Act("reject"))
    stub.plan(busy_tag, Act("reject", hold=True))
    stub.plan(next_tag, Act("reject", hold=True))
    crashed = _ui(app, crash_tag)[0]
    busy = _ui(app, busy_tag)[0]
    worker = workers(OPSPILOT_WORKER_CAPACITY="2")
    assert wait_until(
        lambda: {crash_tag, busy_tag} <= set(stub.arrived()), timeout=START
    ), worker.log()
    stub.release(crash_tag)
    assert wait_until(lambda: crash_tag not in stub.inflight_tags(), timeout=SETTLE)
    # The other slot is untouched and the process keeps running.
    time.sleep(0.6)
    assert worker.alive()
    assert busy_tag in stub.inflight_tags()
    rows = run_rows(owner, [crashed, busy])
    assert rows[busy]["state"] == "running" and rows[busy]["lease_live"]
    # The crashed Run keeps its lease to expiry, like a killed worker.
    assert rows[crashed]["state"] == "running" and rows[crashed]["lease_live"]
    assert stub.count(crash_tag) == 1
    # The crashed slot is free again: a new incident is claimed into it.
    later = _ui(app, next_tag)[0]
    assert wait_until(lambda: next_tag in stub.arrived(), timeout=SETTLE), worker.log()
    # Once the crashed Run's lease lapses it is claimable again.
    owner.execute(
        "UPDATE opspilot_runs SET lease_until = clock_timestamp() - interval '1 second' "
        "WHERE run_id=%s",
        (rows[crashed]["run_id"],),
    )
    owner.commit()
    stub.release(next_tag)
    assert wait_until(lambda: stub.count(crash_tag) == 2, timeout=SETTLE), worker.log()
    stub.release(busy_tag)
    assert wait_until(
        lambda: _all_parked(owner, [crashed, busy, later]), timeout=SETTLE
    )
    assert _claims(owner, [crashed, busy, later]) == {crashed: 2, busy: 1, later: 1}
    assert worker.alive()
    assert worker.stop() == 0


# -- item 7: pool constraint and the default-configuration storm -------------


@pytest.mark.parametrize(("capacity", "pool"), [("3", "5"), ("2", "4"), ("1", "3")])
def test_a_pool_smaller_than_capacity_plus_three_refuses_to_start(
    web_dsn, owner, stub, workers, capacity, pool
):
    tag = new_tag()
    stub.plan(tag, Act("reject"))
    incident = _ui(_app(web_dsn), tag)[0]
    worker = workers(OPSPILOT_WORKER_CAPACITY=capacity, OPSPILOT_POOL_MAX_SIZE=pool)
    code = worker.wait_exit(20)
    assert code is not None and code != 0, worker.log()
    log = worker.log()
    assert "OPSPILOT_POOL_MAX_SIZE" in log or "OPSPILOT_WORKER_CAPACITY" in log, log
    assert stub.count(tag) == 0
    assert events_of(owner, [incident], "run_claimed") == []


@pytest.mark.parametrize("capacity", ["0", "-1", "two"])
def test_an_invalid_capacity_refuses_to_start(web_dsn, owner, stub, workers, capacity):
    tag = new_tag()
    stub.plan(tag, Act("reject"))
    incident = _ui(_app(web_dsn), tag)[0]
    worker = workers(OPSPILOT_WORKER_CAPACITY=capacity)
    code = worker.wait_exit(20)
    assert code is not None and code != 0, worker.log()
    assert "OPSPILOT_WORKER_CAPACITY" in worker.log()
    assert events_of(owner, [incident], "run_claimed") == []


@pytest.mark.parametrize(("capacity", "pool"), [("2", "5"), ("1", "4"), ("3", "6")])
def test_a_pool_of_exactly_capacity_plus_three_starts(
    web_dsn, owner, stub, workers, capacity, pool
):
    tag = new_tag()
    stub.plan(tag, Act("reject"))
    incident = _ui(_app(web_dsn), tag)[0]
    worker = workers(OPSPILOT_WORKER_CAPACITY=capacity, OPSPILOT_POOL_MAX_SIZE=pool)
    assert wait_until(lambda: _all_parked(owner, [incident]), timeout=START), (
        worker.log()
    )
    assert worker.alive()
    assert worker.stop() == 0


def test_a_storm_of_50_under_the_default_configuration_has_no_pool_timeout(
    web_dsn, owner, stub, workers
):
    app = _app(web_dsn)
    incidents = []
    for _ in range(50):
        tag = new_tag()
        stub.plan(tag, Act("reject", delay=0.1))
        incidents.append(_ui(app, tag)[0])
    sampler = Sampler(DSN, incidents)
    try:
        # Neither OPSPILOT_WORKER_CAPACITY nor OPSPILOT_POOL_* is set. The
        # backlog cap (PR-B2, default 8) is lifted so all 50 stay claimable.
        worker = workers(OPSPILOT_BACKLOG_MAX="50")
        assert wait_until(lambda: _all_parked(owner, incidents), timeout=START + 60), (
            worker.log()
        )
    finally:
        sampler.close()
    assert stub.peak == 2, stub.peak  # default capacity 2
    assert sampler.peak <= 2
    assert all(n == 1 for n in _claims(owner, incidents).values())
    assert events_of(owner, incidents, "run_claim_refused") == []
    for _, _, payload in events_of(owner, incidents, "run_handoff"):
        assert payload["reasons"] == ["MODEL_REJECTED"], payload
    assert "TIMEOUT" not in worker.log()
    assert worker.stop() == 0
    assert "TIMEOUT" not in worker.log()


# -- item 8: human-held incidents are never claimed --------------------------


def test_paused_cancelled_and_taken_over_incidents_are_never_claimed(
    web_dsn, owner, stub, workers
):
    app = _app(web_dsn)
    tags = {name: new_tag() for name in ("paused", "cancelled", "taken", "normal")}
    for tag in tags.values():
        stub.plan(tag, Act("reject"))
    ids = {name: _ui(app, tag)[0] for name, tag in tags.items()}
    _control(app, ids["paused"], "pause", 0)
    _control(app, ids["cancelled"], "cancel", 0)
    _control(app, ids["taken"], "takeover", 0)
    worker = workers(OPSPILOT_WORKER_CAPACITY="2")
    assert wait_until(lambda: _all_parked(owner, [ids["normal"]]), timeout=START), (
        worker.log()
    )
    time.sleep(1.0)  # five more poll periods with free slots
    held = [ids["paused"], ids["cancelled"], ids["taken"]]
    assert events_of(owner, held, "run_claimed") == []
    assert all(stub.count(tags[n]) == 0 for n in ("paused", "cancelled", "taken"))
    rows = run_rows(owner, held)
    assert rows[ids["paused"]]["incident_state"] == "paused"
    assert rows[ids["cancelled"]]["state"] == "cancelled"
    assert rows[ids["taken"]]["state"] == "waiting_human"
    assert rows[ids["taken"]]["mode"] != "automatic"
    assert all(r["owner"] is None for r in rows.values())
    assert worker.stop() == 0
