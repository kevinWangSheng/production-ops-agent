"""Independent PostgreSQL acceptance tests for F2 PR-B2.

The tests use only the real Alertmanager intake, durable rows/events and the
public ``park_backlog`` helper.  The worker is a child process; its model
provider is the local B1 HTTPS stub, so no real model or external service is
called.  B2 is deliberately absent on this branch, therefore red tests before
the implementation is merged are expected.
"""

from __future__ import annotations

import concurrent.futures
import os
import re
import threading
import time
from collections import Counter
from uuid import UUID, uuid4

import psycopg
import pytest

from opspilot.investigation import progress
from opspilot.knowledge.jobs import GenerationStore
from opspilot.persistence import DurableStore
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
    WorkerProcess,
    events_of,
    make_certificate,
    neutralize_claimable,
    new_tag,
    run_rows,
    wait_until,
)
from tests.integration.test_f2_b1_capacity_priority_postgres import REGISTRY
from tests.integration.test_m1_04_alert_intake_impl_postgres import _alert, _webhook
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

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)

START = 45.0
SETTLE = 30.0
WORKER_OWNER_RE = re.compile(r"worker slot owner=([0-9a-f-]{36})")


@pytest.fixture(scope="module")
def web_dsn():
    with _login(DSN, WEB_ROLE) as dsn:
        yield dsn


@pytest.fixture(scope="module")
def worker_dsn():
    with _login(DSN, WORKER_ROLE) as dsn:
        yield dsn


@pytest.fixture(scope="module")
def certificate(tmp_path_factory):
    return make_certificate(tmp_path_factory.mktemp("f2b2-tls"))


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


def _login(dsn: str, role: str):
    # Importing the project helper keeps this file aligned with the existing
    # B1 runtime-role setup while avoiding a second role implementation.
    from tests.runtime_roles_support import login

    return login(dsn, role)


def _park(store: DurableStore, log: DurableEventLog, max_queued: int):
    """Resolve the B2 helper at test execution so pre-merge red tests collect."""
    assert hasattr(progress, "park_backlog"), "progress.park_backlog"
    return progress.park_backlog(store, log, max_queued)


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
        auth_revision="auth-rev-f2b2",
        allowed_origins=frozenset({ORIGIN}),
    )
    return create_app(workbench, Authenticator(config), DurableClock(store))


def _firing(app, tag: str, severity: str | None) -> tuple[UUID, UUID]:
    labels = {"alertname": tag, "namespace": "otel-demo", "service": "checkout"}
    if severity is not None:
        labels["severity"] = severity
    response = post_json(
        app,
        "/intake/alertmanager",
        _webhook(_alert(labels=labels)),
        headers=bearer(),
    )
    assert response.status == 200, response.text
    [result] = response.json()["results"]
    assert result["outcome"] == "created" and result["run_id"], result
    return UUID(result["incident_id"]), UUID(result["run_id"])


def _ui(app, tag: str) -> tuple[UUID, UUID]:
    response = post_form(
        app,
        "/intake/ui",
        {
            "target_id": "checkout-prod",
            "question": f"Why is checkout erroring? {tag}",
            "idempotency_key": f"f2b2-{uuid4()}",
        },
        headers={**basic(), **same_origin()},
    )
    assert response.status == 201, response.text
    body = response.json()
    return UUID(body["incident_id"]), UUID(body["run_id"])


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


def _counts(conn, incidents, *, owners: set[UUID] | None = None) -> Counter:
    owner_filter = ""
    params: tuple[object, ...]
    if owners is not None:
        owner_filter = " AND (r.state <> 'running' OR r.owner=ANY(%s))"
        params = (list(incidents), list(owners))
    else:
        params = (list(incidents),)
    rows = conn.execute(
        "SELECT r.state,count(*) FROM opspilot_runs r "
        "JOIN opspilot_incidents i ON i.current_run_id=r.run_id "
        "WHERE i.incident_id=ANY(%s)" + owner_filter + " GROUP BY r.state",
        params,
    ).fetchall()
    conn.commit()
    return Counter({state: int(count) for state, count in rows})


def _priorities(conn, incidents) -> dict[UUID, int]:
    rows = conn.execute(
        "SELECT incident_id,priority FROM opspilot_incidents WHERE incident_id=ANY(%s)",
        (list(incidents),),
    ).fetchall()
    conn.commit()
    return dict(rows)


