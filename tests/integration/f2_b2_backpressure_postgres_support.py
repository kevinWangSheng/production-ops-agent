"""Independent PostgreSQL acceptance tests for F2 PR-B2.

The tests use only the real Alertmanager intake, durable rows/events and the
public ``park_backlog`` helper.  The worker is a child process; its model
provider is the local B1 HTTPS stub, so no real model or external service is
called.  B2 is deliberately absent on this branch, therefore red tests before
the implementation is merged are expected.
"""

from __future__ import annotations

import os
import re
import threading
import time
from collections import Counter
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb

from opspilot.investigation import progress
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
from tests.integration.f2_b1_capacity_priority_postgres_support import REGISTRY
from tests.integration.f2_b1_support import (
    ModelStub,
    WorkerProcess,
    events_of,
    make_certificate,
    neutralize_claimable,
)
from tests.integration.m1_04_alert_intake_impl_postgres_support import _alert, _webhook
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
    target_id, resource_uid = conn.execute(
        "SELECT i.target_id,t.resource_uid FROM opspilot_incidents i "
        "JOIN opspilot_targets t ON t.target_id=i.target_id WHERE i.incident_id=%s",
        (incident,),
    ).fetchone()
    # The snapshot a real session records (``Target.model_dump()``): the
    # shared lab target's later sessions compare their identity with it.
    identity = REGISTRY[resource_uid]
    target = {
        "integration_id": identity["integration_id"],
        "cluster_uid": identity["cluster_uid"],
        "namespace": identity["namespace"],
        "resource_uid": resource_uid,
        "revision": None,
    }
    conn.execute(
        "UPDATE opspilot_incidents SET observation_generation=1 WHERE incident_id=%s",
        (incident,),
    )
    conn.execute(
        "INSERT INTO opspilot_observation_sessions(session_id,incident_id,purpose,target_id,target,"
        "subject_control_generation,observation_generation,authorized_by,state,ended_reason,"
        "authorized_global_generation,authorized_target_generation,deadline_at,max_samples,"
        "sample_interval_seconds,sustained_window_seconds) VALUES(%s,%s,'incident_recovery',%s,%s,"
        "0,1,'op','completed','recovery_confirmed',0,0,clock_timestamp(),1,60,60)",
        (session_id, incident, target_id, Jsonb(target)),
    )
    conn.execute(
        "INSERT INTO opspilot_observation_endings(ending_id,session_id,incident_id,ended_reason,"
        "lifecycle_before,lifecycle_after) VALUES(%s,%s,%s,'deadline_expired','observing_recovery',"
        "'observing_recovery')",
        (ending_id, session_id, incident),
    )
    conn.commit()
