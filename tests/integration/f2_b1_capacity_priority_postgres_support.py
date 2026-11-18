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
from uuid import UUID, uuid4

import psycopg
import pytest

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
    ModelStub,
    WorkerProcess,
    events_of,
    make_certificate,
    neutralize_claimable,
    run_rows,
)
from tests.integration.m1_04_alert_intake_impl_postgres_support import (
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


# -- item 1: capacity --------------------------------------------------------


# -- item 2: priority, then deadline ----------------------------------------


# -- item 3: a late critical takes the next free slot ------------------------


# -- item 4: a renewed Run keeps the incident's priority ---------------------


# -- item 5: one Run's model timeout does not abort another ------------------


# -- item 6: a crashed slot -------------------------------------------------


# -- item 7: pool constraint and the default-configuration storm -------------


# -- item 8: human-held incidents are never claimed --------------------------