def _claimed_by_owners(conn, incidents, owners: set[UUID]) -> list[tuple]:
    """Return run_claimed events whose Run is owned by this test's worker."""
    if not owners:
        return []
    events = events_of(conn, incidents, "run_claimed")
    run_ids = [UUID(event[2]["run_id"]) for event in events]
    if not run_ids:
        return []
    rows = conn.execute(
        "SELECT run_id FROM opspilot_runs WHERE run_id=ANY(%s) AND owner=ANY(%s)",
        (run_ids, list(owners)),
    ).fetchall()
    conn.commit()
    owned_run_ids = {row[0] for row in rows}
    return [event for event in events if UUID(event[2]["run_id"]) in owned_run_ids]


class _StateSampler:
    def __init__(self, dsn: str, incidents, owners: set[UUID]):
        self.dsn = dsn
        self.incidents = list(incidents)
        self.owners = set(owners)
        self.owners_lock = threading.Lock()
        self.samples: list[tuple[float, Counter]] = []
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def add_owners(self, owners: set[UUID]) -> None:
        with self.owners_lock:
            self.owners.update(owners)

    def _run(self):
        with psycopg.connect(self.dsn) as conn:
            while not self.stop.is_set():
                with self.owners_lock:
                    owners = set(self.owners)
                self.samples.append(
                    (
                        time.monotonic(),
                        _counts(conn, self.incidents, owners=owners),
                    )
                )
                self.stop.wait(0.03)

    def close(self):
        self.stop.set()
        self.thread.join(5)


def _set_deadlines(conn, incidents: list[UUID], seconds: list[int]) -> None:
    for incident, value in zip(incidents, seconds, strict=True):
        conn.execute(
            "UPDATE opspilot_runs SET deadline=clock_timestamp()+make_interval(secs=>%s) "
            "WHERE run_id=(SELECT current_run_id FROM opspilot_incidents WHERE incident_id=%s)",
            (value, incident),
        )
    conn.commit()


def _seed_ended_observation(conn, incident: UUID) -> None:
    session_id, ending_id = uuid4(), uuid4()
    target_id = conn.execute(
        "SELECT target_id FROM opspilot_incidents WHERE incident_id=%s", (incident,)
    ).fetchone()[0]
    conn.execute(
        "UPDATE opspilot_incidents SET observation_generation=1 WHERE incident_id=%s",
        (incident,),
    )
    conn.execute(
        "INSERT INTO opspilot_observation_sessions(session_id,incident_id,purpose,target_id,target,"
        "subject_control_generation,observation_generation,authorized_by,state,ended_reason,"
        "authorized_global_generation,authorized_target_generation,deadline_at,max_samples,"
        "sample_interval_seconds,sustained_window_seconds) VALUES(%s,%s,'incident_recovery',%s,'{}',"
        "0,1,'op','completed','recovery_confirmed',0,0,clock_timestamp(),1,60,60)",
        (session_id, incident, target_id),
    )
    conn.execute(
        "INSERT INTO opspilot_observation_endings(ending_id,session_id,incident_id,ended_reason,"
        "lifecycle_before,lifecycle_after) VALUES(%s,%s,%s,'deadline_expired','observing_recovery',"
        "'observing_recovery')",
        (ending_id, session_id, incident),
    )
    conn.commit()


def test_b2_storm_preserves_all_alerts_and_holds_the_queue_at_eight(
    web_dsn, owner, stub, workers
):
    """§5 item 9: 50 real webhook alerts, bounded queue and priority-safe parks."""
    neutralize_claimable(owner)
    app = _app(web_dsn, 1800)
    severities = ["critical"] * 17 + ["warning"] * 17 + [None] * 16
    incidents: list[UUID] = []
    for severity in severities:
        tag = new_tag()
        stub.plan(tag, Act("reject", hold=True))
        incident = _firing(app, tag, severity)[0]
        incidents.append(incident)

    worker_owners: set[UUID] = set()
    sampler = _StateSampler(DSN, incidents, worker_owners)
    worker = workers(
        OPSPILOT_WORKER_CAPACITY="2", OPSPILOT_BACKLOG_MAX="8", poll_seconds=0.2
    )
    try:

        def bounded():
            rows = run_rows(owner, incidents)
            # WorkerProcess.log() reads this child process's own log.  The
            # child prints one owner UUID per slot at startup, so a peer
            # worker cannot enter this set through a shared Run row.
            new_owners = {
                UUID(value) for value in WORKER_OWNER_RE.findall(worker.log())
            }
            worker_owners.update(new_owners)
            sampler.add_owners(new_owners)
            counts = _counts(owner, incidents, owners=worker_owners)
            claimed = _claimed_by_owners(owner, incidents, worker_owners)
            return (
                rows
                if len(rows) == 50
                and counts["waiting_human"]
                and counts["queued"] <= 8
                and counts["running"] == 2
                and len(worker_owners) == 2
                and len(claimed) >= 2
                else None
            )

        rows = wait_until(bounded, timeout=START + SETTLE)
        assert rows and len(rows) == 50, worker.log()
        counts = _counts(owner, incidents)
        assert counts["queued"] <= 8
        assert set(rows) == set(incidents)
        assert all(
            r["state"] in {"running", "queued", "waiting_human"} for r in rows.values()
        )
        assert max((sample["running"] for _, sample in sampler.samples), default=0) <= 2
        assert wait_until(
            lambda: any(
                sample["queued"] <= 8 and sample["waiting_human"] > 0
                for _, sample in sampler.samples
            ),
            timeout=5,
        )

        parked = [
            incident
            for incident, row in rows.items()
            if row["state"] == "waiting_human"
        ]
        queued = [
            incident for incident, row in rows.items() if row["state"] == "queued"
        ]
        priorities = _priorities(owner, incidents)
        claimed = [
            subject
            for subject, _, _ in _claimed_by_owners(owner, incidents, worker_owners)
        ]
        assert len(claimed) == 2
        assert [priorities[subject] for subject in claimed] == sorted(
            priorities[subject] for subject in claimed
        )
        assert all(priorities[p] >= priorities[q] for p in parked for q in queued)
        handoffs = events_of(owner, parked, "run_handoff")
        assert len(handoffs) == len(parked)
        for subject, _, payload in handoffs:
            assert subject in parked
            assert payload["reasons"] == ["BACKLOG_FULL"]
            assert payload["parked"] is True
            assert payload["execution"] == "not_started"
            assert payload["handoff"] is True
        assert all(
            len(events_of(owner, [subject], "run_handoff")) == 1 for subject in parked
        )
    finally:
        stub.release_all()
        worker.stop()
        sampler.close()


def test_same_priority_parks_the_latest_deadlines_first(
    web_dsn, worker_dsn, owner, stub
):
    """§5 item 10 / U-B2-3: equal priority means latest deadline is parked."""
    app = _app(web_dsn)
    incidents = [_firing(app, new_tag(), "warning")[0] for _ in range(4)]
    _set_deadlines(owner, incidents, [100, 200, 300, 400])
    store = DurableStore(worker_dsn)
    log = DurableEventLog(store)
    parked = _park(store, log, max_queued=2)
    assert {row[0] for row in parked} == set(incidents[2:])
    rows = run_rows(owner, incidents)
    assert rows[incidents[0]]["state"] == rows[incidents[1]]["state"] == "queued"
    assert all(rows[i]["state"] == "waiting_human" for i in incidents[2:])
    for incident in incidents[2:]:
        [(_, _, payload)] = events_of(owner, [incident], "run_handoff")
        assert payload["reasons"] == ["BACKLOG_FULL"]
        assert payload["parked"] is True and payload["execution"] == "not_started"


def test_backlog_pass_uses_its_observed_queue_for_concurrent_arrival(
    web_dsn, worker_dsn, owner
):
    """§5 item 9: a concurrent arrival is handled by the next pass."""
    app = _app(web_dsn)
    critical, warning_old, warning_new = [
        _firing(app, new_tag(), severity)[0]
        for severity in ("critical", "warning", "warning")
    ]
    _set_deadlines(owner, [critical, warning_old, warning_new], [100, 101, 200])

    locker = psycopg.connect(DSN)
    locker.execute(
        "SELECT incident_id FROM opspilot_incidents WHERE incident_id=%s FOR UPDATE",
        (warning_new,),
    )
    locker_pid = locker.execute("SELECT pg_backend_pid()").fetchone()[0]
    locker.commit()
    locker.execute(
        "SELECT incident_id FROM opspilot_incidents WHERE incident_id=%s FOR UPDATE",
        (warning_new,),
    )

    application_name = f"f2b2-concurrent-pass-{uuid4().hex}"
    pass_store = DurableStore(f"{worker_dsn} application_name={application_name}")
    pass_log = DurableEventLog(pass_store)
    pass_result: list[tuple] = []
    pass_error: list[BaseException] = []

    def run_pass() -> None:
        try:
            pass_result.append(progress.park_backlog(pass_store, pass_log, 2))
        except (
            BaseException
        ) as exc:  # pragma: no cover - reports implementation failures
            pass_error.append(exc)

    thread = threading.Thread(target=run_pass, daemon=True)
    thread.start()

    def pass_is_blocked() -> bool:
        with psycopg.connect(DSN) as conn:
            row = conn.execute(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE pid<>%s AND application_name=%s AND datname=current_database() "
                "AND state='active' AND wait_event_type='Lock'",
                (locker_pid, application_name),
            ).fetchone()
            return bool(row[0])

    assert wait_until(pass_is_blocked, timeout=5), (
        "backlog pass did not block on warning_new"
    )

    other, _ = _firing(app, new_tag(), None)
    critical_row = run_rows(owner, [critical])[critical]
    claim_store = DurableStore(worker_dsn)
    claim_store.claim(
        critical,
        critical_row["run_id"],
        uuid4(),
        fixture_versions(),
        lease_seconds=300,
    )

    locker.commit()
    locker.close()
    thread.join(10)
    assert not thread.is_alive(), (
        "blocked backlog pass did not finish after the row unlock"
    )
    assert not pass_error, pass_error
    assert pass_result

    priorities = _priorities(owner, [critical, warning_old, warning_new, other])
    rows = run_rows(owner, [critical, warning_old, warning_new, other])
    first_states = {incident: row["state"] for incident, row in rows.items()}
    assert first_states[critical] == "running", first_states

    second_store = DurableStore(worker_dsn)
    second_log = DurableEventLog(second_store)
    progress.park_backlog(second_store, second_log, 2)
    rows = run_rows(owner, [critical, warning_old, warning_new, other])
    assert rows[critical]["state"] == "running"
    assert _counts(owner, [critical, warning_old, warning_new, other])["queued"] <= 2
    parked = [
        incident
        for incident in (critical, warning_old, warning_new, other)
        if rows[incident]["state"] == "waiting_human"
    ]
    queued = [
        incident
        for incident in (critical, warning_old, warning_new, other)
        if rows[incident]["state"] == "queued"
    ]
    assert len(parked) == 1
    assert all(
        priorities[parked_id] >= priorities[queued_id]
        for parked_id in parked
        for queued_id in queued
    )


def test_follow_up_requeues_a_parked_run_once_and_overdue_follow_up_renews(
    web_dsn, worker_dsn, owner, stub, workers
):
    """§5 item 11 / U-B2-2: human control is an escape from the park."""
    app = _app(web_dsn)
    first_tag, second_tag = new_tag(), new_tag()
    stub.plan(second_tag, Act("reject"))
    first, second = (
        _firing(app, first_tag, "warning")[0],
        _firing(app, second_tag, "warning")[0],
    )
    _set_deadlines(owner, [first, second], [100, 200])
    store = DurableStore(worker_dsn)
    _park(store, DurableEventLog(store), max_queued=1)
    assert run_rows(owner, [second])[second]["state"] == "waiting_human"

    _control(app, second, "follow_up", 0, text="Please retry after the human check.")
    assert run_rows(owner, [second])[second]["state"] == "queued"
    _park(store, DurableEventLog(store), max_queued=1)
    assert run_rows(owner, [second])[second]["state"] == "queued"
    assert len(events_of(owner, [second], "run_handoff")) == 1

    # The existing Run is the one re-queued by follow_up; the model tag is
    # planned above so the worker actually claims it and hands it off.
    worker = workers(OPSPILOT_WORKER_CAPACITY="1", OPSPILOT_BACKLOG_MAX="1")
    assert wait_until(
        lambda: bool(events_of(owner, [second], "run_claimed")), timeout=START + SETTLE
    ), worker.log()
    assert wait_until(
        lambda: run_rows(owner, [second])[second]["state"] == "waiting_human",
        timeout=SETTLE,
    ), worker.log()
    worker.stop()

    overdue, _ = _firing(_app(web_dsn, 1), new_tag(), "warning")
    assert wait_until(
        lambda: bool(
            owner.execute(
                "SELECT deadline<=clock_timestamp() FROM opspilot_runs "
                "WHERE run_id=(SELECT current_run_id FROM opspilot_incidents WHERE incident_id=%s)",
                (overdue,),
            ).fetchone()[0]
        ),
        timeout=15,
    )
    owner.commit()
    DurableStore(DSN).sweep_expired_runs(incident_id=overdue)
    old_run = run_rows(owner, [overdue])[overdue]["run_id"]
    _control(_app(web_dsn), overdue, "follow_up", 0, text="Renew after timeout.")
    renewed = run_rows(owner, [overdue])[overdue]
    assert renewed["run_id"] != old_run and renewed["state"] == "queued"


def test_two_independent_passes_park_exactly_the_excess_rows(
    owner, web_dsn, worker_dsn
):
    """§5 item 12 / R-B2-2: advisory serialization prevents over-parking."""
    app = _app(web_dsn)
    incidents = [_firing(app, new_tag(), "warning")[0] for _ in range(5)]

    def invoke():
        store = DurableStore(worker_dsn)
        return _park(store, DurableEventLog(store), max_queued=2)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: invoke(), range(2)))
    parked = [item for result in results for item in result]
    assert len(parked) == 3
    assert len({item[1] for item in parked}) == 3
    rows = run_rows(owner, incidents)
    assert sum(row["state"] == "waiting_human" for row in rows.values()) == 3
    assert sum(row["state"] == "queued" for row in rows.values()) == 2
    for incident in incidents:
        handoffs = events_of(owner, [incident], "run_handoff")
        assert len(handoffs) <= 1
        if rows[incident]["state"] == "waiting_human":
            assert handoffs[0][2]["reasons"] == ["BACKLOG_FULL"]


def test_parked_incident_blocks_postmortem_until_cancelled_then_is_candidate(
    owner, web_dsn, worker_dsn
):
    """§5 item 13 / #255: waiting_human is unfinished; cancel restores candidacy."""
    app = _app(web_dsn)
    incident, _ = _firing(app, new_tag(), "warning")
    other, _ = _firing(app, new_tag(), "warning")
    _set_deadlines(owner, [incident, other], [200, 100])
    _seed_ended_observation(owner, incident)
    store = DurableStore(worker_dsn)
    _park(store, DurableEventLog(store), max_queued=1)
    assert run_rows(owner, [incident])[incident]["state"] == "waiting_human"
    with psycopg.connect(DSN) as conn:
        before = conn.execute(
            "SELECT count(*) FROM opspilot_postmortems WHERE incident_id=%s",
            (incident,),
        ).fetchone()[0]
    jobs = GenerationStore(DSN)
    assert incident not in jobs.candidates(limit=100)
    assert before == 0

    _control(app, incident, "cancel", 0)
    assert run_rows(owner, [incident])[incident]["state"] == "cancelled"
    assert incident in jobs.candidates(limit=100)
    with psycopg.connect(DSN) as conn:
        after = conn.execute(
            "SELECT count(*) FROM opspilot_postmortems WHERE incident_id=%s",
            (incident,),
        ).fetchone()[0]
    assert after == 0


def test_paused_incident_is_excluded_from_backlog_count_and_parking(
    owner, web_dsn, worker_dsn
):
    """§5 item 14: paused incidents do not consume the queued waterline."""
    app = _app(web_dsn)
    paused, _ = _firing(app, new_tag(), "warning")
    queued, _ = _firing(app, new_tag(), "warning")
    _control(app, paused, "pause", 0)
    assert run_rows(owner, [paused])[paused]["incident_state"] == "paused"
    store = DurableStore(worker_dsn)
    assert _park(store, DurableEventLog(store), max_queued=1) == ()
    rows = run_rows(owner, [paused, queued])
    assert rows[paused]["state"] == "queued"
    assert rows[paused]["incident_state"] == "paused"
    assert rows[queued]["state"] == "queued"
    assert events_of(owner, [paused], "run_handoff") == []


@pytest.mark.parametrize("value", ["0", "-1", "two"])
def test_invalid_backlog_max_refuses_worker_start(workers, value):
    """§3 env contract: ``OPSPILOT_BACKLOG_MAX`` is a positive integer."""
    worker = workers(OPSPILOT_BACKLOG_MAX=value)
    code = worker.wait_exit(20)
    assert code is not None and code != 0, worker.log()
